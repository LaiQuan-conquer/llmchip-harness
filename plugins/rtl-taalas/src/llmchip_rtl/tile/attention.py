"""The attention pass of `taalas-tile/1` as a scalar reference (drafts/tile-180nm-pipeline.md §12).

`attention_ref` is the golden model the RTL (`tile_attention`) is compared against. It is written
against `GraphL2.attention`, not against the prose of the design draft, because the three details
of §12.4 are exactly where a hand-written version drifts:

- the dot product is multiplied by `att_scale = round(2^15 / sqrt(headDim))` before the shift; the
  scale is not a power of two in general (`headDim = 8` gives 11,585), so it cannot be folded into
  `SCORE_SHIFT`;
- `k` and `v` are the model's KV cache layout, `(S, kv_heads * hd)`, not one position's row;
- every scaling shift uses `arith.rshift`'s rounding (half-up by default), as `graph_l2` does.

The model's tensor layout is reproduced literally: a query of one position sees all `S` cached
positions (the causal mask in a T=1 pass admits `j <= S-1`), grouped-query heads repeat each KV
head `heads // kv_heads` times, and the result is `(heads * hd,)` int16 in the model's layout.
"""

from __future__ import annotations

import math

import numpy as np
from llmchip_numeric import arith, graph_l2


def att_scale_of(head_dim: int) -> int:
    """The model's attention scale: `round(2^15 / sqrt(head_dim))` (`GraphL2.att_scale`)."""
    return round(2**15 / math.sqrt(head_dim))


def attention_head_ref(
    q_head: np.ndarray,
    k: np.ndarray,
    v: np.ndarray,
    exp_lut: np.ndarray,
    fq: int,
    fk: int,
    hd: int,
    kv_head: int,
    score_frac: int = graph_l2.SCORE_FRAC,
    exp_range: int = graph_l2.EXP_RANGE,
    att_scale: int | None = None,
    rounding: str = "half-up",
    overflow: str = "saturate",
) -> np.ndarray:
    """One query head against the cached keys and values of its KV head; returns `hd` int16 values.

    `q_head` is `hd` int16 values, `k` and `v` are `(S, kv_heads * hd)` as in `GraphL2.attention`,
    and `kv_head` selects the row of the cache this query head repeats. The arithmetic is the
    model's, step by step, so a mismatch names the stage that differs.
    """
    q_head = np.asarray(q_head, dtype=np.int64).reshape(hd)
    k = np.asarray(k, dtype=np.int64)
    v = np.asarray(v, dtype=np.int64)
    positions = k.shape[0]
    kv_heads = k.shape[1] // hd
    scale = att_scale_of(hd) if att_scale is None else int(att_scale)

    keys = k.reshape(positions, kv_heads, hd)[:, kv_head, :]
    values = v.reshape(positions, kv_heads, hd)[:, kv_head, :]

    # graph_l2: raw = rshift((q . k) * att_scale, fq + fk + 15 - SCORE_FRAC), then int32.
    dots = keys @ q_head
    raw = arith.rshift(dots * scale, fq + fk + 15 - score_frac, rounding)
    scores = arith.overflow(raw, 32, overflow)

    smax = int(scores.max())
    d = np.minimum(smax - scores, exp_range * 2**score_frac - 1)
    e = np.asarray(exp_lut, dtype=np.int64)[d >> (score_frac - 5)]
    denominator = int(e.sum())
    p = (e << 15) // denominator

    # graph_l2: out = rshift(p . v, 15), saturated to int16.
    summed = values.T @ p
    return arith.overflow(arith.rshift(summed, 15, rounding), 16, overflow)


def attention_ref(
    q: np.ndarray,
    k: np.ndarray,
    v: np.ndarray,
    exp_lut: np.ndarray,
    fq: int,
    fk: int,
    heads: int,
    kv_heads: int,
    hd: int,
    score_frac: int = graph_l2.SCORE_FRAC,
    exp_range: int = graph_l2.EXP_RANGE,
    att_scale: int | None = None,
    rounding: str = "half-up",
    overflow: str = "saturate",
) -> np.ndarray:
    """The attention output of one position: `(heads * hd,)` int16, in the model's layout.

    `q` is `(heads * hd,)` or `(1, heads * hd)`; `k` and `v` are the `(S, kv_heads * hd)` KV cache
    of the positions fed so far. With T=1 the causal mask admits every cached position, so the
    reference is the loop of `attention_head_ref` over the heads.
    """
    q = np.asarray(q, dtype=np.int64)
    if q.ndim == 2:
        if q.shape[0] != 1:
            raise ValueError("attention_ref covers one query position (T=1)")
        q = q[0]
    q = q.reshape(heads, hd)
    group = heads // kv_heads
    out = np.zeros(heads * hd, dtype=np.int64)
    for head in range(heads):
        out[head * hd : (head + 1) * hd] = attention_head_ref(
            q[head],
            k,
            v,
            exp_lut,
            fq,
            fk,
            hd,
            head // group,
            score_frac=score_frac,
            exp_range=exp_range,
            att_scale=att_scale,
            rounding=rounding,
            overflow=overflow,
        )
    return out
