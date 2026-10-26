"""`tile_attention` with the real `tile_kv` caches against GraphL2.attention.

The K rows are the post-RoPE keys and the V rows are the v_proj values of positions 0..p, loaded
through the cache write ports; the query is the post-RoPE q of position p.
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

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path("/root/projects/deepseek-harness/tools")))
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"
TB = Path(__file__).with_name("tile_attention_kv_tb.v")
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


def _run(tmp_path, l2, position: int, tokens: list[int]) -> subprocess.CompletedProcess:
    hold: dict = {}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        if layer != 0:
            return
        if node["id"] == "rope":
            hold["q"] = env["q"].copy()
            hold["k"] = env["k"].copy()
        if node["id"] == "v_proj":
            hold["v"] = env["v"].copy()
        if node["id"] == "attention":
            hold["a"] = env["a"].copy()

    l2.forward(tokens[: position + 1], observe=observe)
    heads, kv_heads, hd = l2.spec["hyper"]["heads"], l2.spec["hyper"]["kvHeads"], l2.spec["hyper"]["headDim"]
    context = l2.context
    log2context = (context - 1).bit_length()
    addr = (kv_heads * context - 1).bit_length()
    depth = 1 << addr
    q = hold["q"][-1].astype(np.int64)
    k = hold["k"].astype(np.int64)
    v = hold["v"].astype(np.int64)
    expected = hold["a"][-1].astype(np.int64)
    k_rows = [np.zeros(hd, dtype=np.int64) for _ in range(depth)]
    v_rows = [np.zeros(hd, dtype=np.int64) for _ in range(depth)]
    for kv_head in range(kv_heads):
        for pos in range(position + 1):
            row = (kv_head << log2context) + pos
            k_rows[row] = k[pos, kv_head * hd : (kv_head + 1) * hd]
            v_rows[row] = v[pos, kv_head * hd : (kv_head + 1) * hd]
    (tmp_path / "q.hex").write_text(_pack(q) + "\n")
    (tmp_path / "k_cache.hex").write_text("\n".join(_pack(row) for row in k_rows) + "\n")
    (tmp_path / "v_cache.hex").write_text("\n".join(_pack(row) for row in v_rows) + "\n")
    (tmp_path / "expected.hex").write_text("\n".join(f"{int(value) & 0xFFFF:04x}" for value in expected) + "\n")
    (tmp_path / "lut.exp.hex").write_text(
        "\n".join(f"{int(value) & 0xFFFF:04x}" for value in l2.a["lut.exp"].ravel()) + "\n"
    )
    for name in ("tile_attention.v", "tile_kv.v", "tile_lut.v"):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(TB, tmp_path / "tile_attention_kv_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_attention_kv_tb",
        f"-GHEADS={heads}",
        f"-GKVHEADS={kv_heads}",
        f"-GHD={hd}",
        f"-GLOG2CONTEXT={log2context}",
        f"-GADDR={addr}",
        f"-GLENGTH={position}",
        "-GFQ=13",
        "-GFK=13",
        "tile_attention_kv_tb.v",
        "tile_attention.v",
        "tile_kv.v",
        "tile_lut.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run(
        [str(tmp_path / "obj_dir" / "Vtile_attention_kv_tb")], cwd=tmp_path, capture_output=True, text=True
    )


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


@pytest.mark.parametrize("position", [0, 1, 3, 7, 15])
def test_attention_over_the_kv_cache_matches_the_model(l2, tmp_path, position):
    tokens = [3, 9, 17, 25, 33, 41, 49, 57, 11, 13, 19, 23, 29, 31, 37, 43]
    result = _run(tmp_path, l2, position, tokens)
    assert "PASS" in result.stdout, result.stdout + result.stderr


def _run_behavioral(tmp_path, l2, position: int, tokens: list[int]) -> subprocess.CompletedProcess:
    hold: dict = {}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        if layer != 0:
            return
        if node["id"] == "rope":
            hold["q"] = env["q"].copy()
            hold["k"] = env["k"].copy()
        if node["id"] == "v_proj":
            hold["v"] = env["v"].copy()
        if node["id"] == "attention":
            hold["a"] = env["a"].copy()

    l2.forward(tokens[: position + 1], observe=observe)
    heads, kv_heads, hd = l2.spec["hyper"]["heads"], l2.spec["hyper"]["kvHeads"], l2.spec["hyper"]["headDim"]
    context = l2.context
    log2context = (context - 1).bit_length()
    addr = (kv_heads * context - 1).bit_length()
    depth = 1 << addr
    q = hold["q"][-1].astype(np.int64)
    k = hold["k"].astype(np.int64)
    v = hold["v"].astype(np.int64)
    expected = hold["a"][-1].astype(np.int64)
    k_rows = [np.zeros(hd, dtype=np.int64) for _ in range(depth)]
    v_rows = [np.zeros(hd, dtype=np.int64) for _ in range(depth)]
    for kv_head in range(kv_heads):
        for pos in range(position + 1):
            row = (kv_head << log2context) + pos
            k_rows[row] = k[pos, kv_head * hd : (kv_head + 1) * hd]
            v_rows[row] = v[pos, kv_head * hd : (kv_head + 1) * hd]
    (tmp_path / "q.hex").write_text(_pack(q) + "\n")
    (tmp_path / "k.hex").write_text("\n".join(_pack(row) for row in k_rows) + "\n")
    (tmp_path / "v.hex").write_text("\n".join(_pack(row) for row in v_rows) + "\n")
    (tmp_path / "expected.hex").write_text("\n".join(f"{int(value) & 0xFFFF:04x}" for value in expected) + "\n")
    (tmp_path / "lut.exp.hex").write_text(
        "\n".join(f"{int(value) & 0xFFFF:04x}" for value in l2.a["lut.exp"].ravel()) + "\n"
    )
    for name in ("tile_attention.v", "tile_lut.v"):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(Path(__file__).with_name("tile_attention_tb.v"), tmp_path / "tile_attention_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_attention_tb",
        f"-GHEADS={heads}",
        f"-GKVHEADS={kv_heads}",
        f"-GHD={hd}",
        f"-GLOG2CONTEXT={log2context}",
        f"-GADDR={addr}",
        f"-GS={position + 1}",
        "-GFQ=13",
        "-GFK=13",
        "tile_attention_tb.v",
        "tile_attention.v",
        "tile_lut.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run(
        [str(tmp_path / "obj_dir" / "Vtile_attention_tb")], cwd=tmp_path, capture_output=True, text=True
    )


def test_behavioral_kv_matches_the_model(l2, tmp_path):
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    result = _run_behavioral(tmp_path, l2, 1, tokens)
    assert "PASS" in result.stdout, result.stdout + result.stderr
