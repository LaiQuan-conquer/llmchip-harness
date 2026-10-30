"""The Q-full layer chain against GraphL2 (drafts/Qwen05B_Taalas_Refactor_Plan_zh.md §12.1 B1/B3).

`chain.generate` writes one `tile_layer` instance per decoder layer and the handshake between them
(layer i+1 starts on layer i's done). The test feeds the layer-0 residual for each position from L2
and compares the last layer's x2 with L2, at 16 and 24 bits of residual width.
"""

from __future__ import annotations

import json
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
from llmchip_rtl.tile import generator

TOOLS = Path("/root/projects/deepseek-harness/tools")
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TB = Path(__file__).with_name("tile_chain_tb.v")
TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"
TOKENS = [3, 9, 17, 25, 33, 41, 49, 57]


def _pack(values: np.ndarray, bits: int) -> str:
    mask = (1 << bits) - 1
    word = 0
    for lane, value in enumerate(np.asarray(values).ravel()):
        word |= (int(value) & mask) << (bits * lane)
    return f"{word:0{len(np.asarray(values).ravel()) * (bits // 4)}x}"


def _l2(residual_bits: int):
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    stats = graph_l2.calibrate(graph, loader.get, [TOKENS])
    spec, arrays = graph_l2.build(
        graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=16,
        residual_bits=residual_bits,
    )
    return graph_l2.GraphL2(graph, spec, arrays)


def _run(tmp_path, l2, position: int) -> subprocess.CompletedProcess:
    spec, arrays = l2.spec, l2.a
    res = int(spec["activations"]["bits"]["x"])
    layers = int(spec["hyper"]["layers"])
    hold: dict = {"x": None, "x2": None, "x2_l0": None}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        if layer == 0 and node["id"] == "attn_norm":
            hold["x"] = env["x"].astype(np.int64).copy()
        if layer == 0 and node["id"] == "residual_mlp" and layers > 1:
            hold["x2_l0"] = env["x"][-1].astype(np.int64).copy()
        if layer == layers - 1 and node["id"] == "residual_mlp":
            hold["x2"] = env["x"][-1].astype(np.int64).copy()

    l2.forward(TOKENS[: position + 1], observe=observe)
    generator.generate(spec, arrays, macs=8, out=tmp_path)
    (tmp_path / "x.hex").write_text("\n".join(_pack(row, res) for row in hold["x"][: position + 1]) + "\n")
    (tmp_path / "exp_x2.hex").write_text(_pack(hold["x2"], res) + "\n")
    if hold["x2_l0"] is not None:
        (tmp_path / "exp_x2_l0.hex").write_text(_pack(hold["x2_l0"], res) + "\n")
    shutil.copyfile(TB, tmp_path / "tile_chain_tb.v")
    files = sorted(str(p) for p in (tmp_path / "rtl").glob("*.v"))
    command = [
        str(VERILATOR), "--binary", "--timing", "-Wno-fatal",
        "--top-module", "tile_chain_tb",
        f"-GHIDDEN={int(spec['hyper']['hidden'])}", f"-GRES={res}", f"-GSTEPS={position + 1}", f"-GLAYERS={layers}",
        "tile_chain_tb.v", *files,
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run([str(tmp_path / "obj_dir" / "Vtile_chain_tb")], cwd=tmp_path, capture_output=True, text=True)


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


@pytest.mark.parametrize("residual_bits", [16, 24])
def test_the_layer_chain_matches_l2(tmp_path, residual_bits):
    result = _run(tmp_path, _l2(residual_bits), 3)
    assert "PASS" in result.stdout, result.stdout + result.stderr
