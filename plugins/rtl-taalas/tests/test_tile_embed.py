"""tile_embed against GraphL2's embedding node."""

from __future__ import annotations

import json
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

TOOLS = Path("/root/projects/deepseek-harness/tools")
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TB = Path(__file__).with_name("tile_embed_tb.v")
TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"
TOKEN = 9


@pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")
def test_the_embedding_row_matches_l2(tmp_path):
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    stats = graph_l2.calibrate(graph, loader.get, [[3, 9, 17, 25]])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=16)
    l2 = graph_l2.GraphL2(graph, spec, arrays)
    hold = {}

    def observe(node: dict, layer, env: dict) -> None:
        if node.get("op") == "embedding":
            hold["x"] = env["x"][-1].astype(np.int64).copy()

    l2.forward([TOKEN], observe=observe)
    res = int(spec["activations"]["bits"]["x"])
    mask = (1 << res) - 1
    word = 0
    for lane, value in enumerate(hold["x"]):
        word |= (int(value) & mask) << (res * lane)
    (tmp_path / "exp_x.hex").write_text(f"{word:0{len(hold['x']) * (res // 4)}x}\n")
    (tmp_path / "token.hex").write_text(f"{TOKEN:06x}\n")
    generator.generate(spec, arrays, macs=8, out=tmp_path)
    shutil.copyfile(TB, tmp_path / "tile_embed_tb.v")
    files = sorted(str(p) for p in (tmp_path / "rtl").glob("*.v"))
    command = [
        str(VERILATOR), "--binary", "--timing", "-Wno-fatal", "--top-module", "tile_embed_tb",
        f"-GHIDDEN={int(spec['hyper']['hidden'])}", f"-GRES={res}",
        "tile_embed_tb.v", *files,
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert build.returncode == 0, (build.stdout + build.stderr)[-3000:]
    run = subprocess.run([str(tmp_path / "obj_dir" / "Vtile_embed_tb")], cwd=tmp_path, capture_output=True, text=True)
    assert "PASS" in run.stdout, run.stdout + run.stderr
