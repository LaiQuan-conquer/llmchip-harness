"""The MLP gate: tile_silu against GraphL2.silu_gate."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2
from llmchip_rtl.tile import generator

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path("/root/projects/deepseek-harness/tools")))
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"
TB = Path(__file__).with_name("tile_silu_tb.v")
LINEAR_TB = Path(__file__).with_name("tile_slice_tb.v")
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


def _pack(values: np.ndarray) -> str:
    word = 0
    for lane, value in enumerate(values):
        word |= (int(value) & 0xFFFF) << (16 * lane)
    return f"{word:0{len(values) * 4}x}"


def _run(tmp_path, l2, token: int) -> subprocess.CompletedProcess:
    hold: dict = {}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        if layer != 0:
            return
        if node["id"] == "gate_proj":
            hold["h1"] = env["g"].copy()
        if node["id"] == "up_proj":
            hold["h3"] = env["u"].copy()
        if node["id"] == "gate_mul":
            hold["m"] = env["m"].copy()

    l2.forward([token], observe=observe)
    h1 = hold["h1"][0].astype(np.int64)
    h3 = hold["h3"][0].astype(np.int64)
    expected = hold["m"][0].astype(np.int64)
    (tmp_path / "h1.hex").write_text(_pack(h1) + "\n")
    (tmp_path / "h3.hex").write_text(_pack(h3) + "\n")
    (tmp_path / "expected.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in expected) + "\n")
    (tmp_path / "sigmoid.hex").write_text(
        "\n".join(f"{int(v) & 0xFFFF:04x}" for v in l2.a["lut.sigmoid"].ravel()) + "\n"
    )
    shutil.copyfile(TEMPLATES / "tile_silu.v", tmp_path / "tile_silu.v")
    shutil.copyfile(TB, tmp_path / "tile_silu_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_silu_tb",
        f"-GDIM={h1.shape[0]}",
        "-GF1=15",
        "-GF3=15",
        "-GFG=19",
        "tile_silu_tb.v",
        "tile_silu.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run([str(tmp_path / "obj_dir" / "Vtile_silu_tb")], cwd=tmp_path, capture_output=True, text=True)


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


@pytest.mark.parametrize("token", [3, 9])
def test_silu_gate_matches_the_model(l2, tmp_path, token):
    result = _run(tmp_path, l2, token)
    assert "PASS" in result.stdout, result.stdout + result.stderr


def _banks(tmp_path, lines: list[str]) -> int:
    addr_bits = max(2, (len(lines) - 1).bit_length())
    per = 1 << (addr_bits - 2)
    width = len(lines[0])
    for bank in range(4):
        chunk = lines[bank * per : (bank + 1) * per]
        chunk += ["0" * width] * (per - len(chunk))
        (tmp_path / f"rom_bank{bank}.hex").write_text("\n".join(chunk) + "\n")
    return addr_bits


def _run_matrix(tmp_path, l2, key: str, in_node: str, out_node: str, frac_in: int, frac_out: int, token: int):
    _graph, spec, arrays = l2.graph, l2.spec, l2.a
    generator.generate(spec, arrays, macs=8, out=tmp_path)
    lines = (tmp_path / "rom" / f"{key}.hex").read_text().split()
    addr_bits = _banks(tmp_path, lines)
    hold: dict = {}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        if layer == 0 and node["id"] == in_node[0]:
            hold["x"] = env[in_node[1]].copy()
        if layer == 0 and node["id"] == out_node[0]:
            hold["y"] = env[out_node[1]].copy()

    l2.forward([token], observe=observe)
    x = hold["x"][0].astype(np.int64)
    expected = hold["y"][0].astype(np.int64)
    group = int(spec["weights"]["groupSize"])
    m = arrays[f"{key}.m"].astype(np.int64)
    bias = arrays.get(f"{key}.b")
    bias = np.zeros(m.shape[0], dtype=np.int64) if bias is None else bias.astype(np.int64)
    (tmp_path / "x.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in x) + "\n")
    (tmp_path / "expected.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in expected) + "\n")
    (tmp_path / "slice_m.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in m.ravel()) + "\n")
    (tmp_path / "slice_b.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in bias) + "\n")
    for name in ("tile_slice_top.v", "tile_linear.v", "tile_engine.v", "tile_rom.v", "tile_mac.v"):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(LINEAR_TB, tmp_path / "tile_slice_tb.v")
    chunks = (x.shape[0] + 7) // 8
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_slice_tb",
        "-GMACS=8",
        f"-GBITS={spec['weights']['storageBits']}",
        "-GGROUPS=8",
        f"-GROWS={expected.shape[0]}",
        f"-GCHUNKS={chunks}",
        f"-GLOG2CHUNKS={(chunks - 1).bit_length()}",
        f"-GLOG2ROWS={(expected.shape[0] - 1).bit_length()}",
        f"-GLOG2ADDR={addr_bits}",
        "-GLOG2MACS=3",
        f"-GLOG2CPG={(group // 8).bit_length() - 1}",
        f"-GNGROUPS={-(-x.shape[0] // group)}",
        f"-GFRAC_IN={frac_in}",
        f"-GFRAC_OUT={frac_out}",
        f"-GEXP={int(spec['weights']['exponents'][key])}",
        "tile_slice_tb.v",
        "tile_slice_top.v",
        "tile_linear.v",
        "tile_engine.v",
        "tile_rom.v",
        "tile_mac.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run([str(tmp_path / "obj_dir" / "Vtile_slice_tb")], cwd=tmp_path, capture_output=True, text=True)


@pytest.mark.parametrize("token", [3, 9])
def test_gate_proj_matches_the_model(l2, tmp_path, token):
    result = _run_matrix(tmp_path, l2, "0.gate_proj", ("mlp_norm", "h"), ("gate_proj", "g"), 12, 15, token)
    assert "PASS" in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize("token", [3, 9])
def test_up_proj_matches_the_model(l2, tmp_path, token):
    result = _run_matrix(tmp_path, l2, "0.up_proj", ("mlp_norm", "h"), ("up_proj", "u"), 12, 15, token)
    assert "PASS" in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize("token", [3, 9, 25])
def test_down_proj_matches_the_model(l2, tmp_path, token):
    result = _run_matrix(tmp_path, l2, "0.down_proj", ("gate_mul", "m"), ("down_proj", "f"), 19, 18, token)
    assert "PASS" in result.stdout, result.stdout + result.stderr
