"""Host frames of taalas-tile/1 (drafts/Qwen05B_Taalas_Refactor_Plan_zh.md §12.1 B2): TOKEN,
LENGTH, ADDR, START, the refusal of START while busy and of an illegal frame, CLEAR, and the status
byte read back over MISO. The observation word is a parallel readback addressed by ADDR."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path("/root/projects/deepseek-harness/tools")))
VERILATOR = TOOLS / "oss-cad-suite" / "bin" / "verilator"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"
TB = Path(__file__).with_name("tile_host_tb.v")


@pytest.mark.skipif(not VERILATOR.is_file(), reason="Verilator is not in the tools directory")
def test_host_frames_and_refusals(tmp_path):
    for name in ("tile_host_tb.v", "tile_host.v"):
        source = TB if name == "tile_host_tb.v" else TEMPLATES / name
        shutil.copyfile(source, tmp_path / name)
    command = [
        str(VERILATOR),
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tile_host_tb",
        "tile_host_tb.v",
        "tile_host.v",
    ]
    build = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(tmp_path / "obj_dir" / "Vtile_host_tb")], cwd=tmp_path, capture_output=True, text=True)
    assert "PASS" in run.stdout, run.stdout + run.stderr
