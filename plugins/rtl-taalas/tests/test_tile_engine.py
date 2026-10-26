"""The row-major pipelined datapath of the tile: `tile_engine` against a Python reference (design
N3 of drafts/tile-180nm-pipeline.md). One ROM word holds MACS consecutive weights of one output
row, so the engine's group tree sums each word's products and the chunk sums accumulate into the
row's value: y[r] = sum over columns of level[r, c] * x[c]. The testbench is Verilog, run by
Verilator's --binary mode; the vectors and the expected values are written from Python.
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


def _vectors(seed: int, macs: int, rows: int, chunks: int, bits: int):
    rng = np.random.default_rng(seed)
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    columns = chunks * macs
    levels = rng.integers(lo, hi + 1, size=(rows, columns), dtype=np.int64)
    x = rng.integers(-(1 << 14), 1 << 14, size=columns, dtype=np.int64)
    expected = np.array([int((levels[r] * x).sum()) for r in range(rows)])
    return levels, x, expected


def _x_line(values: np.ndarray) -> str:
    word = 0
    for k, value in enumerate(values):
        word |= (int(value) & 0xFFFF) << (16 * k)
    return f"{word:0{len(values) * 4}x}"


def _run(
    tmp_path: Path, macs: int, rows: int, chunks: int, bits: int, groups: int, seed: int
) -> subprocess.CompletedProcess:
    levels, x, expected = _vectors(seed, macs, rows, chunks, bits)
    (tmp_path / "x.hex").write_text("\n".join(_x_line(x[c * macs : (c + 1) * macs]) for c in range(chunks)) + "\n")
    packed = rom.to_words(levels, macs, bits)
    lines = rom.hexfile(packed, macs, bits)
    (tmp_path / "rom.hex").write_text("\n".join(lines) + "\n")
    addr_bits = max(2, (len(lines) - 1).bit_length())
    per = 1 << (addr_bits - 2)
    for bank in range(4):
        chunk = lines[bank * per : (bank + 1) * per]
        chunk += ["0" * len(lines[0])] * (per - len(chunk))
        (tmp_path / f"rom_bank{bank}.hex").write_text("\n".join(chunk) + "\n")
    mask = (1 << 32) - 1
    (tmp_path / "expected.hex").write_text("\n".join(f"{int(v) & mask:08x}" for v in expected) + "\n")
    for name in ("tile_engine.v", "tile_rom.v", "tile_mac.v"):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(TB, tmp_path / "tile_engine_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_engine_tb",
        f"-GMACS={macs}",
        f"-GBITS={bits}",
        f"-GGROUPS={groups}",
        f"-GROWS={rows}",
        f"-GCHUNKS={chunks}",
        f"-GLOG2CHUNKS={max(1, (chunks - 1).bit_length())}",
        f"-GLOG2ROWS={max(1, (rows - 1).bit_length())}",
        f"-GLOG2ADDR={max(1, (rows * chunks - 1).bit_length())}",
        f"-GLOG2MACS={max(1, (macs - 1).bit_length())}",
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


@pytest.mark.parametrize(
    "macs,rows,chunks,bits,groups",
    [(16, 4, 2, 4, 8), (16, 6, 3, 4, 8), (32, 4, 2, 8, 8), (8, 3, 2, 4, 4), (8, 2, 2, 4, 4)],
)
def test_the_engine_matches_the_reference(tmp_path, macs, rows, chunks, bits, groups):
    result = _run(tmp_path, macs, rows, chunks, bits, groups, seed=macs * 100 + rows * 10 + chunks + bits)
    assert "PASS" in result.stdout, result.stdout + result.stderr


def _run_dual(tmp_path, macs, rows, chunks, bits, groups, seed):
    rng = np.random.default_rng(seed)
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    columns = chunks * macs
    levels_a = rng.integers(lo, hi + 1, size=(rows, columns), dtype=np.int64)
    levels_b = rng.integers(lo, hi + 1, size=(rows, columns), dtype=np.int64)
    x = rng.integers(-(1 << 14), 1 << 14, size=columns, dtype=np.int64)
    expected_a = np.array([int((levels_a[r] * x).sum()) for r in range(rows)])
    expected_b = np.array([int((levels_b[r] * x).sum()) for r in range(rows)])
    lines_x = []
    for chunk in range(chunks):
        word = 0
        for k in range(macs):
            word |= (int(x[chunk * macs + k]) & 0xFFFF) << (16 * k)
        lines_x.append(f"{word:0{macs * 4}x}")
    (tmp_path / "x.hex").write_text("\n".join(lines_x) + "\n")
    for prefix, levels in (("rom_a", levels_a), ("rom_b", levels_b)):
        packed = rom.to_words(levels, macs, bits)
        lines = rom.hexfile(packed, macs, bits)
        (tmp_path / f"{prefix}.hex").write_text("\n".join(lines) + "\n")
        addr_bits = max(2, (len(lines) - 1).bit_length())
        per = 1 << (addr_bits - 2)
        for bank in range(4):
            chunk = lines[bank * per : (bank + 1) * per]
            chunk += ["0" * len(lines[0])] * (per - len(chunk))
            (tmp_path / f"{prefix}_bank{bank}.hex").write_text("\n".join(chunk) + "\n")
    mask = (1 << 32) - 1
    for name, expected in (("expected_a.hex", expected_a), ("expected_b.hex", expected_b)):
        (tmp_path / name).write_text("\n".join(f"{int(v) & mask:08x}" for v in expected) + "\n")
    for name in ("tile_engine2.v", "tile_engine.v", "tile_rom.v", "tile_mac.v"):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(Path(__file__).with_name("tile_engine2_tb.v"), tmp_path / "tile_engine2_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_engine2_tb",
        f"-GMACS={macs}",
        f"-GBITS={bits}",
        f"-GGROUPS={groups}",
        f"-GROWS={rows}",
        f"-GCHUNKS={chunks}",
        f"-GLOG2CHUNKS={max(1, (chunks - 1).bit_length())}",
        f"-GLOG2ROWS={max(1, (rows - 1).bit_length())}",
        f"-GLOG2ADDR={max(1, (rows * chunks - 1).bit_length())}",
        f"-GLOG2MACS={max(1, (macs - 1).bit_length())}",
        "tile_engine2_tb.v",
        "tile_engine2.v",
        "tile_engine.v",
        "tile_rom.v",
        "tile_mac.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run(
        [str(tmp_path / "obj_dir" / "Vtile_engine2_tb")], cwd=tmp_path, capture_output=True, text=True
    )


@pytest.mark.parametrize("macs,rows,chunks,bits,groups", [(16, 4, 2, 4, 8), (8, 3, 2, 4, 4)])
def test_two_streams_match_their_references(tmp_path, macs, rows, chunks, bits, groups):
    result = _run_dual(tmp_path, macs, rows, chunks, bits, groups, seed=macs * 7 + rows)
    assert "PASS" in result.stdout, result.stdout + result.stderr


def _run_batch(tmp_path, macs, rows, chunks, bits, groups, batch, seed):
    rng = np.random.default_rng(seed)
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    columns = chunks * macs
    levels = rng.integers(lo, hi + 1, size=(rows, columns), dtype=np.int64)
    xs = [rng.integers(-(1 << 14), 1 << 14, size=columns, dtype=np.int64) for _ in range(batch)]
    lines_x = []
    for token in range(batch):
        for chunk in range(chunks):
            word = 0
            for k in range(macs):
                word |= (int(xs[token][chunk * macs + k]) & 0xFFFF) << (16 * k)
            lines_x.append(f"{word:0{macs * 4}x}")
    (tmp_path / "x.hex").write_text("\n".join(lines_x) + "\n")
    packed = rom.to_words(levels, macs, bits)
    lines = rom.hexfile(packed, macs, bits)
    (tmp_path / "rom.hex").write_text("\n".join(lines) + "\n")
    addr_bits = max(2, (len(lines) - 1).bit_length())
    per = 1 << (addr_bits - 2)
    for bank in range(4):
        chunk = lines[bank * per : (bank + 1) * per]
        chunk += ["0" * len(lines[0])] * (per - len(chunk))
        (tmp_path / f"rom_bank{bank}.hex").write_text("\n".join(chunk) + "\n")
    mask = (1 << 32) - 1
    for token in range(batch):
        expected = np.array([int((levels[r] * xs[token]).sum()) for r in range(rows)])
        (tmp_path / f"expected_{token}.hex").write_text("\n".join(f"{int(v) & mask:08x}" for v in expected) + "\n")
    for name in ("tile_engine_b.v", "tile_engine.v", "tile_rom.v", "tile_mac.v"):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(Path(__file__).with_name("tile_engine_b_tb.v"), tmp_path / "tile_engine_b_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_engine_b_tb",
        f"-GMACS={macs}",
        f"-GBITS={bits}",
        f"-GGROUPS={groups}",
        f"-GROWS={rows}",
        f"-GCHUNKS={chunks}",
        f"-GLOG2CHUNKS={max(1, (chunks - 1).bit_length())}",
        f"-GLOG2ROWS={max(1, (rows - 1).bit_length())}",
        f"-GLOG2ADDR={max(2, (rows * chunks - 1).bit_length())}",
        f"-GLOG2MACS={max(1, (macs - 1).bit_length())}",
        f"-GB={batch}",
        f"-GLOG2B={max(1, (batch - 1).bit_length())}",
        "tile_engine_b_tb.v",
        "tile_engine_b.v",
        "tile_engine.v",
        "tile_rom.v",
        "tile_mac.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run(
        [str(tmp_path / "obj_dir" / "Vtile_engine_b_tb")], cwd=tmp_path, capture_output=True, text=True
    )


@pytest.mark.parametrize("macs,rows,chunks,bits,groups,batch", [(16, 4, 2, 4, 8, 2), (8, 3, 2, 4, 4, 2)])
def test_a_batch_shares_one_weight_stream(tmp_path, macs, rows, chunks, bits, groups, batch):
    result = _run_batch(tmp_path, macs, rows, chunks, bits, groups, batch, seed=macs * 3 + rows)
    assert "PASS" in result.stdout, result.stdout + result.stderr
