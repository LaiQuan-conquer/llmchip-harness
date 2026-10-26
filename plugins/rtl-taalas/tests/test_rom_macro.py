"""The hard-macro view built from `rom_bank0..3.hex` (draft §12.3/§12.4).

The generator writes the macro's black box, its simulation model, a provisional LEF and a manifest;
the test checks the contents, the area arithmetic and — with Yosys — that the simulation model is
equivalent to the behavioural `tile_rom` at the macro's parameters.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from llmchip_arch.slicing import slice_spec
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2
from llmchip_rtl.tile import generator, macro

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path("/root/projects/deepseek-harness/tools")))
YOSYS = TOOLS / "oss-cad-suite" / "bin" / "yosys"
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
    return graph, spec, arrays


def _bundle(built, tmp_path):
    graph, spec, arrays = built
    manifest = generator.generate(spec, arrays, slice_spec(graph, spec, "S-1"), macs=8, out=tmp_path)
    return manifest, tmp_path


def test_the_macro_is_built_from_the_bank_files(built, tmp_path):
    manifest, bundle = _bundle(built, tmp_path)
    entry = manifest["macro"]
    assert entry["bankFiles"] == [f"rom_bank{index}.hex" for index in range(4)]
    assert entry["width"] == manifest["mapping"]["macs"] * manifest["mapping"]["bits"]
    assert entry["depth"] == 4 * entry["bankWords"]
    assert entry["addrBits"] == (entry["depth"] - 1).bit_length()
    assert (bundle / entry["blackbox"]).is_file()
    assert (bundle / entry["simulation"]).is_file()
    assert (bundle / entry["lef"]).is_file()
    assert json.loads((bundle / "rom_macro" / "rom_macro.json").read_text()) == entry


def test_the_black_box_and_the_simulation_model_differ(built, tmp_path):
    entry, bundle = _bundle(built, tmp_path)[0]["macro"], tmp_path
    blackbox = (bundle / entry["blackbox"]).read_text()
    simulation = (bundle / entry["simulation"]).read_text()
    assert "(* blackbox *)" in blackbox
    assert "$readmemh" not in blackbox
    assert "$readmemh" in simulation
    assert "rom_bank0.hex" in simulation


def test_the_lef_is_a_provisional_estimate(built, tmp_path):
    entry = _bundle(built, tmp_path)[0]["macro"]
    text = (tmp_path / entry["lef"]).read_text()
    assert "PROVISIONAL" in text
    assert "MACRO tile_rom_macro" in text
    assert "SIZE " in text
    assert "PIN addr[0]" in text and "PIN q[0]" in text
    assert "PIN VDD" in text and "PIN VSS" in text
    expected = entry["width"] * entry["depth"] * macro.DENSITY_UM2_PER_BIT
    assert entry["areaUm2"]["estimate"] == pytest.approx(expected, rel=0.01)
    assert entry["hardening"].startswith("not run")


@pytest.mark.skipif(not YOSYS.is_file(), reason="Yosys is not in the tools directory")
def test_the_simulation_view_equals_tile_rom(built, tmp_path):
    entry = _bundle(built, tmp_path)[0]["macro"]
    report = macro.equivalent(tmp_path, YOSYS)
    assert report["equivalent"] is True, report["output"]
    assert entry["latency"].startswith("combinational")
