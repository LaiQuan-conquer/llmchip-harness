"""The Q-full chip generating eight tokens by itself (drafts/Qwen05B_Taalas_Refactor_Plan_zh.md B3).

The chip runs embedding -> layer chain -> final norm -> lm_head -> argmax eight times, starting
from one token, and its last generated token is compared with GraphL2's greedy sequence.
"""

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
TB = Path(__file__).with_name("tile_chip_tb.v")
TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"
PROMPT = [3]
STEPS = 8


def _l2() -> graph_l2.GraphL2:
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    stats = graph_l2.calibrate(graph, loader.get, [[3, 9, 17, 25]])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=16)
    return graph_l2.GraphL2(graph, spec, arrays)


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


def _greedy(l2, prompt: list[int], steps: int) -> list[int]:
    """Greedy decoding of the graph L2: argmax over logits, lowest token id on ties."""
    ids = list(prompt)
    out: list[int] = []
    for _ in range(steps):
        logits = l2.forward(ids)[-1]
        nxt = int(np.argmax(logits))
        ids.append(nxt)
        out.append(nxt)
    return out


def test_the_chip_generates_eight_tokens_by_itself(tmp_path):
    l2 = _l2()
    tokens = _greedy(l2, PROMPT, STEPS)
    assert len(tokens) == STEPS
    generator.generate(l2.spec, l2.a, macs=8, out=tmp_path)
    (tmp_path / "exp_token.hex").write_text(f"{int(tokens[-1]) & 0xFFFFFF:06x}\n")
    (tmp_path / "exp_tokens.hex").write_text("\n".join(f"{int(t) & 0xFFFFFF:06x}" for t in tokens) + "\n")
    shutil.copyfile(TB, tmp_path / "tile_chip_tb.v")
    files = sorted(str(p) for p in (tmp_path / "rtl").glob("*.v"))
    command = [
        str(VERILATOR), "--binary", "--timing", "-Wno-fatal",
        "--top-module", "tile_chip_tb",
        f"-GHIDDEN={int(l2.spec['hyper']['hidden'])}",
        f"-GRES={int(l2.spec['activations']['bits']['x'])}",
        f"-GLAYERS={int(l2.spec['hyper']['layers'])}",
        "tile_chip_tb.v", *files,
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert build.returncode == 0, (build.stdout + build.stderr)[-3000:]
    run = subprocess.run([str(tmp_path / "obj_dir" / "Vtile_chip_tb")], cwd=tmp_path, capture_output=True, text=True)
    assert "PASS" in run.stdout, run.stdout + run.stderr
