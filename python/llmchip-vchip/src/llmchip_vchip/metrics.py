"""Quality and speed metrics of generations, and the comparison with a reference or golden chip.

A metric that cannot be computed is None with the reason in `missing`, never 0: "not
measured" must not read as "measured and zero".
"""

from __future__ import annotations

import math
import statistics

import numpy as np

from .generator import Generation
from .interface import ChipMeta


def _pct(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def repetition_rate(tokens: list[int], n: int = 3) -> float | None:
    """Share of the n-grams of `tokens` that repeat an earlier one (0: none, near 1: a loop)."""
    grams = [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]
    return None if not grams else 1 - len(set(grams)) / len(grams)


def speed(gens: list[Generation], meta: ChipMeta) -> tuple[dict, dict]:
    """(metrics, missing reasons). Cycles of decode steps; tok/s from the mean cycles of a token
    including the host frames, at the chip's declared clock."""
    missing: dict[str, str] = {}
    decode = [s for g in gens for s in g.decode_steps]
    out: dict = {"tokens": len(decode), "simulated": True}
    walls = [s.wall_s for s in decode]
    out["wall_s_per_token_median"] = statistics.median(walls) if walls else None
    if "cycles" not in meta.capabilities:
        missing["cycles"] = "the chip does not declare the `cycles` capability"
        return out, missing
    if not decode:
        missing["cycles"] = "no decode step completed"
        return out, missing
    compute = [s.cycles for s in decode]
    unknown = sum(c == 0 for c in compute)
    if unknown:  # 0 means "not reported"; a mean over it would be a made-up speed
        missing["cycles"] = f"the chip declares `cycles` but {unknown} of {len(compute)} decode steps reported none"
        return out, missing
    host = [s.host_cycles for s in decode]
    total = [c + h for c, h in zip(compute, host, strict=True)]
    firsts = [g.decode_steps[0].cycles for g in gens if g.decode_steps]
    lasts = [g.decode_steps[-1].cycles for g in gens if g.decode_steps]
    out |= {
        "cycles_per_token_median": statistics.median(compute),
        "cycles_per_token_mean": statistics.fmean(compute),
        "cycles_per_token_min": min(compute),
        "cycles_per_token_max": max(compute),
        "cycles_per_token_first": statistics.median(firsts),
        "cycles_per_token_last": statistics.median(lasts),
        "host_cycles_per_token": statistics.median(host) if meta.host["kind"] != "direct" else None,
        "host_cycles_source": sorted({s.host_source for s in decode}),
        "total_cycles_per_token_mean": statistics.fmean(total),
    }
    if meta.host["kind"] == "direct":
        missing["host_cycles_per_token"] = "direct host: no frames; tok/s is compute only"
    prefill = [s.cycles + s.host_cycles for g in gens for s in g.prefill_steps]
    out["prefill_cycles_per_token_mean"] = statistics.fmean(prefill) if prefill else None
    clock = meta.clock_mhz
    if clock is None:
        missing["tok_s"] = "the chip declares no clock (clock_mhz)"
        return out, missing
    hz = clock * 1e6
    out |= {
        "clock_mhz": clock,
        "tok_s": hz / out["total_cycles_per_token_mean"],
        "tok_s_compute_only": hz / out["cycles_per_token_mean"],
        "latency_ms_p50": _pct(total, 50) / hz * 1e3,
        "latency_ms_p95": _pct(total, 95) / hz * 1e3,
    }
    ttft = []
    for g in gens:
        if g.decode_steps:
            pre = sum(s.cycles + s.host_cycles for s in g.prefill_steps)
            first = g.decode_steps[0]
            ttft.append((pre + first.cycles + first.host_cycles) / hz * 1e3)
    out["time_to_first_token_ms_median"] = statistics.median(ttft) if ttft else None
    return out, missing


def quality(gens: list[Generation], expected: list[str | None], texts: list[str]) -> dict:
    """Metrics that need no reference: length, stopping, repetition, expected text."""
    reps = [r for g in gens if (r := repetition_rate(g.tokens)) is not None]
    reasons: dict[str, int] = {}
    for g in gens:
        reasons[g.stop_reason] = reasons.get(g.stop_reason, 0) + 1
    out: dict = {
        "prompts": len(gens),
        "errors": sum(g.error is not None for g in gens),
        "stop_reasons": reasons,
        "length_mean": statistics.fmean([len(g.tokens) for g in gens]) if gens else None,
        "repetition_rate": statistics.fmean(reps) if reps else None,
    }
    judged = [(e, t) for e, t in zip(expected, texts, strict=True) if e is not None]
    out["expected_match"] = sum(t.strip().startswith(e.strip()) for e, t in judged) / len(judged) if judged else None
    return out


def agreement(chip_forced: list[Generation], ref_gens: list[Generation]) -> dict:
    """Teacher-forced next-token agreement: the chip is fed the reference's sequence and its
    prediction at every generated position is compared with the reference's token."""
    match = total = 0
    errors: list[dict] = []  # the chip failed on a sequence the reference produced: counted, not skipped
    for forced, ref in zip(chip_forced, ref_gens, strict=True):
        if ref.error:
            continue
        if forced.error:
            errors.append(forced.error)
            continue
        n = len(ref.prompt)
        for s in forced.steps:
            if s.pos >= n - 1:
                expected = ref.decode_steps[s.pos - (n - 1)].output
                match += s.output == expected
                total += 1
    return {
        "next_token_agreement": match / total if total else None,
        "agreement_positions": total,
        "forced_errors": errors[:5],
        "forced_error_count": len(errors),
    }


def sequences(chip_gens: list[Generation], ref_gens: list[Generation]) -> dict:
    """Free-running generations compared token by token: share identical, first divergence."""
    same, divergences = 0, []
    for g, r in zip(chip_gens, ref_gens, strict=True):
        a = [s.output for s in g.decode_steps]
        b = [s.output for s in r.decode_steps]
        if a == b and g.error is None and r.error is None:
            same += 1
            divergences.append(None)
        else:
            divergences.append(
                next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
            )
    n = len(chip_gens)
    return {"sequence_match": same / n if n else None, "first_divergence": divergences}


def bit_exact(chip_forced: list[Generation], golden_forced: list[Generation], chip_gens, golden_gens) -> dict:
    """Golden comparison: tokens, and logits and traces where both chips return them, identical."""
    checked, diffs = {"tokens": 0, "logits": 0, "traces": 0}, []
    for a_run, b_run in [*zip(chip_forced, golden_forced, strict=True), *zip(chip_gens, golden_gens, strict=True)]:
        if a_run.error or b_run.error:
            same_refusal = a_run.error and b_run.error and a_run.error["code"] == b_run.error["code"] == "invalid_input"
            if not same_refusal:  # both refusing the same unencodable prompt is agreement, not a difference
                diffs.append({"error": a_run.error or b_run.error})
            continue
        for a, b in zip(a_run.steps, b_run.steps, strict=False):
            checked["tokens"] += 1
            if a.output != b.output:
                diffs.append({"pos": a.pos, "what": "token", "chip": a.output, "golden": b.output})
            if a.logits is not None and b.logits is not None:
                checked["logits"] += 1
                if a.logits != b.logits:
                    diffs.append({"pos": a.pos, "what": "logits"})
            if a.traces and b.traces:
                for key in sorted(set(a.traces) & set(b.traces)):
                    checked["traces"] += 1
                    if a.traces[key] != b.traces[key]:
                        diffs.append({"pos": a.pos, "what": f"trace {key}"})
        if len(a_run.steps) != len(b_run.steps):
            diffs.append({"what": "length", "chip": len(a_run.steps), "golden": len(b_run.steps)})
    return {"bit_exact": not diffs and checked["tokens"] > 0, "compared": checked, "differences": diffs[:20]}


def perplexity(forced: list[Generation]) -> dict:
    """exp(mean NLL) of each next token of teacher-forced passages, from the chip's logits."""
    nll, count, errors = 0.0, 0, 0
    for g in forced:
        if g.error:
            errors += 1
            continue
        for s in g.steps[:-1]:
            if s.logits is None:
                return {"perplexity": None, "perplexity_tokens": 0, "perplexity_errors": errors, "no_logits": True}
            x = np.asarray(s.logits, dtype=np.float64)
            mx = x.max()
            nll += mx + math.log(np.exp(x - mx).sum()) - x[g.prompt[s.pos + 1]]
            count += 1
    return {
        "perplexity": math.exp(nll / count) if count else None,
        "perplexity_tokens": count,
        "perplexity_errors": errors,
    }
