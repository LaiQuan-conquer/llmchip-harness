"""The `taalas-tile/1` design: the schedule table is the cycle model, and the spec verifies."""

import json
from pathlib import Path

import pytest
from llmchip_arch import tile
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2

TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"


@pytest.fixture(scope="module")
def spec():
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    stats = graph_l2.calibrate(graph, loader.get, [tokens])
    spec, _ = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=16)
    return spec


def test_the_schedule_is_the_cycle_model(spec):
    chip = tile.design(spec, {"macsPerEngine": 8, "clockMHz": 8})
    assert chip["architecture"] == "taalas-tile/1"
    assert chip["dataflow"]["engines"] == 2  # one engine per decoder layer
    assert chip["dataflow"]["romWordBits"] == 8 * 4
    schedule = chip["schedule"]
    per_layer = sum(p["cycles"] for p in schedule["rows"][0]["phases"])
    assert (
        chip["performance"]["cyclesPerToken"]
        == max(sum(p["cycles"] for p in row["phases"]) for row in schedule["rows"]) + schedule["hostCycles"]
    )
    assert chip["performance"]["tokensPerSecond"] == pytest.approx(
        8e6 / chip["performance"]["cyclesPerToken"], rel=1e-6
    )
    assert per_layer > 0
    checked = tile.verify(chip)
    assert checked["schedule_agrees"] is True
    assert checked["cycles_recomputed"] == chip["performance"]["cyclesPerToken"]


def test_a_slice_has_one_engine(spec):
    chip = tile.design(spec, {"macsPerEngine": 8, "engines": "one"})
    assert chip["dataflow"]["engines"] == 1
    per_layer = sum(p["cycles"] for p in chip["schedule"]["rows"][0]["phases"])
    assert chip["performance"]["cyclesPerToken"] == per_layer + chip["schedule"]["hostCycles"]


def test_macs_that_do_not_divide_the_shapes_are_refused(spec):
    with pytest.raises(ValueError, match="do not divide"):
        tile.design(spec, {"macsPerEngine": 7})


def test_a_tampered_schedule_is_caught(spec):
    chip = tile.design(spec, {"macsPerEngine": 8})
    chip["schedule"]["rows"][0]["phases"][0]["cycles"] += 1
    assert tile.verify(chip)["schedule_agrees"] is False
