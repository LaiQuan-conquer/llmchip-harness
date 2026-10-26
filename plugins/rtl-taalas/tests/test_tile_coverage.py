"""Line coverage of the chained Q-full layer harness (Verilator --coverage)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tile_layer import VERILATOR, _run

TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"


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


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


def test_the_layer_harness_has_line_coverage(l2, tmp_path):
    result = _run(tmp_path, l2, 3, [3, 9, 17, 25, 33, 41, 49, 57], 0, coverage=True)
    text = getattr(result, "coverage_text", "")
    line = re.search(r"line\s*:\s*([0-9.]+)%", text)
    assert line is not None, result.stdout + result.stderr + text
    value = float(line.group(1))
    print(f"COVERAGE line={value:.1f}", file=sys.__stdout__)
    assert value >= 90.0, text
