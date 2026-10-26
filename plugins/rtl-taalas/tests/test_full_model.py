"""Full tiny-model path: layer 0 -> layer 1 -> final_norm -> lm_head, compared with L2 logits.

The RTL is exercised as three pin-level harnesses: each layer harness exposes its residual output
x2, and the tail harness consumes that output and produces the 128 logits.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tile_layer import _run, _split

TOOLS = Path("/root/projects/deepseek-harness/tools")
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"
TAIL_TB = Path(__file__).with_name("tile_tail_tb.v")
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


def _read_x2(tmp_path: Path) -> np.ndarray:
    values = [int(line, 16) for line in (tmp_path / "x2.hex").read_text().split()]
    return np.array(values, dtype=np.int64)


def _run_tail(tmp_path: Path, l2, x2: np.ndarray) -> subprocess.CompletedProcess:
    tokens = [3, 9, 17, 25]
    logits = l2.forward(tokens)[-1].astype(np.int64)
    arrays = l2.a
    _split(tmp_path, "embed", "lm")
    m = arrays["embed.m"].astype(np.int64)
    (tmp_path / "lm_m.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in m.ravel()) + "\n")
    (tmp_path / "lm_b.hex").write_text("\n".join("0000" for _ in range(128)) + "\n")
    (tmp_path / "x2.hex").write_text(_pack(x2) + "\n")
    (tmp_path / "final_w.hex").write_text(_pack(arrays["final_norm.w"]) + "\n")
    (tmp_path / "expected_logits.hex").write_text("\n".join(f"{int(v) & 0xFFFFFFFF:08x}" for v in logits) + "\n")
    (tmp_path / "rsqrt.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in arrays["lut.rsqrt"].ravel()) + "\n")
    for name in (
        "tile_tail_tb.v",
        "tile_rmsnorm.v",
        "tile_linear.v",
        "tile_engine.v",
        "tile_rom.v",
        "tile_mac.v",
        "tile_lut.v",
    ):
        source = TAIL_TB if name == "tile_tail_tb.v" else TEMPLATES / name
        shutil.copyfile(source, tmp_path / name)
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_tail_tb",
        "tile_tail_tb.v",
        "tile_rmsnorm.v",
        "tile_linear.v",
        "tile_engine.v",
        "tile_rom.v",
        "tile_mac.v",
        "tile_lut.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run([str(tmp_path / "obj_dir" / "Vtile_tail_tb")], cwd=tmp_path, capture_output=True, text=True)


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


def test_the_full_model_logits_match_l2(l2, tmp_path):
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    first = _run(tmp_path, l2, 3, tokens, 0)
    assert "PASS" in first.stdout, first.stdout + first.stderr
    x2_layer0 = _read_x2(tmp_path)
    second = _run(tmp_path, l2, 3, tokens, 1, x_override=x2_layer0)
    assert "PASS" in second.stdout, second.stdout + second.stderr
    x2_layer1 = _read_x2(tmp_path)
    tail = _run_tail(tmp_path, l2, x2_layer1)
    assert "PASS" in tail.stdout, tail.stdout + tail.stderr
    print("FULL_MODEL logits=128", file=sys.__stdout__)
