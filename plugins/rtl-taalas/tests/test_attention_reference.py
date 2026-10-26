"""The attention reference against `GraphL2.attention` (drafts/tile-180nm-pipeline.md §12.4).

The RTL block must reproduce the model's arithmetic, so the golden model is checked against the
model itself before any RTL is written. The three details the draft records are the tests: the
attention scale is multiplied before the shift (it is not a power of two), the keys and values are
the `(S, kv_heads * hd)` KV cache of every position fed so far, and the scaling shifts use
`graph_l2`'s `arith.rshift` rounding.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2
from llmchip_rtl.tile.attention import att_scale_of, attention_head_ref, attention_ref

TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"


@pytest.fixture(scope="module")
def l2():
    """`GraphL2` of the tiny Qwen3 fixture: the model's own attention, not a copy of it."""
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    stats = graph_l2.calibrate(graph, loader.get, [tokens])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=16)
    return graph_l2.GraphL2(graph, spec, arrays)


def _vectors(l2, seed: int, positions: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    h = l2.spec["hyper"]
    rng = np.random.default_rng(seed)
    q = rng.integers(-16384, 16384, size=h["heads"] * h["headDim"], dtype=np.int64)
    k = rng.integers(-16384, 16384, size=(positions, h["kvHeads"] * h["headDim"]), dtype=np.int64)
    v = rng.integers(-16384, 16384, size=(positions, h["kvHeads"] * h["headDim"]), dtype=np.int64)
    return q, k, v


def _model(l2, q: np.ndarray, k: np.ndarray, v: np.ndarray) -> np.ndarray:
    h = l2.spec["hyper"]
    return l2.attention(q.reshape(1, -1), k, v, l2.f["q"], l2.f["k"], h["heads"], h["kvHeads"], h["headDim"], None)[0]


@pytest.mark.parametrize("positions,seed", [(1, 3), (2, 5), (3, 7), (5, 11), (8, 13)])
def test_the_reference_matches_the_model(l2, positions, seed):
    h = l2.spec["hyper"]
    q, k, v = _vectors(l2, seed, positions)
    reference = attention_ref(q, k, v, l2.a["lut.exp"], l2.f["q"], l2.f["k"], h["heads"], h["kvHeads"], h["headDim"])
    assert np.array_equal(_model(l2, q, k, v), reference)


def test_every_head_matches_the_model_head_by_head(l2):
    """The RTL processes one head at a time, so the per-head reference is checked as well."""
    h = l2.spec["hyper"]
    q, k, v = _vectors(l2, 17, 4)
    model = _model(l2, q, k, v).reshape(h["heads"], h["headDim"])
    group = h["heads"] // h["kvHeads"]
    for head in range(h["heads"]):
        reference = attention_head_ref(
            q.reshape(h["heads"], h["headDim"])[head],
            k,
            v,
            l2.a["lut.exp"],
            l2.f["q"],
            l2.f["k"],
            h["headDim"],
            head // group,
        )
        assert np.array_equal(model[head], reference)


def test_the_attention_scale_is_not_folded_into_the_shift(l2):
    """`att_scale` is 11,585 for headDim 8: a shift-only score is a different model."""
    h = l2.spec["hyper"]
    assert att_scale_of(h["headDim"]) == l2.att_scale == 11585
    q, k, v = _vectors(l2, 19, 3)
    folded = attention_ref(
        q,
        k,
        v,
        l2.a["lut.exp"],
        l2.f["q"],
        l2.f["k"],
        h["heads"],
        h["kvHeads"],
        h["headDim"],
        att_scale=8192,  # 2^13: the nearest power-of-two substitute for 11,585
    )
    assert not np.array_equal(_model(l2, q, k, v), folded)


def test_the_kv_cache_layout_is_every_cached_position(l2):
    """A T=1 pass sees positions 0..S-1, not the last position's row alone."""
    h = l2.spec["hyper"]
    q, k, v = _vectors(l2, 23, 5)
    whole = attention_ref(q, k, v, l2.a["lut.exp"], l2.f["q"], l2.f["k"], h["heads"], h["kvHeads"], h["headDim"])
    last_only = attention_ref(
        q, k[-1:], v[-1:], l2.a["lut.exp"], l2.f["q"], l2.f["k"], h["heads"], h["kvHeads"], h["headDim"]
    )
    assert np.array_equal(_model(l2, q, k, v), whole)
    assert not np.array_equal(whole, last_only)


def test_the_shifts_use_the_models_rounding(l2):
    """A truncating shift drifts from the model; half-up is the model's `arith.rshift`."""
    h = l2.spec["hyper"]
    q, k, v = _vectors(l2, 29, 6)
    half_up = attention_ref(q, k, v, l2.a["lut.exp"], l2.f["q"], l2.f["k"], h["heads"], h["kvHeads"], h["headDim"])
    floor = attention_ref(
        q,
        k,
        v,
        l2.a["lut.exp"],
        l2.f["q"],
        l2.f["k"],
        h["heads"],
        h["kvHeads"],
        h["headDim"],
        rounding="truncate",
    )
    assert np.array_equal(_model(l2, q, k, v), half_up)
    assert not np.array_equal(half_up, floor)
