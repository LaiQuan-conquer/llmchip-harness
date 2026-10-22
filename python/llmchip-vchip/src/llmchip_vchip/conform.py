"""Conformance: the checks every virtual chip must pass before its numbers mean anything
(design T-01..T-09). They test the chip against the interface, not the model's quality.

Each check passes, fails, is n/a (does not apply to this chip, e.g. frames on a direct host),
or is not_run (it applies but could not be checked, e.g. no prompt fits the tokenizer), always
with the reason. The chip conforms (`pass`) only when no check fails or is not_run; a not_run
check makes the result `incomplete`, never a pass.
"""

from __future__ import annotations

from . import metrics
from .generator import check_prompt, generate, teacher_force
from .interface import ChipError
from .prompts import PromptSet
from .registry import Registry
from .runner import Session


def _check(cid: str, title: str, verdict: str, detail: str = "") -> dict:
    return {"id": cid, "title": title, "verdict": verdict, "detail": detail}


def _expect_error(fn, code: str) -> str | None:
    """None when `fn` raises ChipError(code); else what happened instead."""
    try:
        fn()
    except ChipError as e:
        return None if e.code == code else f"raised {e.code} ({e.message}), expected {code}"
    except Exception as e:
        return f"raised {type(e).__name__}: {e}, expected ChipError {code}"
    return f"no error, expected {code}"


def conform(registry: Registry, chip_id: str, prompts: PromptSet, max_tokens: int = 16, quick: bool = False) -> dict:
    checks: list[dict] = []
    entry = registry.get(chip_id)
    if not entry.available:
        checks.append(_check("T-01", "manifest valid", "fail", entry.reason))
        return {"chip": chip_id, "verdict": "fail", "checks": checks}
    try:
        s = Session(entry.manifest)
    except Exception as e:  # anything that keeps the chip from opening is a T-01 failure
        checks.append(_check("T-01", "manifest valid, chip opens", "fail", str(e)))
        return {"chip": chip_id, "verdict": "fail", "checks": checks}
    checks.append(_check("T-01", "manifest valid, chip opens", "pass", f"backend {s.meta.backend}"))
    try:
        _run(s, prompts, max_tokens, checks, registry, quick)
    finally:
        s.close()
    verdicts = {c["verdict"] for c in checks}
    overall = "fail" if "fail" in verdicts else "incomplete" if "not_run" in verdicts else "pass"
    return {"chip": chip_id, "verdict": overall, "checks": checks}


def _run(s: Session, prompts: PromptSet, max_tokens: int, checks: list[dict], registry: Registry, quick: bool) -> None:
    meta, chip, tok = s.meta, s.chip, s.tokenizer
    # T-02 tokenizer round trip
    encoded: list[tuple[str, list[int]]] = []
    for c in prompts.cases:
        try:
            ids = tok.encode(c.prompt)
            check_prompt(ids, meta.vocab, meta.context)
            encoded.append((c.prompt, ids))
        except ChipError:
            continue
    if not encoded:
        checks.append(
            _check("T-02", "tokenizer round trip", "not_run", "no prompt of the set fits this tokenizer and context")
        )
        for cid in ("T-03", "T-04", "T-05", "T-06", "T-07", "T-08", "T-09", "T-15", "T-16", "T-17", "T-18", "T-19"):
            checks.append(_check(cid, "needs an encodable prompt", "not_run", "no prompt of the set could be used"))
        return
    bad = [p for p, _ in encoded if not tok.roundtrip(p)]
    checks.append(
        _check(
            "T-02",
            "tokenizer round trip",
            "fail" if bad else "pass",
            f"not restored: {bad[:3]}" if bad else f"{len(encoded)} prompts",
        )
    )
    # T-03 reset / prefill
    _, ids = encoded[0]
    try:
        chip.reset()
        results = chip.prefill(ids)
        ok = len(results) == len(ids) and all(0 <= r.token < meta.vocab for r in results)
        checks.append(
            _check(
                "T-03", "reset and prefill", "pass" if ok else "fail", f"{len(results)} results for {len(ids)} tokens"
            )
        )
    except Exception as e:
        checks.append(_check("T-03", "reset and prefill", "fail", str(e)))
    # T-04 greedy generation; prefill and step agree on the same sequence
    gens = [generate(s.host, ids, max_tokens) for _, ids in encoded[:3]]
    errors = [g.error for g in gens if g.error]
    if errors:
        checks.append(_check("T-04", "greedy generation", "fail", str(errors[0])))
    else:
        mismatch = []
        for g in gens:
            outs = [st.output for st in g.decode_steps]
            forced = teacher_force(s.host, g.prompt + outs[:-1])
            if forced.error:
                mismatch.append(forced.error)
                continue
            predicted = [st.output for st in forced.steps[len(g.prompt) - 1 :]]
            if predicted != outs:
                mismatch.append(f"prompt {g.prompt[:4]}...: step gave {outs[:8]}, prefill {predicted[:8]}")
        checks.append(
            _check(
                "T-04",
                "greedy generation; prefill agrees with step",
                "fail" if mismatch else "pass",
                str(mismatch[0]) if mismatch else f"{sum(len(g.tokens) for g in gens)} tokens",
            )
        )
    # T-05 context boundary
    full = (ids * (meta.context // len(ids) + 1))[: meta.context]
    problems = []
    try:
        chip.reset()
        if len(chip.prefill(full)) != meta.context:
            problems.append("prefill of a full context did not return one result per position")
    except Exception as e:
        problems.append(f"prefill of a full context failed: {e}")
    beyond = _expect_error(lambda: chip.step(full[-1], meta.context), "invalid_input")
    if beyond:
        problems.append(f"step at position {meta.context} (= context): {beyond}")
    over = generate(s.host, [*full, full[0]], 1)
    if not over.error or over.error["code"] != "context_overflow":
        problems.append(f"a prompt longer than the context ended {over.stop_reason}, expected context_overflow")
    at_end = generate(s.host, full, max_tokens)
    if at_end.error or len(at_end.tokens) > 1 or (at_end.decode_steps and at_end.decode_steps[-1].pos >= meta.context):
        problems.append(f"a prompt filling the context generated {len(at_end.tokens)} tokens ({at_end.error})")
    checks.append(_check("T-05", "context boundary", "fail" if problems else "pass", "; ".join(problems)))
    # T-06 stop tokens
    stop = set(meta.tokenizer.get("stop", []))
    if not stop:
        checks.append(_check("T-06", "stop at a stop token", "n/a", "the tokenizer declares no stop token"))
    else:
        # Long enough to meet a stop token on a trained model, bounded for large contexts.
        long = [generate(s.host, i, min(meta.context, 256)) for _, i in encoded[:3]]
        stopped = [g for g in long if g.stop_reason == "stop_token"]
        if not stopped:
            checks.append(
                _check(
                    "T-06",
                    "stop at a stop token",
                    "n/a",
                    "no generation produced a stop token (the stop rule itself is the harness's, tested in its suite)",
                )
            )
        else:
            ok = all(g.decode_steps[-1].output in stop and not set(g.tokens) & stop for g in stopped)
            checks.append(
                _check("T-06", "stop at a stop token", "pass" if ok else "fail", f"{len(stopped)} generations stopped")
            )
    # T-07 invalid input, and the chip still works afterwards
    problems = []
    chip.reset()
    for label, fn in (
        (f"token {meta.vocab}", lambda: chip.step(meta.vocab, 0)),
        ("token -1", lambda: chip.step(-1, 0)),
        ("position -1", lambda: chip.step(ids[0], -1)),
    ):
        err = _expect_error(fn, "invalid_input")
        if err:
            problems.append(f"{label}: {err}")
    again = generate(s.host, gens[0].prompt, max_tokens) if not errors else None
    if again is not None and (again.error or again.tokens != gens[0].tokens):
        problems.append("after invalid inputs and a reset, the chip no longer generates the same tokens")
    checks.append(
        _check("T-07", "invalid input rejected, chip recovers", "fail" if problems else "pass", "; ".join(problems))
    )
    # T-08 host frames
    if meta.host["kind"] == "direct":
        checks.append(_check("T-08", "host frames counted", "n/a", "direct host: no frames"))
    elif errors:
        checks.append(_check("T-08", "host frames counted", "not_run", "no generation completed"))
    else:
        least = s.host.frame_cycles()
        steps = [st for g in gens for st in g.decode_steps]
        short = [st for st in steps if st.host_cycles < least]
        checks.append(
            _check(
                "T-08",
                "host frames counted",
                "fail" if short else "pass",
                f"{len(short)} steps report fewer host cycles than START + STATUS need ({least})"
                if short
                else f"{least} cycles per token ({steps[0].host_source})",
            )
        )
    # T-09 determinism
    if errors:
        checks.append(_check("T-09", "deterministic", "not_run", "no generation completed"))
    else:
        repeat = [generate(s.host, g.prompt, max_tokens) for g in gens]
        same = all(
            [(st.output, st.cycles, st.host_cycles) for st in a.steps]
            == [(st.output, st.cycles, st.host_cycles) for st in b.steps]
            for a, b in zip(gens, repeat, strict=True)
        )
        checks.append(_check("T-09", "deterministic", "pass" if same else "fail", "tokens and cycles of two runs"))
    # Cycles reported when declared
    if "cycles" not in meta.capabilities:
        checks.append(_check("T-12", "cycles reported", "n/a", "the chip does not declare `cycles`"))
    elif not errors:
        zero = [st.pos for g in gens for st in g.decode_steps if st.cycles <= 0]
        checks.append(
            _check(
                "T-12",
                "cycles reported",
                "fail" if zero else "pass",
                f"steps without cycles at {zero[:5]}" if zero else "",
            )
        )
    else:
        checks.append(_check("T-12", "cycles reported", "not_run", "no generation completed"))
    # L3 checks (design §8.6): the hardware bridge, trusted cycles, the host interface on the pins,
    # and the acceptance run.
    _hardware(s, gens, errors, checks, registry, prompts, quick)


def _hardware(
    s: Session, gens: list, errors: list, checks: list[dict], registry: Registry, prompts, quick: bool
) -> None:
    from .bridges.rtl_bridge import HOST_CHECKS

    meta, chip = s.meta, s.chip
    by_id = {c["id"]: c["verdict"] for c in checks}
    bridged = meta.backend in metrics.TESTBENCH_BACKENDS
    if bridged:
        basics = [by_id.get(i) for i in ("T-01", "T-03", "T-04")]
        source = getattr(chip, "hello", {}).get("measurement")
        ok = basics == ["pass"] * 3 and source == "testbench"
        detail = f"hello/reset/prefill/step through the bridge; measurement {source}"
        checks.append(_check("T-15", "hardware bridge loads and steps", "pass" if ok else "fail", detail))
    else:
        checks.append(_check("T-15", "hardware bridge loads and steps", "n/a", f"backend {meta.backend} has no bridge"))
    if not meta.measurement:
        checks.append(_check("T-16", "trusted cycles", "n/a", "the chip declares no measurement (not L3 hardware)"))
    elif errors:
        checks.append(_check("T-16", "trusted cycles", "not_run", "no generation completed"))
    else:
        block, reasons = metrics.measurement(gens, meta)
        if block["cycles_match_design"] is False:
            reasons.append(f"cycles differ from the design's {block['cycles_per_token_design']}")
        detail = (
            "; ".join(reasons) or f"testbench counted every step; equal to the design: {block['cycles_match_design']}"
        )
        checks.append(_check("T-16", "trusted cycles", "fail" if reasons else "pass", detail))
    # T-17: only a testbench bridge's own check counts; a chip's report about itself does not.
    if not bridged:
        verdict = "fail" if meta.measurement.get("trusted") else "n/a"
        checks.append(_check("T-17", "host interface on the pins", verdict, "no testbench bridge drives the pins"))
    else:
        try:
            result = chip.op("host_check")
            got = result.get("checks", {})
            failed = [k for k in HOST_CHECKS if got.get(k) is not True]
            if set(got) != set(HOST_CHECKS):
                failed.append(f"check set {sorted(got)} is not {list(HOST_CHECKS)}")
        except ChipError as e:
            failed = [f"host_check: {e}"]
        failed += _read_against_golden(s, registry)
        detail = f"failed: {', '.join(failed)}" if failed else f"{', '.join(HOST_CHECKS)}; READ equals the golden"
        checks.append(_check("T-17", "host interface on the pins", "fail" if failed else "pass", detail))
    # T-18 / T-19: the acceptance run of the chip's policy (golden, all prompts, policy length).
    policy = s.manifest.policy()
    if not bridged or not policy.get("golden"):
        why = "no testbench bridge" if not bridged else "the policy names no golden chip"
        checks.append(_check("T-18", "bit-exact with the golden on the prompt set", "n/a", why))
        checks.append(_check("T-19", "trusted speed", "n/a", why))
        return
    if quick:
        for cid, title in (("T-18", "bit-exact with the golden on the prompt set"), ("T-19", "trusted speed")):
            checks.append(_check(cid, title, "not_run", "--quick: run `vchip bench` or conform without --quick"))
        return
    import tempfile
    from pathlib import Path

    from .runner import bench

    with tempfile.TemporaryDirectory() as tmp:
        r = bench(registry, meta.id, prompts, out_dir=Path(tmp))
    q, sp = r["quality"], r["speed"]
    n = f"{r['prompt_set']['prompts']} prompts x {r['max_tokens']} tokens"
    ok18 = q.get("bit_exact") is True and q["errors"] == 0 and q.get("comparison_errors", 0) == 0
    checks.append(
        _check(
            "T-18",
            "bit-exact with the golden on the prompt set",
            "pass" if ok18 else "fail",
            f"{n}; {q.get('bit_exact')}",
        )
    )
    least = policy.get("speed", {}).get("tok_s_min", 1000)
    tok_s = sp.get("tok_s")
    ok19 = sp.get("measurement_trusted") is True and tok_s is not None and tok_s >= least
    checks.append(
        _check(
            "T-19",
            "trusted speed",
            "pass" if ok19 else "fail",
            f"{tok_s} tok/s (at least {least}), trusted {sp.get('measurement_trusted')}",
        )
    )


def _read_against_golden(s: Session, registry: Registry) -> list[str]:
    """One step on the chip and on its golden: the observation words read on the pins (logits and
    traces) must equal the golden's."""
    golden_id = s.manifest.policy().get("golden")
    if not golden_id:
        return ["the policy names no golden chip to compare READ with"]
    try:
        g = Session(registry.get(golden_id).manifest)
    except Exception as e:
        return [f"golden {golden_id} does not open: {e}"]
    try:
        out = []
        for chip in (s.chip, g.chip):
            chip.reset()
        a, b = s.chip.step(2, 0), g.chip.step(2, 0)
        if a.token != b.token:
            out.append(f"token {a.token} != golden {b.token}")
        if a.logits is None or a.logits != b.logits:
            out.append("logits read on the pins differ from the golden")
        if not a.traces or a.traces != b.traces:
            out.append("observation words read on the pins differ from the golden")
        return out
    finally:
        g.close()
