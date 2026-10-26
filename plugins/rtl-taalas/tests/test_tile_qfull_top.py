"""`tile_qfull_top`: one engine per layer, serialized and pipelined (draft §12.2).

Every layer reads the same bank files and the same activation vector, so the last layer's rows are
the reference matvec rows for both handshakes. The serial version releases layer i+1 on layer i's
`done`; the pipelined one releases it on layer i's first `y_valid`. The host writes the activation
lanes once and all engines receive that write stream.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from llmchip_rtl.tile import rom

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path("/root/projects/deepseek-harness/tools")))
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"
TB = Path(__file__).with_name("tile_qfull_top_tb.v")


def _run(tmp_path, layers, pipelined, macs, rows, chunks, bits, groups, seed):
    rng = np.random.default_rng(seed)
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    columns = chunks * macs
    levels = rng.integers(lo, hi + 1, size=(rows, columns), dtype=np.int64)
    x = rng.integers(-(1 << 14), 1 << 14, size=columns, dtype=np.int64)
    expected = np.array([int((levels[r] * x).sum()) for r in range(rows)])

    (tmp_path / "x.hex").write_text("\n".join(f"{int(value) & 0xFFFF:04x}" for value in x) + "\n")
    (tmp_path / "expected.hex").write_text("\n".join(f"{int(value) & 0xFFFFFFFF:08x}" for value in expected) + "\n")
    lines = rom.hexfile(rom.to_words(levels, macs, bits), macs, bits)
    (tmp_path / "rom.hex").write_text("\n".join(lines) + "\n")
    addr_bits = max(2, (len(lines) - 1).bit_length())
    per = 1 << (addr_bits - 2)
    for bank in range(4):
        chunk = lines[bank * per : (bank + 1) * per]
        chunk += ["0" * len(lines[0])] * (per - len(chunk))
        (tmp_path / f"rom_bank{bank}.hex").write_text("\n".join(chunk) + "\n")

    for name in ("tile_qfull_top.v", "tile_engine.v", "tile_rom.v", "tile_mac.v"):
        shutil.copyfile(TEMPLATES / name, tmp_path / name)
    shutil.copyfile(TB, tmp_path / "tile_qfull_top_tb.v")
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_qfull_top_tb",
        f"-GLAYERS={layers}",
        f"-GPIPELINED={pipelined}",
        f"-GMACS={macs}",
        f"-GBITS={bits}",
        f"-GGROUPS={groups}",
        f"-GROWS={rows}",
        f"-GCHUNKS={chunks}",
        f"-GLOG2CHUNKS={max(1, (chunks - 1).bit_length())}",
        f"-GLOG2ROWS={max(1, (rows - 1).bit_length())}",
        f"-GLOG2ADDR={addr_bits}",
        f"-GLOG2MACS={max(1, (macs - 1).bit_length())}",
        "tile_qfull_top_tb.v",
        "tile_qfull_top.v",
        "tile_engine.v",
        "tile_rom.v",
        "tile_mac.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    if build.returncode != 0:
        return subprocess.CompletedProcess(command, 1, build.stdout + build.stderr, "")
    return subprocess.run(
        [str(tmp_path / "obj_dir" / "Vtile_qfull_top_tb")], cwd=tmp_path, capture_output=True, text=True
    )


pytestmark = pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")


@pytest.mark.parametrize("layers,pipelined", [(2, 0), (3, 0), (2, 1), (3, 1)])
def test_the_layer_chain_matches_the_reference(tmp_path, layers, pipelined):
    result = _run(tmp_path, layers, pipelined, macs=8, rows=3, chunks=2, bits=4, groups=4, seed=7 * layers + pipelined)
    assert "PASS" in result.stdout, result.stdout + result.stderr


def test_a_wider_layer_chain_matches_the_reference(tmp_path):
    result = _run(tmp_path, 2, 1, macs=16, rows=3, chunks=2, bits=8, groups=8, seed=71)
    assert "PASS" in result.stdout, result.stdout + result.stderr
