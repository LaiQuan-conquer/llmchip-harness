"""Formal BMC of the residual add, with a deliberately wrong module as the negative control."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path("/root/projects/deepseek-harness/tools")))
SBY = TOOLS / "oss-cad-suite" / "bin" / "sby"
FORMAL = Path(__file__).resolve().parents[1] / "formal"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"

BAD = """`default_nettype none
module tile_residual #(parameter integer DIM = 32, parameter integer WIDTH = 16) (
  input wire signed [DIM*WIDTH-1:0] a, input wire signed [DIM*WIDTH-1:0] b,
  output wire signed [DIM*WIDTH-1:0] y);
  genvar lane;
  generate for (lane = 0; lane < DIM; lane = lane + 1) begin : g_lane
    wire signed [WIDTH:0] sum = $signed(a[lane*WIDTH +: WIDTH]) + $signed(b[lane*WIDTH +: WIDTH]);
    assign y[lane*WIDTH +: WIDTH] = sum[WIDTH-1:0];
  end endgenerate
endmodule
"""


def _run(tmp_path, residual: str) -> subprocess.CompletedProcess:
    (tmp_path / "tile_residual.v").write_text(residual)
    shutil.copyfile(FORMAL / "tile_residual_formal.v", tmp_path / "tile_residual_formal.v")
    shutil.copyfile(FORMAL / "tile_residual.sby", tmp_path / "tile_residual.sby")
    return subprocess.run(
        [str(SBY), "-f", "tile_residual.sby"], cwd=tmp_path, capture_output=True, text=True, timeout=180
    )


pytestmark = pytest.mark.skipif(not SBY.is_file(), reason="SymbiYosys is not in the tools directory")


def test_the_residual_formal_property_passes(tmp_path):
    result = _run(tmp_path, (TEMPLATES / "tile_residual.v").read_text())
    assert result.returncode == 0 and "DONE (PASS" in result.stdout + result.stderr, result.stdout + result.stderr


def test_a_missing_saturation_is_caught(tmp_path):
    result = _run(tmp_path, BAD)
    assert result.returncode != 0
    assert "DONE (FAIL" in result.stdout + result.stderr, result.stdout + result.stderr
