"""Many prompts through the full chip, every emitted token against GraphL2 (B5 harness).

The tiny model runs 20 prompts x 8 tokens as a fast regression of the harness; the real Qwen chip
runs 20 prompts x 64 tokens when LLMCHIP_QWEN_FULL=1 and the machine has the expanded memory.
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

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", "/root/projects/deepseek-harness/tools"))
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TB = Path(__file__).with_name("tile_chip_multi_tb.v")
TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"
PROMPTS = [3, 9, 17, 25, 33, 41, 49, 57, 65, 73, 81, 89, 97, 105, 113, 121, 5, 11, 19, 27]


def _l2(path: Path, context: int):
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((path.parent / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    stats = graph_l2.calibrate(graph, loader.get, [PROMPTS[:4]])
    spec, arrays = graph_l2.build(
        graph, loader.peek, weight_format="int4", group_size=16 if context == 16 else 128,
        stats=stats, context=context, residual_bits=16 if context == 16 else 24,
    )
    return graph_l2.GraphL2(graph, spec, arrays)


def _greedy(l2, prompt: int, steps: int) -> list[int]:
    ids = [prompt]
    out = []
    for _ in range(steps):
        nxt = int(np.argmax(l2.forward(ids)[-1]))
        ids.append(nxt)
        out.append(nxt)
    return out


def _run(tmp_path, l2, layers: int, length: int, prompts: list[int]) -> subprocess.CompletedProcess:
    spec, arrays = l2.spec, l2.a
    generator.generate(spec, arrays, macs=8, out=tmp_path)
    (tmp_path / "prompts.hex").write_text("\n".join(f"{p & 0xFFFFFF:06x}" for p in prompts) + "\n")
    expected = [t for p in prompts for t in _greedy(l2, p, length)]
    (tmp_path / "expected.hex").write_text("\n".join(f"{t & 0xFFFFFF:06x}" for t in expected) + "\n")
    shutil.copyfile(TB, tmp_path / "tile_chip_multi_tb.v")
    files = sorted(str(p) for p in (tmp_path / "rtl").glob("*.v"))
    command = [
        str(VERILATOR), "--binary", "--timing", "-Wno-fatal", "--top-module", "tile_chip_multi_tb",
        f"-GHIDDEN={int(spec['hyper']['hidden'])}", f"-GRES={int(spec['activations']['bits']['x'])}",
        f"-GLAYERS={layers}", f"-GPROMPTS={len(prompts)}", f"-GLENGTH={length}",
        "tile_chip_multi_tb.v", *files,
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run([str(tmp_path / "obj_dir" / "Vtile_chip_multi_tb")], cwd=tmp_path, capture_output=True, text=True)


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


def test_twenty_prompts_through_the_tiny_chip(tmp_path):
    l2 = _l2(TINY_QWEN3 / "model.safetensors", 16)
    result = _run(tmp_path, l2, 2, 8, PROMPTS)
    assert "PASS" in result.stdout, result.stdout + result.stderr
