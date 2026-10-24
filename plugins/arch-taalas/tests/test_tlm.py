"""The transaction-level model: the schedule's phases and the L2 observation points they produce."""

import json
from pathlib import Path

import pytest
from llmchip_arch import tile, tlm
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2

TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"


@pytest.fixture(scope="module")
def built():
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    stats = graph_l2.calibrate(graph, loader.get, [tokens])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=16)
    chip = tile.design(spec, {"macsPerEngine": 8, "engines": "one"})
    return chip, graph, spec, arrays


def test_the_tlm_covers_the_schedule_and_matches_l2(built):
    chip, graph, spec, arrays = built
    report = tlm.trace(chip, graph, spec, arrays, [3, 9, 17, 25, 33])
    assert report["pointCount"] > 0
    assert report["missingPoints"] == []
    assert report["mismatches"] == 0
    assert report["deterministic"] is True
    assert report["cyclesAgree"] is True
    assert report["cycles"] == chip["performance"]["cyclesPerToken"]


def test_a_chip_whose_cycles_disagree_is_caught(built):
    chip, graph, spec, arrays = built
    chip = json.loads(json.dumps(chip))
    chip["performance"]["cyclesPerToken"] += 1
    report = tlm.trace(chip, graph, spec, arrays, [3, 9, 17])
    assert report["cyclesAgree"] is False
