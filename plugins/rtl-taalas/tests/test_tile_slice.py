"""The functional S-1 slice against GraphL2: q_proj rows of head 0, real activation input.

The generator writes the slice's weight banks, mantissas and biases; the testbench drives the
activation vector of the model's `attn_norm` output and compares every emitted row with the
`q_proj` observation point of `GraphL2.forward`.
"""

from __future__ import annotations

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
from llmchip_rtl.tile import generator

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path("/root/projects/deepseek-harness/tools")))
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"
TB = Path(__file__).with_name("tile_slice_tb.v")
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


def _run(tmp_path, built, token: int) -> subprocess.CompletedProcess:
    graph, spec, arrays = built
    manifest = generator.generate(spec, arrays, slice_spec(graph, spec, "S-1"), macs=8, out=tmp_path)
    linear = manifest["linear"]
    l2 = graph_l2.GraphL2(graph, spec, arrays)
    hold: dict = {}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        if layer == 0 and node["id"] == "q_proj":
            hold["x"] = env[node["inputs"][0]].copy()
            hold["q"] = env[node["outputs"][0]].copy()

    l2.forward([token], observe=observe)
    x = hold["x"][0, : linear["columns"]].astype(np.int64)
    expected = hold["q"][0, : linear["rows"]].astype(np.int64)
    (tmp_path / "x.hex").write_text("\n".join(f"{int(value) & 0xFFFF:04x}" for value in x) + "\n")
    (tmp_path / "expected.hex").write_text("\n".join(f"{int(value) & 0xFFFF:04x}" for value in expected) + "\n")
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
        f"-GMACS={linear['macs']}",
        f"-GBITS={manifest['mapping']['bits']}",
        "-GGROUPS=8",
        f"-GROWS={linear['rows']}",
        f"-GCHUNKS={linear['chunks']}",
        f"-GLOG2CHUNKS={(linear['chunks'] - 1).bit_length()}",
        f"-GLOG2ROWS={(linear['rows'] - 1).bit_length()}",
        f"-GLOG2ADDR={(manifest['banks'][0]['words'] - 1).bit_length()}",
        f"-GLOG2MACS={(linear['macs'] - 1).bit_length()}",
        f"-GLOG2CPG={(linear['chunksPerGroup']).bit_length() - 1}",
        f"-GNGROUPS={linear['quantGroups']}",
        f"-GFRAC_IN={linear['fracIn']}",
        f"-GFRAC_OUT={linear['fracOut']}",
        f"-GEXP={linear['exponent']}",
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


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


@pytest.mark.parametrize("token", [3, 9, 25, 57])
def test_the_slice_matches_the_model_observation(built, tmp_path, token):
    result = _run(tmp_path, built, token)
    assert "PASS" in result.stdout, result.stdout + result.stderr
