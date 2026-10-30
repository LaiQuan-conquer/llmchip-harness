"""Directed tests of the layer schedule (drafts/Qwen05B_Taalas_Refactor_Plan_zh.md §12.1 B2).

Power-on, a start while busy, a reset in the middle of inference, a single token and the context
boundary. The harness is `tile_layer_directed_tb.v`; every case compares the last position's
observations with GraphL2, so a reset that leaves the datapath in a bad state is caught.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tile_layer import TINY_QWEN3, _run

TOKENS = [3, 9, 17, 25, 33, 41, 49, 57, 65, 73, 81, 89, 97, 105, 113, 121]


@pytest.fixture(scope="module")
def l2():
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    stats = graph_l2.calibrate(graph, loader.get, [tokens])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=16)
    return graph_l2.GraphL2(graph, spec, arrays)


@pytest.mark.parametrize(
    "steps,midreset,label",
    [
        (1, False, "single token"),
        (4, True, "reset in the middle of inference"),
        (16, False, "context boundary"),
    ],
)
def test_directed_schedule_cases(l2, tmp_path, steps, midreset, label):
    result = _run(tmp_path, l2, steps - 1, TOKENS, 0, steps=steps, midreset=midreset)
    assert "PASS" in result.stdout, label + "\n" + result.stdout + result.stderr
