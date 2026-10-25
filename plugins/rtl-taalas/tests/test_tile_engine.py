"""The pipelined datapath of the tile: `tile_engine` against a Python reference (design N3 of
drafts/tile-180nm-pipeline.md). The engine walks the ROM's words, each word holding MACS levels of
one input column, and accumulates per lane; the reference is the same sum computed here.

The testbench is Verilog, run by Verilator's --binary mode; the vectors and the expected values are
written from Python. It skips visibly when Verilator is not in the tools directory.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from llmchip_rtl.tile import rom

TOOLS = Path("/root/projects/deepseek-harness/tools/oss-cad-suite/bin")
VERILATOR = TOOLS / "verilator"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"
TB = Path(__file__).with_name("tile_engine_tb.v")


def _vectors(seed: int, macs: int, words: int, bits: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    levels = rng.integers(lo, hi + 1, size=(macs, words), dtype=np.int64)
    x = rng.integers(-(1 << 14), 1 << 14, size=words, dtype=np.int64)
    expected = np.array([int((levels[k] * x).sum()) for k in range(macs)])
    return levels, x, expected


def _run(tmp_path: Path, macs: int, words: int, bits: int, seed: int) -> subprocess.CompletedProcess:
    levels, x, expected = _vectors(seed, macs, words, bits)
    (tmp_path / "x.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in x) + "\n")
    packed = rom.to_words(levels, macs, bits)
    (tmp_path / "rom.hex").write_text("\n".join(rom.hexfile(packed, macs, bits)) + "\n")
    mask = (1 << 32) - 1
    (tmp_path / "expected.hex").write_text("\n".join(f"{int(v) & mask:08x}" for v in expected) + "\n")
    log2words = max(1, (words - 1).bit_length())
    for name in ("tile_engine.v", "tile_rom.v", "tile_mac.v"):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(TB, tmp_path / "tile_engine_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        f"-GMACS={macs}",
        f"-GBITS={bits}",
        f"-GWORDS={words}",
        f"-GLOG2WORDS={log2words}",
        f"-GLOG2ADDR={log2words}",
        "--top-module",
        "tile_engine_tb",
        "tile_engine_tb.v",
        "tile_engine.v",
        "tile_rom.v",
        "tile_mac.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run([str(tmp_path / "obj_dir" / "Vtile_engine_tb")], cwd=tmp_path, capture_output=True, text=True)


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


@pytest.mark.parametrize("macs,words,bits", [(8, 4, 4), (8, 8, 4), (16, 5, 8), (4, 3, 4)])
def test_the_engine_matches_the_reference(tmp_path, macs, words, bits):
    result = _run(tmp_path, macs, words, bits, seed=macs * 100 + words * 10 + bits)
    assert "PASS" in result.stdout, result.stdout + result.stderr
