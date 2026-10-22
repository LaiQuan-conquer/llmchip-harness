"""Benchmark runs: one chip on a prompt set, compared with a reference and a golden chip when
given, judged by the chip's policy, written to a run directory with hashes for every input.

    runs/<run_id>/
      run.json        chip, inputs and their hashes, parameters, versions
      prompts.jsonl   the prompt set as run
      outputs.jsonl   per prompt: tokens, text, stop reason, error, every step
      report.json     metrics, comparisons, policy verdicts
      report.md       the same in words
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import time
from pathlib import Path

from . import __version__, metrics
from . import policy as pol
from .generator import Generation, generate, teacher_force
from .hosts import make_host
from .interface import ChipError
from .manifest import Manifest
from .prompts import PromptSet
from .registry import Registry, open_chip
from .report import markdown
from .tokenizer import HarnessTokenizer


def default_runs_dir() -> Path:
    return Path(os.environ.get("LLMCHIP_HOME", "~/.llmchip")).expanduser() / "vchip" / "runs"


def _encode_all(tok: HarnessTokenizer, ps: PromptSet) -> list[list[int] | ChipError]:
    out: list[list[int] | ChipError] = []
    for c in ps.cases:
        try:
            out.append(tok.encode(c.prompt))
        except ChipError as e:
            out.append(e)
    return out


def _failed(e: ChipError) -> Generation:
    return Generation([], stop_reason="error", error={"code": e.code, "message": e.message})


class Session:
    """An opened chip with its tokenizer and host."""

    def __init__(self, m: Manifest) -> None:
        self.manifest = m
        self.meta = m.meta()
        self.tokenizer = HarnessTokenizer(m)
        self.chip = open_chip(m)
        self.host = make_host(self.chip)
        self.hashes = m.hashes()

    def generate_all(self, encoded: list[list[int] | ChipError], max_tokens: int) -> list[Generation]:
        return [_failed(ids) if isinstance(ids, ChipError) else generate(self.host, ids, max_tokens) for ids in encoded]

    def force_all(self, sequences: list[list[int] | None]) -> list[Generation]:
        out = []
        for seq in sequences:
            if seq is None:
                out.append(_failed(ChipError("crash", "the sequence to replay is missing")))
            else:
                out.append(teacher_force(self.host, seq))
        return out

    def close(self) -> None:
        self.chip.close()


def _replay(gens: list[Generation], context: int) -> list[list[int] | None]:
    """The sequences to teacher-force: prompt plus every output but the last (which nothing follows),
    cut to `context` (a reference with a longer context generates past the chip's)."""
    return [
        None if g.error or not g.decode_steps else (g.prompt + [s.output for s in g.decode_steps][:-1])[:context]
        for g in gens
    ]


def _compatible(a: Session, b: Session) -> str | None:
    if a.hashes["tokenizer"] != b.hashes["tokenizer"] or a.meta.vocab != b.meta.vocab:
        return f"chip {b.meta.id} uses a different tokenizer or vocabulary"
    return None


def bench(
    registry: Registry,
    chip_id: str,
    prompts: PromptSet,
    *,
    max_tokens: int | None = None,
    reference: str | None = None,
    golden: str | None = None,
    out_dir: Path | None = None,
    traces: bool = False,
) -> dict:
    m = registry.get(chip_id).manifest
    policy = m.policy()
    max_tokens = max_tokens if max_tokens is not None else int(policy.get("max_tokens", 64))
    reference = reference or policy.get("reference")
    golden = golden or policy.get("golden")
    started = time.time()
    s = Session(m)
    others: dict[str, Session] = {}
    try:
        encoded = _encode_all(s.tokenizer, prompts)
        gens = s.generate_all(encoded, max_tokens)
        texts = [
            "" if isinstance(ids, ChipError) else s.tokenizer.continuation(ids, g.tokens)
            for ids, g in zip(encoded, gens, strict=True)
        ]
        quality = metrics.quality(gens, [c.expected for c in prompts.cases], texts)
        speed, missing = metrics.speed(gens, s.meta)
        comparisons: dict = {}
        compared_gens: dict[str, list[Generation]] = {}
        for role, other_id in (("reference", reference), ("golden", golden)):
            if not other_id:
                missing.setdefault(
                    "next_token_agreement" if role == "reference" else "bit_exact", f"no {role} chip given"
                )
                continue
            try:
                if other_id not in others:
                    others[other_id] = Session(registry.get(other_id).manifest)
                o = others[other_id]
            except (KeyError, ValueError, ChipError) as e:
                comparisons[role] = {"chip": other_id, "error": str(e)}
                continue
            reason = _compatible(s, o)
            if reason:
                comparisons[role] = {"chip": other_id, "error": reason}
                continue
            if other_id not in compared_gens:
                compared_gens[other_id] = o.generate_all(encoded, max_tokens)
            ref_gens = compared_gens[other_id]
            context = min(s.meta.context, o.meta.context)
            chip_forced = s.force_all(_replay(ref_gens, context))
            c = {"chip": other_id, **metrics.agreement(chip_forced, ref_gens), **metrics.sequences(gens, ref_gens)}
            if role == "golden":
                golden_forced = o.force_all(_replay(ref_gens, context))
                c |= metrics.bit_exact(chip_forced, golden_forced, gens, ref_gens)
            comparisons[role] = c
        for role in ("reference", "golden"):
            c = comparisons.get(role, {})
            if "error" in c:
                missing.setdefault("next_token_agreement" if role == "reference" else "bit_exact", c["error"])
        ref = comparisons.get("reference", {})
        quality |= {k: ref.get(k) for k in ("next_token_agreement", "sequence_match")}
        # Failures of the chip while replaying a comparison chip's sequences count as errors too.
        quality["comparison_errors"] = sum(c.get("forced_error_count", 0) for c in comparisons.values())
        for role, c in comparisons.items():
            if c.get("agreement_positions") == 0 and "error" not in c:
                missing.setdefault(
                    "next_token_agreement" if role == "reference" else "bit_exact",
                    f"no position could be compared with the {role} chip",
                )
        quality["bit_exact"] = comparisons.get("golden", {}).get("bit_exact")
        # Perplexity: held-out passages, teacher-forced, from the chip's logits.
        passages: list[list[int]] = []
        unencodable = truncated = 0
        for text in prompts.passages:
            try:
                ids = s.tokenizer.encode(text)
            except ChipError:
                unencodable += 1
                continue
            truncated += len(ids) > s.meta.context
            if len(ids) >= 2:
                passages.append(ids[: s.meta.context])
        quality["perplexity_passages"] = {
            "used": len(passages),
            "unencodable": unencodable,
            "cut_to_context": truncated,
        }
        if "logits" not in s.meta.capabilities:
            missing["perplexity"] = "the chip does not declare the `logits` capability"
            quality["perplexity"] = None
        elif not passages:
            missing["perplexity"] = "the prompt set has no passages this tokenizer can encode"
            quality["perplexity"] = None
        else:
            quality |= metrics.perplexity(s.force_all(passages))
            if quality.pop("no_logits", False):
                missing["perplexity"] = "the chip declares `logits` but returned none"
            elif quality["perplexity"] is None:
                missing["perplexity"] = f"every passage failed on the chip ({quality['perplexity_errors']} errors)"
            if ref and "error" not in ref and reference and "logits" in others[reference].meta.capabilities:
                quality["reference_perplexity"] = metrics.perplexity(others[reference].force_all(passages))[
                    "perplexity"
                ]
        verdict = pol.evaluate(policy, quality, speed, missing)
        run_id = f"{chip_id}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        report = {
            "format": "llmchip-vchip-report/1",
            "run_id": run_id,
            "chip": {
                "id": s.meta.id,
                "backend": s.meta.backend,
                "clock_mhz": s.meta.clock_mhz,
                "host": s.meta.host["kind"],
                "model": s.meta.model,
                "capabilities": s.meta.capabilities,
            },
            "hashes": s.hashes,
            "compared_hashes": {k: o.hashes for k, o in others.items()},
            "prompt_set": {"source": prompts.source, "prompts": len(prompts.cases), "passages": len(prompts.passages)},
            "max_tokens": max_tokens,
            "decoding": "greedy (argmax, lowest id on ties)",
            "quality": quality,
            "speed": speed,
            "comparisons": comparisons,
            "missing": missing,
            "policy": policy,
            "verdict": verdict,
            "samples": [
                {
                    "id": c.id,
                    "prompt": c.prompt,
                    "text": t,
                    "tokens": g.tokens,
                    "stop_reason": g.stop_reason,
                    "error": g.error,
                }
                for c, g, t in zip(prompts.cases, gens, texts, strict=True)
            ],
        }
        run_dir = (out_dir or default_runs_dir()) / run_id
        _write(run_dir, report, prompts, encoded, gens, texts, started, traces)
        report["run_dir"] = str(run_dir)
        return report
    finally:
        s.close()
        for o in others.values():
            o.close()


def _write(run_dir: Path, report: dict, prompts: PromptSet, encoded, gens, texts, started: float, traces: bool) -> None:
    run_dir.mkdir(parents=True, exist_ok=False)
    run = {
        "run_id": report["run_id"],
        "chip": report["chip"]["id"],
        "hashes": report["hashes"],
        "compared_hashes": report["compared_hashes"],
        "max_tokens": report["max_tokens"],
        "prompt_set": report["prompt_set"],
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
        "seconds": time.time() - started,
        "harness": {"llmchip-vchip": __version__, "python": platform.python_version()},
    }
    (run_dir / "run.json").write_text(json.dumps(run, indent=2))
    (run_dir / "prompts.jsonl").write_text(prompts.to_jsonl())
    with (run_dir / "outputs.jsonl").open("w") as f:
        for c, ids, g, t in zip(prompts.cases, encoded, gens, texts, strict=True):
            row = {
                "id": c.id,
                "prompt": c.prompt,
                "prompt_tokens": None if isinstance(ids, ChipError) else ids,
                "tokens": g.tokens,
                "text": t,
                "stop_reason": g.stop_reason,
                "error": g.error,
                "wall_s": g.wall_s,
                "steps": [st.row(with_traces=traces) for st in g.steps],
            }
            f.write(json.dumps(row) + "\n")
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (run_dir / "report.md").write_text(markdown(report))
