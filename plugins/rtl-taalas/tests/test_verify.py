"""The R4 verification report: the plan, the clock-domain check and the not-run reasons."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from llmchip_arch.slicing import slice_spec
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2
from llmchip_rtl.tile import generator, verify

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


def test_the_plan_names_the_runnable_and_blocked_items():
    plan = verify.plan()
    ids = {item["id"] for item in plan["items"]}
    assert {"ver.unit", "ver.slice", "ver.cdc", "ver.layer", "rtl.tile.equivalence"} <= ids
    blocked = {item["id"] for item in plan["items"] if item.get("state") == "blocked"}
    assert not blocked


def test_the_clock_domains_are_clk_and_host_sclk(bundle):
    report = verify.clocks(bundle)
    assert "clk" in report["clocks"]
    assert report["ok"] is True
    assert report["violations"] == 0


def test_the_report_marks_disabled_layer_as_not_run(bundle):
    report = verify.report(
        bundle,
        run_unit=False,
        run_slice=False,
        run_layer=False,
        run_coverage=False,
        run_formal=False,
        run_mutation=False,
        run_dft=False,
        run_equiv=False,
    )
    items = {item["id"]: item for item in report["items"]}
    assert items["ver.cdc"]["status"] == "passed"
    assert items["ver.unit"]["status"] == "not run"
    assert items["ver.slice"]["status"] == "not run"
    assert items["ver.layer"]["status"] == "not run"
    assert "ver.layer" in report["summary"]["notRun"]
    assert report["summary"]["complete"] is False
