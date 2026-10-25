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
from llmchip_rtl.tile import checks, generator, rom, views

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


def _graph(built):
    return built[0]


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


def test_the_generated_bundle_carries_the_lookup_tables(built, tmp_path):
    _, spec, arrays = built
    manifest = generator.generate(spec, arrays, slice_spec(_graph(built), spec, "S-1"), macs=8, out=tmp_path)
    assert set(manifest["luts"]) >= {"rsqrt", "exp", "sigmoid"}
    for name, entry in manifest["luts"].items():
        lines = (tmp_path / entry["hex"]).read_text().split()
        assert len(lines) == entry["entries"] == int(arrays[f"lut.{name}"].size)
        assert all(len(line) in (4, 8) for line in lines)
    assert (tmp_path / "rtl" / "tile_lut.v").is_file()
    assert (tmp_path / "rtl" / "tile_rmsnorm.v").is_file()
    assert (tmp_path / "rsqrt.hex").is_file()
    assert (tmp_path / "rtl" / "tile_rope.v").is_file()
    assert (tmp_path / "rtl" / "tile_kv.v").is_file()


def test_the_full_model_writes_one_bank_per_matrix_and_the_classifier(built, tmp_path):
    _, spec, arrays = built
    manifest = generator.generate(spec, arrays, macs=8, out=tmp_path)
    assert manifest["slice"]["level"] == "Q-full"
    assert len(manifest["banks"]) == 7 * spec["hyper"]["layers"] + 1
    assert manifest["banks"][-1]["matrix"] == "embed"
    assert manifest["banks"][-1]["rows"] == spec["hyper"]["vocab"]
    assert all(bank["restored"] for bank in manifest["banks"])
    weights = sum(bank["rows"] * bank["columns"] for bank in manifest["banks"])
    assert manifest["slice"]["weights"] == weights


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
    assert len(manifest["files"]) == 7


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


def test_the_bundle_passes_the_rtl_checks(built, tmp_path):
    graph, spec, arrays = built
    generator.generate(spec, arrays, slice_spec(graph, spec, "S-1"), macs=8, out=tmp_path)
    report = checks.all_checks(tmp_path, VERILATOR if VERILATOR.is_file() else None, YOSYS if YOSYS.is_file() else None)
    assert report["roundtrip"]["roundtrip_mismatches"] == 0
    assert report["rules"]["violations"] == 0
    if "warnings" in report["lint"]:
        assert report["lint"]["warnings"] == 0, report["lint"]["output"]
    if "returncode" in report["synthesizable"]:
        assert report["synthesizable"]["returncode"] == 0, report["synthesizable"]["output"]


def test_a_corrupted_bank_fails_the_roundtrip(built, tmp_path):
    graph, spec, arrays = built
    manifest = generator.generate(spec, arrays, slice_spec(graph, spec, "S-1"), macs=8, out=tmp_path)
    bank = manifest["banks"][0]
    path = tmp_path / bank["hex"]
    lines = path.read_text().split()
    lines[-1] = "0" * len(lines[-1])
    path.write_text("\n".join(lines) + "\n")
    assert checks.roundtrip(tmp_path)["roundtrip_mismatches"] == 1


def test_a_rule_violation_is_reported(built, tmp_path):
    graph, spec, arrays = built
    generator.generate(spec, arrays, slice_spec(graph, spec, "S-1"), macs=8, out=tmp_path)
    top = tmp_path / "rtl" / "tile_top.v"
    top.write_text(top.read_text() + "\n/* verilator lint_off UNUSED */\n")
    report = checks.rules(tmp_path)
    assert report["violations"] >= 1


@pytest.mark.skipif(not YOSYS.is_file(), reason="Yosys is not installed in the tools directory")
def test_the_two_rom_views_are_equivalent(built, tmp_path):
    graph, spec, arrays = built
    generator.generate(spec, arrays, slice_spec(graph, spec, "S-1"), macs=8, out=tmp_path)
    report = views.rom_views(tmp_path, YOSYS)
    assert report["equivalent"] is True, report["output"]


@pytest.mark.skipif(not YOSYS.is_file(), reason="Yosys is not installed in the tools directory")
def test_a_view_from_another_bundle_is_not_equivalent(built, tmp_path):
    graph, spec, arrays = built
    first, second = tmp_path / "a", tmp_path / "b"
    generator.generate(spec, arrays, slice_spec(graph, spec, "S-1"), macs=8, out=first)
    other = {key: value.copy() for key, value in arrays.items()}
    other["0.q_proj.q"] = -other["0.q_proj.q"]
    generator.generate(spec, other, slice_spec(graph, spec, "S-1"), macs=8, out=second)
    assert views.synthesize_view2(first, YOSYS)["returncode"] == 0
    shutil.copyfile(first / views.VIEW2_FILE, second / views.VIEW2_FILE)
    assert views.equivalent(second, YOSYS)["equivalent"] is False
