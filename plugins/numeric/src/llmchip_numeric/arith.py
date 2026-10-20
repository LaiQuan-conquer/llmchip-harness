"""Integer arithmetic of the numeric spec: rounding shifts, overflow handling, accumulation.

One definition used by L2 (`l2.py`) and the graph L2 (`graph_l2.py`). The RTL
(`llmchip_functions.vh`) and the instruction-set simulator shipped in the RTL bundle
(`isa.py`) implement the same rules; their tests check them against this file.

- Rounding applies to every shift that scales a result down (`rshift`):
  `half-up` (v + 2^(s-1)) >> s, `half-even` (ties to the even neighbour), `truncate`
  v >> s (floor). Table-index shifts and divisions are floors whatever the mode.
- Overflow applies where a result is narrowed to its storage width (`overflow`):
  `saturate` clamps to the range, `wrap` keeps the low bits (two's complement).
- A matrix-vector product sums per-group partial results; with an accumulator narrower
  than the proven bound the groups are added in index order and every addition is
  narrowed (`accumulate`).
"""

from __future__ import annotations

import numpy as np

ROUNDING = ("half-up", "half-even", "truncate")
OVERFLOW = ("saturate", "wrap")


def modes_of(spec: dict) -> tuple[str, str]:
    """(rounding, overflow) of a numeric spec. Specs written before the modes existed say
    "round-half-up" (as text) and have no overflow field: half-up and saturate."""
    rounding = str(spec.get("rounding", {}).get("mode", "half-up")).removeprefix("round-")
    return rounding, spec.get("overflow", {}).get("mode", "saturate")


def rshift(v, s, mode: str = "half-up"):
    """v / 2^s rounded per `mode` (arithmetic shifts); a negative s shifts left exactly.
    `s` may be an array broadcast against `v` (one shift per row)."""
    if isinstance(s, np.ndarray):
        return np.where(s > 0, _right(v, np.maximum(s, 1), mode), v << np.maximum(-s, 0))
    if s < 0:
        return v << (-s)
    if s == 0:
        return v
    return _right(v, s, mode)


def _right(v, s, mode: str):
    """The rounding right shift for s >= 1 (s an int or an array)."""
    if mode == "half-up":
        return (v + (1 << (s - 1))) >> s
    if mode == "half-even":
        return (v + ((1 << (s - 1)) - 1) + ((v >> s) & 1)) >> s
    if mode == "truncate":
        return v >> s
    raise ValueError(f"unknown rounding mode {mode}")


def out_of_range(v, bits: int) -> int:
    """How many values do not fit a signed `bits`-bit integer."""
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if isinstance(v, np.ndarray):
        return int(np.count_nonzero((v < lo) | (v > hi)))
    return int(v < lo or v > hi)


def overflow(v, bits: int, mode: str = "saturate"):
    """v narrowed to a signed `bits`-bit integer per `mode`."""
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if mode == "saturate":
        return np.clip(v, lo, hi) if isinstance(v, np.ndarray) else min(max(v, lo), hi)
    if mode == "wrap":
        return ((v - lo) & ((1 << bits) - 1)) + lo
    raise ValueError(f"unknown overflow mode {mode}")


def accumulate(terms: np.ndarray, bits: int | None, mode: str = "saturate") -> tuple[np.ndarray, int]:
    """Sums `terms` (..., groups) over the last axis. With `bits`, the groups are added in
    index order and every partial sum is narrowed to `bits`; returns (sum, overflow events)."""
    if bits is None:
        return terms.sum(axis=-1), 0
    acc = np.zeros(terms.shape[:-1], np.int64)
    events = 0
    for g in range(terms.shape[-1]):
        acc = acc + terms[..., g]
        events += out_of_range(acc, bits)
        acc = overflow(acc, bits, mode)
    return acc, events
