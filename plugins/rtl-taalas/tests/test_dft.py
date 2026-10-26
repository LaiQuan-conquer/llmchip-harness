"""DFT readiness audit and its negative controls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from llmchip_arch.slicing import slice_spec
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2
from llmchip_rtl.tile import dft, generator

TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    stats = graph_l2.calibrate(graph, loader.get, [tokens])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=16)
    out = tmp_path_factory.mktemp("bundle")
    generator.generate(spec, arrays, slice_spec(graph, spec, "S-1"), macs=8, out=out)
    return out


def test_the_bundle_is_dft_ready(bundle):
    report = dft.audit(bundle)
    assert report["ok"] is True
    assert report["issues"] == []
    assert report["ffs"] > 0
    assert report["clocks"] == ["clk", "host_sclk"]


def test_a_gated_clock_is_rejected():
    issues = dft._issues_for_text("bad.v", "always @(posedge gclk) q <= d;")
    assert any(issue["kind"] == "gated-clock" for issue in issues)


def test_a_latch_is_rejected():
    issues = dft._issues_for_text("bad.v", "always @* q <= d;")
    assert any(issue["kind"] == "latch" for issue in issues)


def test_a_non_rom_initial_is_rejected():
    issues = dft._issues_for_text("bad.v", "initial q = 1'b0;")
    assert any(issue["kind"] == "initial-not-rom" for issue in issues)


def test_a_missing_reset_is_rejected():
    issues = dft._issues_for_text("bad.v", "always @(posedge clk) q <= d;")
    assert any(issue["kind"] == "missing-reset" for issue in issues)
