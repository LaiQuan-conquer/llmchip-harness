"""Diagnosis of answers (FR-QNT-09): the L2's observation points against the float graph's."""

import json
from pathlib import Path

import numpy as np
import pytest
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover

torch = pytest.importorskip("torch")
from llmchip_numeric import diagnose, graph_l2  # noqa: E402
from llmchip_numeric.l2_torch import TorchL2  # noqa: E402

TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"


@pytest.fixture(scope="module")
def tiny():
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    stats = graph_l2.calibrate(graph, loader.get, [tokens])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=64)
    return graph, loader, graph_l2.GraphL2(graph, spec, arrays), tokens


def test_the_l2s_points_are_the_float_graphs(tiny):
    graph, loader, ref, tokens = tiny
    float_points = diagnose._points_float(graph, loader.get, tokens)
    l2_points = diagnose._points_l2(TorchL2(ref), tokens)
    assert len(float_points) == len(l2_points) == 1 + graph["nodes"]["repeat"] + 1
    for a, b in zip(float_points, l2_points, strict=True):
        assert a.shape == b.shape
        assert np.linalg.norm(a - b) / (np.linalg.norm(a) or 1.0) < 0.2


def test_a_diagnosis_names_the_first_divergence_and_its_layer(tiny):
    graph, loader, ref, tokens = tiny
    report = diagnose.diagnose_answer(graph, loader.get, TorchL2(ref), tokens, steps=4)
    assert set(report) >= {"tokens", "l0", "l2", "firstDivergence"}
    assert report["firstDivergence"] >= -1
    if report["firstDivergence"] >= 0:
        assert len(report["layers"]) == 1 + graph["nodes"]["repeat"] + 1
        assert report["worst"]["matrices"]
