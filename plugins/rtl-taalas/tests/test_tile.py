"""The taalas-tile/1 RTL bundle: the ROM mapping round trip, the generated slice, lint, synthesis.

The ROM mapping is the versioned rule of FR-RTL-02: forward (weights to words) then inverse
restores the quantized levels exactly. The generator writes the hand-written templates plus the
behavioural ROM contents (view 1 of FR-SGN-07) and a manifest with every hash; the tests lint the
bundle with Verilator -Wall and elaborate it with Yosys (skipped, visibly, without the tools).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from llmchip_arch.slicing import slice_spec
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2
from llmchip_rtl.tile import generator, rom

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path.home() / "projects" / "deepseek-harness" / "tools"))
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
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


def test_the_rom_mapping_round_trips():
    rng = np.random.default_rng(0)
    for bits in (4, 8):
        qmax = (1 << (bits - 1)) - 1
        for rows, columns, macs in ((8, 4, 8), (13, 6, 8), (32, 8, 16), (5, 3, 4)):
            q = rng.integers(-qmax - 1, qmax + 1, size=(rows, columns)).astype(np.int64)
            assert rom.roundtrip(q, macs, bits), (bits, rows, columns, macs)
            words = rom.to_words(q, macs, bits)
            assert len(words) == rom.words_per_matrix(rows, columns, macs)
            assert np.array_equal(rom.from_words(words, rows, columns, macs, bits), q)
            lines = rom.hexfile(words, macs, bits)
            assert all(len(line) == -(-macs * bits // 4) for line in lines)


def test_the_generated_slice_writes_its_rom_and_manifest(built, tmp_path):
    graph, spec, arrays = built
    report = slice_spec(graph, spec, "S-1")
    manifest = generator.generate(spec, arrays, report, macs=8, out=tmp_path)
    assert manifest["architecture"] == "taalas-tile/1"
    assert manifest["mapping"]["rule"] == rom.MAPPING_VERSION
    assert [bank["matrix"] for bank in manifest["banks"]] == [
        f"{report['layer']}.{entry['node']}" for entry in report["matrices"]
    ]
    bank = manifest["banks"][0]
    assert bank["restored"] is True
    lines = (tmp_path / bank["hex"]).read_text().split()
    assert len(lines) == bank["words"]
    assert (tmp_path / "rtl" / "tile_top.v").is_file()
    assert len(manifest["files"]) == 3


@pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not installed in the tools directory")
def test_the_generated_rtl_lints_clean(built, tmp_path):
    _, spec, arrays = built
    graph = built[0]
    manifest = generator.generate(spec, arrays, slice_spec(graph, spec, "S-1"), macs=8, out=tmp_path)
    rtl = tmp_path / "rtl"
    result = subprocess.run(
        [
            str(VERILATOR),
            "--lint-only",
            "-Wall",
            "--top-module",
            "tile_top",
            str(rtl / "tile_rom.v"),
            str(rtl / "tile_mac.v"),
            str(rtl / "tile_top.v"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Warning" not in result.stdout + result.stderr
    assert manifest["files"]["rtl/tile_top.v"]


@pytest.mark.skipif(not YOSYS.is_file(), reason="Yosys is not installed in the tools directory")
def test_the_generated_rtl_elaborates_in_yosys(built, tmp_path):
    _, spec, arrays = built
    graph = built[0]
    manifest = generator.generate(spec, arrays, slice_spec(graph, spec, "S-1"), macs=8, out=tmp_path)
    rtl = tmp_path / "rtl"
    shutil.copyfile(tmp_path / manifest["banks"][0]["hex"], tmp_path / "rom.hex")
    script = "; ".join(
        [
            f"read_verilog {rtl / 'tile_rom.v'} {rtl / 'tile_mac.v'} {rtl / 'tile_top.v'}",
            "chparam -set ADDR 6 -set MACS 8 -set BITS 4 tile_top",
            "hierarchy -check -top tile_top",
            "check -assert",
            "proc",
            "opt",
            "check -assert",
        ]
    )
    result = subprocess.run([str(YOSYS), "-q", "-p", script], capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
