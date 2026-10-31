"""One real layer of Qwen2.5-0.5B-Instruct against GraphL2 (drafts/.../§12.1 B4).

Opt-in: LLMCHIP_QWEN=1; LLMCHIP_QWEN_LAYER selects the layer (default 0). The model is loaded from
the tools directory, quantized to the numeric spec (int4, groups of 128, 24-bit residual), the
layer's ROM banks and norm weights are written with the same names the harness expects, and the
layer runs four positions supplied by L2.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2
from llmchip_rtl.tile import attention, rom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tile_layer import _matrix_files, _pack, _write_words

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", "/root/projects/deepseek-harness/tools"))
MODEL = Path(os.environ.get("LLMCHIP_QWEN_MODEL", TOOLS / "models/Qwen2.5-0.5B-Instruct"))
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TB = Path(__file__).with_name("tile_layer_qwen_tb.v")
TOKENS = [3, 9, 17, 25, 33, 41, 49, 57]
LAYER = int(os.environ.get("LLMCHIP_QWEN_LAYER", "0"))

ready = bool(os.environ.get("LLMCHIP_QWEN")) and (MODEL / "model.safetensors").is_file() and VERILATOR.is_file()


def _l2():
    path = MODEL / "model.safetensors"
    config = json.loads((MODEL / "config.json").read_text())
    layout = read_layout(path)
    graph = recover(census([layout], config, paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    stats = graph_l2.calibrate(graph, loader.get, [TOKENS])
    spec, arrays = graph_l2.build(
        graph, loader.peek, weight_format="int4", group_size=128, stats=stats, context=512, residual_bits=24
    )
    return graph_l2.GraphL2(graph, spec, arrays)


@pytest.fixture(scope="module")
def qwen_l2():
    return _l2()


LAYERS = [int(x) for x in os.environ.get("LLMCHIP_QWEN_LAYERS", str(LAYER)).split(",")]


@pytest.mark.skipif(not ready, reason="set LLMCHIP_QWEN=1 and install the model")
@pytest.mark.parametrize("layer", LAYERS)
def test_one_real_layer_matches_l2(qwen_l2, tmp_path, layer):
    l2 = qwen_l2
    spec, arrays = l2.spec, l2.a
    h = spec["hyper"]
    frac = spec["activations"]["frac"]
    hold: dict = {"x": None, "obs": {}}

    def observe(node: dict, layer_no, env: dict) -> None:
        if layer_no != layer:
            return
        if node["id"] == "attn_norm":
            hold["x"] = env["x"].astype(np.int64).copy()
        for key, node_id, field in (
            ("h", "attn_norm", "h"), ("q", "q_proj", "q"), ("k", "k_proj", "k"), ("v", "v_proj", "v"),
            ("a", "attention", "a"), ("o", "o_proj", "o"), ("x1", "residual_attn", "x"),
            ("h2", "mlp_norm", "h"), ("g", "gate_proj", "g"), ("u", "up_proj", "u"),
            ("m", "gate_mul", "m"), ("f", "down_proj", "f"), ("x2", "residual_mlp", "x"),
        ):
            if node["id"] == node_id:
                hold["obs"][key] = env[field][-1].astype(np.int64).copy()
        if node["id"] == "rope":
            hold["obs"]["qr"] = env["q"][-1].astype(np.int64).copy()
            hold["obs"]["kr"] = env["k"][-1].astype(np.int64).copy()

    pos = int(os.environ.get("LLMCHIP_QWEN_POS", "3"))
    l2.forward(TOKENS[: pos + 1], observe=observe)
    res = int(spec["activations"]["bits"]["x"])
    x = hold["x"][pos]
    (tmp_path / "x.hex").write_text("\n".join(_pack(row, res) for row in hold["x"][: pos + 1]) + "\n")
    obs = hold["obs"]
    obs["qn"] = obs["q"]          # Qwen2.5 has no q/k norm: the observation is q/k itself
    obs["kn"] = obs["k"]
    widths = {"h": 16, "q": 16, "k": 16, "v": 16, "qn": 16, "kn": 16, "qr": 16, "kr": 16, "a": 16,
              "o": res, "x1": res, "h2": 16, "g": 16, "u": 16, "m": 16, "f": res, "x2": res}
    for name, values in obs.items():
        (tmp_path / f"exp_{name}.hex").write_text(_pack(values, widths[name]) + "\n")
    (tmp_path / "rom").mkdir(exist_ok=True)
    for key in ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"):
        q = np.asarray(arrays[f"{layer}.{key}.q"]).astype(np.int64)
        lines = rom.hexfile(rom.to_words(q, 128, 4), 128, 4)
        (tmp_path / "rom" / f"{layer}.{key}.hex").write_text("\n".join(lines) + "\n")
    for key, prefix in (("q_proj", "q"), ("k_proj", "k"), ("v_proj", "v"), ("o_proj", "o"),
                        ("gate_proj", "gate"), ("up_proj", "up"), ("down_proj", "down")):
        bias_bits = 32 if (prefix in ("o", "down") and res > 16) else 16
        _matrix_files(tmp_path, arrays, spec, f"{layer}.{key}", prefix, bias_bits)
    (tmp_path / "attn_w.hex").write_text(_pack(arrays[f"{layer}.attn_norm.w"]) + "\n")
    (tmp_path / "mlp_w.hex").write_text(_pack(arrays[f"{layer}.mlp_norm.w"]) + "\n")
    for name in ("rsqrt", "exp", "sigmoid", "rope_cos", "rope_sin"):
        _write_words(tmp_path, f"{name}.hex", arrays[f"lut.{name}"].ravel())
    shutil.copyfile(tmp_path / "exp.hex", tmp_path / "lut.exp.hex")
    for name in ("tile_layer.v", "tile_rmsnorm.v", "tile_linear.v", "tile_engine.v", "tile_rom.v",
                 "tile_mac.v", "tile_rope.v", "tile_kv.v", "tile_attention.v", "tile_lut.v",
                 "tile_residual.v", "tile_silu.v"):
        shutil.copyfile(Path(__file__).resolve().parents[1] / "src/llmchip_rtl/tile/templates" / name, tmp_path / name)
    shutil.copyfile(TB, tmp_path / "tile_layer_qwen_tb.v")
    mk, gk = int(h["headDim"]), int(h["kvHeads"])
    command = [
        str(VERILATOR), "--binary", "--timing", "-Wno-fatal", "--top-module", "tile_layer_qwen_tb",
        f"-GHIDDEN={int(h['hidden'])}", f"-GINTER={int(h['intermediate'])}", f"-GHEADS={int(h['heads'])}",
        f"-GKVHEADS={gk}", f"-GHD={mk}", f"-GLOG2CONTEXT=9", "-GMACS=128", f"-GRES={res}",
        f"-GFRAC_H={int(frac['xn'])}", f"-GFRAC_QK={int(frac['q'])}", f"-GNORM_FN={int(spec['normWeights']['frac'])}",
        f"-GEPS_ATTN={round(1e-6 * 2.0 ** (2 * int(frac['x'])))}",
        f"-GEPS_MLP={round(1e-6 * 2.0 ** (2 * int(frac['x'])))}",
        f"-GEPS_HEAD={round(1e-6 * 2.0 ** (2 * int(frac['q'])))}",
        f"-GROPE_ADDR={(int(np.asarray(arrays['lut.rope_cos']).size) - 1).bit_length()}",
        f"-GEXP_ADDR={(int(np.asarray(arrays['lut.exp']).size) - 1).bit_length()}",
        f"-GATTN_SCALE={attention.att_scale_of(mk)}",
        f"-GSILU_F1={int(frac['h1'])}", f"-GSILU_F3={int(frac['h3'])}", f"-GSILU_FG={int(frac['g'])}",
        *[arg for tag, node in (("Q", "q_proj"), ("K", "k_proj"), ("V", "v_proj"), ("O", "o_proj"),
                                ("G", "gate_proj"), ("U", "up_proj"), ("D", "down_proj"))
          for arg in (f"-G{tag}_FI={int(frac[{'q_proj':'xn','k_proj':'xn','v_proj':'xn','o_proj':'v','gate_proj':'xn','up_proj':'xn','down_proj':'g'}[node]])}",
                      f"-G{tag}_FO={int(frac[{'q_proj':'q','k_proj':'k','v_proj':'v','o_proj':'x','gate_proj':'h1','up_proj':'h3','down_proj':'x'}[node]])}",
                      f"-G{tag}_EXP={int(spec['weights']['exponents'][f'{layer}.{node}'])}")],
        "tile_layer_qwen_tb.v", "tile_layer.v", "tile_rmsnorm.v", "tile_linear.v", "tile_engine.v",
        "tile_rom.v", "tile_mac.v", "tile_rope.v", "tile_kv.v", "tile_attention.v", "tile_lut.v",
        "tile_residual.v", "tile_silu.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert build.returncode == 0, (build.stdout + build.stderr)[-4000:]
    run = subprocess.run([str(tmp_path / "obj_dir" / "Vtile_layer_qwen_tb")], cwd=tmp_path, capture_output=True, text=True)
    assert "PASS" in run.stdout, run.stdout + run.stderr
    match = re.search(r"CHECKS (\d+)", run.stdout)
    assert match, run.stdout[-2000:]
    print(f"LAYER_CHECKS {int(match.group(1))}")
