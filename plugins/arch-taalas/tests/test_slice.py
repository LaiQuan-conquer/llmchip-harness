"""The slice of a decoder layer: group-aligned parts and the L2 observation points they give."""

import json
from pathlib import Path

import pytest
from llmchip_arch.slicing import slice_spec
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2

TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"


@pytest.fixture(scope="module")
def tiny():
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    stats = graph_l2.calibrate(graph, loader.get, [tokens])
    spec, _ = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=64)
    return graph, spec


def test_the_levels_of_the_slice(tiny):
    graph, spec = tiny
    s1 = slice_spec(graph, spec, "S-1")
    assert [m["node"] for m in s1["matrices"]] == ["q_proj"]
    assert s1["total"]["weights"] == 8 * 32  # one head of 8 rows over 32 input columns
    assert s1["total"]["bits"] == 4 * s1["total"]["weights"]
    assert s1["observationPoints"] == ["q"]
    s2 = slice_spec(graph, spec, "S-2")
    assert [m["node"] for m in s2["matrices"]] == ["q_proj", "k_proj", "v_proj", "o_proj"]
    assert s2["total"]["weights"] == 256 + 256 + 256 + 32 * 16
    s3 = slice_spec(graph, spec, "S-3", channels=16)
    assert s3["total"]["weights"] == s2["total"]["weights"] + 3 * 16 * 32
    assert s3["observationPoints"] == ["q", "k", "v", "attn", "h1", "h3", "g"]


def test_a_slice_whose_columns_do_not_fill_groups_is_refused(tiny):
    graph, spec = tiny
    spec = json.loads(json.dumps(spec))
    spec["weights"]["groupSize"] = 128  # the tiny model has 4 heads of 8: no 128-column range
    with pytest.raises(ValueError, match="not aligned to groups"):
        slice_spec(graph, spec, "S-2")
