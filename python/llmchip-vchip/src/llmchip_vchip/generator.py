"""The generation loop. It lives in the harness, not in the chip, so that every backend is
driven by the same loop: same prompt tokens, same greedy rule, same stopping conditions.

Positions 0..n-2 of the prompt are prefilled; the last prompt token is stepped at n-1 and
each output is fed back until `max_tokens` tokens are generated, a stop token comes out,
or the context is full. Failures end the run with an error code; nothing is retried.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .hosts import DirectHost
from .interface import ChipError, StepResult


@dataclass
class StepRecord:
    pos: int
    input: int
    output: int
    cycles: int
    host_cycles: int
    host_source: str  # none | model | chip
    phase: str  # prefill | decode
    wall_s: float
    logits: list[float] | None = None
    traces: dict | None = None

    def row(self, with_traces: bool = False) -> dict:
        r = {k: v for k, v in self.__dict__.items() if k not in ("logits", "traces")}
        if with_traces and self.traces is not None:
            r["traces"] = self.traces
        return r


@dataclass
class Generation:
    prompt: list[int]
    steps: list[StepRecord] = field(default_factory=list)
    tokens: list[int] = field(default_factory=list)  # generated tokens, the stop token excluded
    stop_reason: str = ""  # max_tokens | stop_token | context | error
    error: dict | None = None  # {code, message}
    wall_s: float = 0.0

    @property
    def decode_steps(self) -> list[StepRecord]:
        return [s for s in self.steps if s.phase == "decode"]

    @property
    def prefill_steps(self) -> list[StepRecord]:
        return [s for s in self.steps if s.phase == "prefill"]


def _record(
    r: object, host_cycles: int, source: str, token: int, pos: int, phase: str, vocab: int, dt: float
) -> StepRecord:
    if not isinstance(r, StepResult):
        raise ChipError("invalid_output", f"step at position {pos} returned {type(r).__name__}, not a StepResult")
    if isinstance(r.token, bool) or not isinstance(r.token, int) or not 0 <= r.token < vocab:
        raise ChipError("invalid_output", f"step at position {pos} returned token {r.token!r} outside the vocabulary")
    if isinstance(r.cycles, bool) or not isinstance(r.cycles, int) or r.cycles < 0:
        raise ChipError("invalid_output", f"step at position {pos} returned cycles {r.cycles!r}")
    if isinstance(host_cycles, bool) or not isinstance(host_cycles, int) or host_cycles < 0:
        raise ChipError("invalid_output", f"step at position {pos} returned host cycles {host_cycles!r}")
    if r.logits is not None and (not isinstance(r.logits, list) or len(r.logits) != vocab):
        raise ChipError("invalid_output", f"step at position {pos} returned logits that are not {vocab} values")
    if r.traces is not None and not isinstance(r.traces, dict):
        raise ChipError("invalid_output", f"step at position {pos} returned traces that are not an object")
    return StepRecord(pos, token, r.token, r.cycles, host_cycles, source, phase, dt, r.logits, r.traces)


def _fail(g: Generation, e: Exception) -> Generation:
    code = e.code if isinstance(e, ChipError) else "crash"
    message = e.message if isinstance(e, ChipError) else f"{type(e).__name__}: {e}"
    g.stop_reason, g.error = "error", {"code": code, "message": message}
    return g


def check_prompt(ids: list[int], vocab: int, context: int) -> None:
    if not ids:
        raise ChipError("invalid_input", "the prompt has no tokens")
    bad = [t for t in ids if not 0 <= t < vocab]
    if bad:
        raise ChipError("invalid_input", f"prompt tokens outside the vocabulary: {bad[:5]}")
    if len(ids) > context:
        raise ChipError("context_overflow", f"the prompt has {len(ids)} tokens, the context {context}")


def _prefill(host: DirectHost, ids: list[int], g: Generation, vocab: int) -> None:
    t0 = time.monotonic()
    results = host.prefill(ids) if ids else []
    if len(results) != len(ids):
        raise ChipError("invalid_output", f"prefill of {len(ids)} tokens returned {len(results)} results")
    dt = (time.monotonic() - t0) / max(len(ids), 1)
    for pos, (r, hc, src) in enumerate(results):
        g.steps.append(_record(r, hc, src, ids[pos], pos, "prefill", vocab, dt))


def generate(host: DirectHost, ids: list[int], max_tokens: int, stop: set[int] | None = None) -> Generation:
    meta = host.chip.metadata()
    vocab, context = meta.vocab, meta.context
    stop = stop if stop is not None else set(meta.tokenizer.get("stop", []))
    g = Generation(list(ids))
    t_start = time.monotonic()
    try:
        check_prompt(ids, vocab, context)
        host.chip.reset()
        _prefill(host, ids[:-1], g, vocab)
        token, pos = ids[-1], len(ids) - 1
        g.stop_reason = "max_tokens"
        while len(g.tokens) < max_tokens:
            if pos >= context:
                g.stop_reason = "context"
                break
            t0 = time.monotonic()
            r, hc, src = host.step(token, pos)
            rec = _record(r, hc, src, token, pos, "decode", vocab, time.monotonic() - t0)
            g.steps.append(rec)
            if rec.output in stop:
                g.stop_reason = "stop_token"
                break
            g.tokens.append(rec.output)
            token, pos = rec.output, pos + 1
    except Exception as e:  # every failure is recorded, none is retried
        _fail(g, e)
    g.wall_s = time.monotonic() - t_start
    return g


def teacher_force(host: DirectHost, ids: list[int]) -> Generation:
    """Feeds `ids` at positions 0..n-1 regardless of the chip's outputs: the chip's prediction at
    every position (agreement with a reference, perplexity)."""
    meta = host.chip.metadata()
    g = Generation(list(ids))
    t_start = time.monotonic()
    try:
        check_prompt(ids, meta.vocab, meta.context)
        host.chip.reset()
        _prefill(host, ids, g, meta.vocab)
        g.stop_reason = "forced"
    except Exception as e:
        _fail(g, e)
    g.wall_s = time.monotonic() - t_start
    return g
