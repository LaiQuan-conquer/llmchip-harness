"""The attention-half linear stages against GraphL2: o_proj and the residual add.

`tile_slice_top` is reused as the generic bit-exact linear unit; the matrix, banks, mantissas and
biases are the generated o_proj ones. The residual test compares the saturated int16 add with
GraphL2's `residual_attn`.
"""

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
TB = Path(__file__).with_name("tile_slice_tb.v")
RESIDUAL_TB = Path(__file__).with_name("tile_residual_tb.v")
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


def _pack(values: np.ndarray) -> str:
    word = 0
    for lane, value in enumerate(values):
        word |= (int(value) & 0xFFFF) << (16 * lane)
    return f"{word:0{len(values) * 4}x}"


def _banks(tmp_path, lines: list[str]) -> int:
    addr_bits = max(2, (len(lines) - 1).bit_length())
    per = 1 << (addr_bits - 2)
    width = len(lines[0])
    for bank in range(4):
        chunk = lines[bank * per : (bank + 1) * per]
        chunk += ["0" * width] * (per - len(chunk))
        (tmp_path / f"rom_bank{bank}.hex").write_text("\n".join(chunk) + "\n")
    return addr_bits


def _run_linear(tmp_path, built, key: str, token: int) -> subprocess.CompletedProcess:
    graph, spec, arrays = built
    manifest = generator.generate(spec, arrays, macs=8, out=tmp_path)
    lines = (tmp_path / "rom" / f"{key}.hex").read_text().split()
    addr_bits = _banks(tmp_path, lines)
    l2 = graph_l2.GraphL2(graph, spec, arrays)
    hold: dict = {}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        if layer == 0 and node["id"] == "attention":
            hold["x"] = env[node["outputs"][0]].copy()
        if layer == 0 and node["id"] == "o_proj":
            hold["y"] = env[node["outputs"][0]].copy()

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
    shutil.copyfile(TB, tmp_path / "tile_slice_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_slice_tb",
        "-GMACS=8",
        f"-GBITS={manifest['mapping']['bits']}",
        "-GGROUPS=8",
        f"-GROWS={expected.shape[0]}",
        f"-GCHUNKS={(x.shape[0] + 7) // 8}",
        f"-GLOG2CHUNKS={((x.shape[0] + 7) // 8 - 1).bit_length()}",
        f"-GLOG2ROWS={(expected.shape[0] - 1).bit_length()}",
        f"-GLOG2ADDR={addr_bits}",
        "-GLOG2MACS=3",
        f"-GLOG2CPG={(group // 8).bit_length() - 1}",
        f"-GNGROUPS={-(-x.shape[0] // group)}",
        "-GFRAC_IN=15",
        "-GFRAC_OUT=18",
        "tile_slice_tb.v",
        "tile_slice_top.v",
        "tile_linear.v",
        "tile_engine.v",
        "tile_rom.v",
        "tile_mac.v",
    ]
    # The matrix exponent is the spec's, not the slice's layer number.
    command = [item for item in command if not item.startswith("-GEXP=")] + [
        f"-GEXP={int(spec['weights']['exponents'][key])}"
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run([str(tmp_path / "obj_dir" / "Vtile_slice_tb")], cwd=tmp_path, capture_output=True, text=True)


def _run_residual(tmp_path, built, token: int) -> subprocess.CompletedProcess:
    graph, spec, arrays = built
    l2 = graph_l2.GraphL2(graph, spec, arrays)
    hold: dict = {}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        if layer == 0 and node["id"] == "o_proj":
            hold["a"] = env["x"].copy()
            hold["b"] = env["o"].copy()
        if layer == 0 and node["id"] == "residual_attn":
            hold["y"] = env[node["outputs"][0]].copy()

    l2.forward([token], observe=observe)
    a, b, y = (hold[name][0].astype(np.int64) for name in ("a", "b", "y"))
    (tmp_path / "a.hex").write_text(_pack(a) + "\n")
    (tmp_path / "b.hex").write_text(_pack(b) + "\n")
    (tmp_path / "expected.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in y) + "\n")
    shutil.copyfile(TEMPLATES / "tile_residual.v", tmp_path / "tile_residual.v")
    shutil.copyfile(RESIDUAL_TB, tmp_path / "tile_residual_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_residual_tb",
        f"-GDIM={a.shape[0]}",
        "-GWIDTH=16",
        "tile_residual_tb.v",
        "tile_residual.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run(
        [str(tmp_path / "obj_dir" / "Vtile_residual_tb")], cwd=tmp_path, capture_output=True, text=True
    )


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


@pytest.mark.parametrize("token", [3, 9])
def test_o_proj_matches_the_model(built, tmp_path, token):
    result = _run_linear(tmp_path, built, "0.o_proj", token)
    assert "PASS" in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize("token", [3, 9])
def test_the_residual_add_matches_the_model(built, tmp_path, token):
    result = _run_residual(tmp_path, built, token)
    assert "PASS" in result.stdout, result.stdout + result.stderr
