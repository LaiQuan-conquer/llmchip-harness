"""`tile_rmsnorm` against GraphL2.rmsnorm: attn_norm (DIM=32) and the per-head q_norm (DIM=8)."""

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

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path("/root/projects/deepseek-harness/tools")))
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"
TB = Path(__file__).with_name("tile_rmsnorm_tb.v")
ROPE_TB = Path(__file__).with_name("tile_rope_tb.v")
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


def _run(
    tmp_path, l2, kind: str, f_in: int, f_out: int, eps_q: int, dim: int, log2dim: int
) -> subprocess.CompletedProcess:
    hold: dict = {}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        if layer == 0 and node["id"] == "q_proj":
            hold["q_proj"] = env["q"].copy()
        if layer == 0 and node["id"] == kind:
            if node["id"] == "attn_norm":
                hold["x"] = env[node["inputs"][0]].copy()
            hold["y"] = env[node["outputs"][0]].copy()

    l2.forward([3], observe=observe)
    x = (hold["q_proj"] if kind == "q_norm" else hold["x"])[0].astype(np.int64)[:dim]
    expected = hold["y"][0].astype(np.int64)
    weight = l2.a[f"0.{kind}.w" if kind != "attn_norm" else "0.attn_norm.w"].astype(np.int64)
    (tmp_path / "x.hex").write_text(_pack(x) + "\n")
    (tmp_path / "weight.hex").write_text(_pack(weight) + "\n")
    (tmp_path / "expected.hex").write_text("\n".join(f"{int(value) & 0xFFFF:04x}" for value in expected[:dim]) + "\n")
    (tmp_path / "rsqrt.hex").write_text(
        "\n".join(f"{int(value) & 0xFFFF:04x}" for value in l2.a["lut.rsqrt"].ravel()) + "\n"
    )
    for name in ("tile_rmsnorm.v", "tile_lut.v"):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(TB, tmp_path / "tile_rmsnorm_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_rmsnorm_tb",
        f"-GDIM={dim}",
        f"-GLOG2DIM={log2dim}",
        "-GFN=15",
        f"-GFO={f_out}",
        f"-GEPS_Q={eps_q}",
        "tile_rmsnorm_tb.v",
        "tile_rmsnorm.v",
        "tile_lut.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run(
        [str(tmp_path / "obj_dir" / "Vtile_rmsnorm_tb")], cwd=tmp_path, capture_output=True, text=True
    )


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


def test_attn_norm_matches_the_model(l2, tmp_path):
    result = _run(tmp_path, l2, "attn_norm", f_in=18, f_out=12, eps_q=round(1e-6 * 2**36), dim=32, log2dim=5)
    assert "PASS" in result.stdout, result.stdout + result.stderr


def test_the_head_q_norm_matches_the_model(l2, tmp_path):
    result = _run(tmp_path, l2, "q_norm", f_in=15, f_out=13, eps_q=round(1e-6 * 2**30), dim=8, log2dim=3)
    assert "PASS" in result.stdout, result.stdout + result.stderr


def _run_rope(tmp_path, l2, position: int, tokens: list[int]) -> subprocess.CompletedProcess:
    hold: dict = {}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        if layer == 0 and node["id"] == "q_norm":
            hold["q"] = env["q"].copy()
        if layer == 0 and node["id"] == "k_norm":
            hold["k"] = env["k"].copy()

    l2.forward(tokens[: position + 1], observe=observe)
    heads, kv_heads, hd = l2.spec["hyper"]["heads"], l2.spec["hyper"]["kvHeads"], l2.spec["hyper"]["headDim"]
    q = hold["q"][-1].astype(np.int64)
    k = hold["k"][-1].astype(np.int64)
    expected_q = l2.rope(q[None, :], heads, hd, "half", "q", position)[0]
    expected_k = l2.rope(k[None, :], kv_heads, hd, "half", "k", position)[0]
    (tmp_path / "q.hex").write_text(_pack(q) + "\n")
    (tmp_path / "k.hex").write_text(_pack(k) + "\n")
    (tmp_path / "expected_q.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in expected_q) + "\n")
    (tmp_path / "expected_k.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in expected_k) + "\n")
    (tmp_path / "rope_cos.hex").write_text(
        "\n".join(f"{int(v) & 0xFFFF:04x}" for v in l2.a["lut.rope_cos"].ravel()) + "\n"
    )
    (tmp_path / "rope_sin.hex").write_text(
        "\n".join(f"{int(v) & 0xFFFF:04x}" for v in l2.a["lut.rope_sin"].ravel()) + "\n"
    )
    for name in ("tile_rope.v", "tile_lut.v"):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(ROPE_TB, tmp_path / "tile_rope_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_rope_tb",
        f"-GQHEADS={heads}",
        f"-GKHEADS={kv_heads}",
        f"-GHD={hd}",
        f"-GLOG2HD={(hd - 1).bit_length()}",
        "-GSTYLE=0",
        f"-GADDR={(len(l2.a['lut.rope_cos'].ravel()) - 1).bit_length()}",
        f"-GPOSITION={position}",
        "tile_rope_tb.v",
        "tile_rope.v",
        "tile_lut.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run([str(tmp_path / "obj_dir" / "Vtile_rope_tb")], cwd=tmp_path, capture_output=True, text=True)


@pytest.mark.parametrize("position", [0, 1, 5, 15])
def test_rope_matches_the_model(l2, tmp_path, position):
    tokens = [3, 9, 17, 25, 33, 41, 49, 57, 11, 13, 19, 23, 29, 31, 37, 43]
    result = _run_rope(tmp_path, l2, position, tokens)
    assert "PASS" in result.stdout, result.stdout + result.stderr
