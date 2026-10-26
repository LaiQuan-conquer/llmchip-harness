"""`tile_attention` against the scalar golden model (drafts/tile-180nm-pipeline.md §12.1).

The testbench is Verilog, run by Verilator's --binary mode. The vectors, the KV cache and the
expected output come from `llmchip_rtl.tile.attention`, the reference that
`test_attention_reference.py` checks against `GraphL2.attention`. The exp table is the numeric
spec's own table, written as the `$readmemh` file `tile_lut` reads, so the registered LUT and its
wait state are part of the comparison.
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
from llmchip_rtl.tile.attention import att_scale_of, attention_ref

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path("/root/projects/deepseek-harness/tools")))
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"
TB = Path(__file__).with_name("tile_attention_tb.v")
TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"


@pytest.fixture(scope="module")
def model():
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


def _run(
    tmp_path: Path,
    l2,
    seed: int,
    positions: int,
    heads: int,
    kv_heads: int,
    hd: int,
    log2context: int,
    low: int = -16384,
    high: int = 16384,
) -> subprocess.CompletedProcess:
    fq, fk = l2.f["q"], l2.f["k"]
    rng = np.random.default_rng(seed)
    q = rng.integers(low, high, size=heads * hd, dtype=np.int64)
    k = rng.integers(low, high, size=(positions, kv_heads * hd), dtype=np.int64)
    v = rng.integers(low, high, size=(positions, kv_heads * hd), dtype=np.int64)
    expected = attention_ref(q, k, v, l2.a["lut.exp"], fq, fk, heads, kv_heads, hd)

    addr = max(log2context + max(1, (kv_heads - 1).bit_length()), 2)
    depth = 1 << addr
    rows_k = ["0" * (hd * 4)] * depth
    rows_v = ["0" * (hd * 4)] * depth
    for kv_head in range(kv_heads):
        for pos in range(positions):
            row = (kv_head << log2context) + pos
            rows_k[row] = _pack(k[pos, kv_head * hd : (kv_head + 1) * hd])
            rows_v[row] = _pack(v[pos, kv_head * hd : (kv_head + 1) * hd])
    (tmp_path / "q.hex").write_text(_pack(q) + "\n")
    (tmp_path / "k.hex").write_text("\n".join(rows_k) + "\n")
    (tmp_path / "v.hex").write_text("\n".join(rows_v) + "\n")
    (tmp_path / "expected.hex").write_text("\n".join(f"{int(value) & 0xFFFF:04x}" for value in expected) + "\n")
    (tmp_path / "lut.exp.hex").write_text(
        "\n".join(f"{int(value) & 0xFFFF:04x}" for value in l2.a["lut.exp"].ravel()) + "\n"
    )
    for name in ("tile_attention.v", "tile_lut.v"):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(TB, tmp_path / "tile_attention_tb.v")
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
        f"-GFQ={fq}",
        f"-GFK={fk}",
        f"-GATTN_SCALE={att_scale_of(hd)}",
        f"-GS={positions}",
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


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


@pytest.mark.parametrize(
    "positions,heads,kv_heads,log2context,seed",
    [(1, 2, 1, 3, 31), (2, 2, 1, 3, 37), (3, 2, 1, 3, 41), (8, 2, 1, 3, 43)],
)
def test_one_head_group_matches_the_reference(model, tmp_path, positions, heads, kv_heads, log2context, seed):
    result = _run(tmp_path, model, seed, positions, heads, kv_heads, model.spec["hyper"]["headDim"], log2context)
    assert "PASS" in result.stdout, result.stdout + result.stderr


def test_grouped_query_heads_repeat_the_kv_head(model, tmp_path):
    """HEADS=4, KVHEADS=2: heads 0/1 read KV head 0 and heads 2/3 read KV head 1."""
    result = _run(tmp_path, model, 47, 4, 4, 2, model.spec["hyper"]["headDim"], 3)
    assert "PASS" in result.stdout, result.stdout + result.stderr


def test_a_small_head_dimension_and_full_width_values_match(model, tmp_path):
    """HD=4 exercises 16-bit lanes and 17-bit products; full int16 vectors stress saturation."""
    result = _run(tmp_path, model, 53, 4, 2, 1, 4, 2, low=-32768, high=32768)
    assert "PASS" in result.stdout, result.stdout + result.stderr
