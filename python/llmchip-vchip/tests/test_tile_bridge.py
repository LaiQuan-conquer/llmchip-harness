"""The tile pin-level bridge: the tiny tile chip driven over its parallel host pins, token by
token, against GraphL2 (B6). The vchip `TileSim` will speak this testbench's protocol."""

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
TB_DIR = Path(__file__).resolve().parents[1] / "src/llmchip_vchip/bridges/tb"
TINY_QWEN3 = Path(__file__).parents[3] / "tests/fixtures/models/tiny-qwen3"
PROMPT, STEPS = 3, 8


def _l2():
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    stats = graph_l2.calibrate(graph, loader.get, [[3, 9, 17, 25]])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=16)
    return graph_l2.GraphL2(graph, spec, arrays)


def _greedy(l2, prompt: int, steps: int):
    ids = [prompt]
    out = []
    for _ in range(steps):
        nxt = int(np.argmax(l2.forward(ids)[-1]))
        ids.append(nxt)
        out.append(nxt)
    return out


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


def test_the_tile_pin_bridge_generates_tokens(tmp_path):
    l2 = _l2()
    generator.generate(l2.spec, l2.a, macs=8, out=tmp_path)
    for name in ("tile_top.v", "tile_tb.cpp"):
        shutil.copyfile(TB_DIR / name, tmp_path / name)
    files = sorted(str(p) for p in (tmp_path / "rtl").glob("*.v"))
    build = subprocess.run(
        [
            str(VERILATOR),
            "--cc",
            "--exe",
            "--build",
            "-Wno-fatal",
            "--top-module",
            "tb_top",
            "tile_top.v",
            "tile_tb.cpp",
            *files,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, (build.stdout + build.stderr)[-4000:]
    proc = subprocess.Popen(
        [str(tmp_path / "obj_dir" / "Vtb_top")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=tmp_path
    )
    proc.stdin.write("RESET\n")
    proc.stdin.flush()
    assert proc.stdout.readline().strip() == "OK"
    got = []
    for _ in range(STEPS):
        proc.stdin.write(f"STEP {got[-1] if got else PROMPT} {len(got)}\n")
        proc.stdin.flush()
        line = proc.stdout.readline().split()
        assert line[0] == "STEP" and "TIMEOUT" not in line, line
        got.append(int(line[1]))
    proc.stdin.write("QUIT\n")
    proc.stdin.flush()
    proc.wait(timeout=30)
    assert got == _greedy(l2, PROMPT, STEPS), got


def _image() -> str:
    import json

    manifest = json.loads((Path(__file__).parents[3] / "plugins/sim-verilator/llmchip-plugin.json").read_text())
    return manifest["entry"]["image"]


def _bundle_tar(tmp_path):
    import tarfile

    l2 = _l2()
    root = tmp_path / "bundle-root"
    generator.generate(l2.spec, l2.a, macs=8, out=root)
    tar = tmp_path / "tile.tar"
    with tarfile.open(tar, "w") as archive:
        archive.add(root, arcname="bundle")
    return tar


def test_the_tile_bundle_builds_in_the_simulator_image(tmp_path):
    from llmchip_vchip.bridges import verilator

    if not verilator.image_present(_image()):
        pytest.skip("the simulator image is not present")
    out = verilator.build_tile(_bundle_tar(tmp_path), _image(), tmp_path / "cache")
    assert (out / "bundle/rtl/obj_tile/Vtile").is_file()
