"""The two Q-full layers, chained and compared with GraphL2 at every observation point.

The harness wires attn_norm, q/k/v, per-head q_norm/k_norm, RoPE, the two KV caches, attention,
o_proj, residual, mlp_norm, gate/up, SiLU, down_proj and the second residual. For each layer the
weights, the input residual and all 17 expected vectors are written from GraphL2.
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
VERILATOR_COVERAGE = TOOLS / "oss-cad-suite" / "bin" / "verilator_coverage"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"
TB = Path(__file__).with_name("tile_layer_attn_tb.v")
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


def _write_words(tmp_path, name: str, values: np.ndarray) -> None:
    (tmp_path / name).write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in values) + "\n")


def _split(tmp_path, key: str, prefix: str) -> int:
    lines = (tmp_path / "rom" / f"{key}.hex").read_text().split()
    addr_bits = max(2, (len(lines) - 1).bit_length())
    per = 1 << (addr_bits - 2)
    width = len(lines[0])
    for bank in range(4):
        chunk = lines[bank * per : (bank + 1) * per]
        chunk += ["0" * width] * (per - len(chunk))
        (tmp_path / f"{prefix}_bank{bank}.hex").write_text("\n".join(chunk) + "\n")
    return addr_bits


def _matrix_files(tmp_path, arrays, spec, key: str, prefix: str) -> None:
    _split(tmp_path, key, prefix)
    m = arrays[f"{key}.m"].astype(np.int64)
    b = arrays.get(f"{key}.b")
    b = np.zeros(m.shape[0], dtype=np.int64) if b is None else b.astype(np.int64)
    (tmp_path / f"{prefix}_m.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in m.ravel()) + "\n")
    (tmp_path / f"{prefix}_b.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in b) + "\n")


def _run(
    tmp_path, l2, position: int, tokens: list[int], layer_no: int, coverage: bool = False
) -> subprocess.CompletedProcess:
    _graph, spec, arrays = l2.graph, l2.spec, l2.a
    generator.generate(spec, arrays, macs=8, out=tmp_path)
    for key, prefix in (
        (f"{layer_no}.q_proj", "q"),
        (f"{layer_no}.k_proj", "k"),
        (f"{layer_no}.v_proj", "v"),
        (f"{layer_no}.o_proj", "o"),
        (f"{layer_no}.gate_proj", "gate"),
        (f"{layer_no}.up_proj", "up"),
        (f"{layer_no}.down_proj", "down"),
    ):
        _matrix_files(tmp_path, arrays, spec, key, prefix)
    hold: dict = {}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        if layer != layer_no:
            return
        if node["id"] == "attn_norm":
            hold["x"] = env["x"].copy()
            hold["h"] = env["h"].copy()
        if node["id"] == "q_proj":
            hold["q"] = env["q"].copy()
        if node["id"] == "k_proj":
            hold["k"] = env["k"].copy()
        if node["id"] == "v_proj":
            hold["v"] = env["v"].copy()
        if node["id"] == "q_norm":
            hold["qn"] = env["q"].copy()
        if node["id"] == "k_norm":
            hold["kn"] = env["k"].copy()
        if node["id"] == "rope":
            hold["qr"] = env["q"].copy()
            hold["kr"] = env["k"].copy()
        if node["id"] == "attention":
            hold["a"] = env["a"].copy()
        if node["id"] == "o_proj":
            hold["o"] = env["o"].copy()
        if node["id"] == "residual_attn":
            hold["x1"] = env["x"].copy()
        if node["id"] == "mlp_norm":
            hold["h2"] = env["h"].copy()
        if node["id"] == "gate_proj":
            hold["g"] = env["g"].copy()
        if node["id"] == "up_proj":
            hold["u"] = env["u"].copy()
        if node["id"] == "gate_mul":
            hold["m"] = env["m"].copy()
        if node["id"] == "down_proj":
            hold["f"] = env["f"].copy()
        if node["id"] == "residual_mlp":
            hold["x2"] = env["x"].copy()

    l2.forward(tokens[: position + 1], observe=observe)
    x = hold["x"][-1].astype(np.int64)
    expected = {
        name: hold[name][-1].astype(np.int64)
        for name in ("h", "q", "k", "v", "qn", "kn", "qr", "kr", "a", "o", "x1", "h2", "g", "u", "m", "f", "x2")
    }
    (tmp_path / "x.hex").write_text(_pack(x) + "\n")
    (tmp_path / "attn_w.hex").write_text(_pack(arrays[f"{layer_no}.attn_norm.w"]) + "\n")
    (tmp_path / "mlp_w.hex").write_text(_pack(arrays[f"{layer_no}.mlp_norm.w"]) + "\n")
    (tmp_path / "qnorm_w.hex").write_text(_pack(arrays[f"{layer_no}.q_norm.w"]) + "\n")
    (tmp_path / "knorm_w.hex").write_text(_pack(arrays[f"{layer_no}.k_norm.w"]) + "\n")
    for name, values in (
        ("exp_h", expected["h"]),
        ("exp_q", expected["q"]),
        ("exp_k", expected["k"]),
        ("exp_v", expected["v"]),
        ("exp_qn", expected["qn"]),
        ("exp_kn", expected["kn"]),
        ("exp_qr", expected["qr"]),
        ("exp_kr", expected["kr"]),
        ("exp_a", expected["a"]),
        ("exp_o", expected["o"]),
        ("exp_x1", expected["x1"]),
        ("exp_h2", expected["h2"]),
        ("exp_g", expected["g"]),
        ("exp_u", expected["u"]),
        ("exp_m", expected["m"]),
        ("exp_f", expected["f"]),
        ("exp_x2", expected["x2"]),
    ):
        (tmp_path / f"{name}.hex").write_text(_pack(values) + "\n")
    kv_heads, hd = l2.spec["hyper"]["kvHeads"], l2.spec["hyper"]["headDim"]
    context = l2.context
    depth = kv_heads * context
    k_rows = [np.zeros(hd, dtype=np.int64) for _ in range(depth)]
    v_rows = [np.zeros(hd, dtype=np.int64) for _ in range(depth)]
    log2context = (context - 1).bit_length()
    for kv_head in range(kv_heads):
        for pos in range(position + 1):
            row = (kv_head << log2context) + pos
            k_rows[row] = hold["kr"][pos, kv_head * hd : (kv_head + 1) * hd]
            v_rows[row] = hold["v"][pos, kv_head * hd : (kv_head + 1) * hd]
    (tmp_path / "k_cache.hex").write_text("\n".join(_pack(row) for row in k_rows) + "\n")
    (tmp_path / "v_cache.hex").write_text("\n".join(_pack(row) for row in v_rows) + "\n")
    _write_words(tmp_path, "lut.exp.hex", l2.a["lut.exp"].ravel())
    _write_words(tmp_path, "rsqrt.hex", l2.a["lut.rsqrt"].ravel())
    _write_words(tmp_path, "rope_cos.hex", l2.a["lut.rope_cos"].ravel())
    _write_words(tmp_path, "rope_sin.hex", l2.a["lut.rope_sin"].ravel())
    _write_words(tmp_path, "sigmoid.hex", l2.a["lut.sigmoid"].ravel())
    for name in (
        "tile_rmsnorm.v",
        "tile_linear.v",
        "tile_engine.v",
        "tile_rom.v",
        "tile_mac.v",
        "tile_rope.v",
        "tile_kv.v",
        "tile_attention.v",
        "tile_lut.v",
        "tile_residual.v",
        "tile_silu.v",
    ):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(TB, tmp_path / "tile_layer_attn_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_layer_attn_tb",
        "tile_layer_attn_tb.v",
        "tile_rmsnorm.v",
        "tile_linear.v",
        "tile_engine.v",
        "tile_rom.v",
        "tile_mac.v",
        "tile_rope.v",
        "tile_kv.v",
        "tile_attention.v",
        "tile_lut.v",
        "tile_residual.v",
        "tile_silu.v",
    ]
    if coverage:
        command.insert(1, "--coverage")
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    run = subprocess.run(
        [str(tmp_path / "obj_dir" / "Vtile_layer_attn_tb")], cwd=tmp_path, capture_output=True, text=True
    )
    if coverage:
        cov = subprocess.run(
            [str(VERILATOR_COVERAGE), "--annotate", "obj_dir", "coverage.dat"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        run.coverage_text = cov.stdout + cov.stderr
    return run


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


@pytest.mark.parametrize("layer_no", [0, 1])
def test_the_layer_matches_every_observation(l2, tmp_path, layer_no):
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    result = _run(tmp_path, l2, 3, tokens, layer_no)
    assert "PASS" in result.stdout, result.stdout + result.stderr
