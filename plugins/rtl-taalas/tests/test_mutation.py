"""MCY mutation score of the residual add, with equivalence-classified survivors."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path("/root/projects/deepseek-harness/tools")))
MCY = TOOLS / "oss-cad-suite" / "bin" / "mcy"
PROJECT = Path(__file__).resolve().parents[1] / "mutation" / "tile_residual"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "llmchip_rtl" / "tile" / "templates"


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = str(TOOLS / "oss-cad-suite" / "bin") + os.pathsep + env.get("PATH", "")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


pytestmark = pytest.mark.skipif(not MCY.is_file(), reason="MCY is not in the tools directory")


def test_the_residual_mutation_score_is_at_least_95_percent(tmp_path):
    for name in ("config.mcy", "test_sim.sh", "test_eq.sh", "test_eq.ys", "tile_residual_tb.v"):
        shutil.copyfile(PROJECT / name, tmp_path / name)
    (tmp_path / "test_sim.sh").chmod(0o755)
    (tmp_path / "test_eq.sh").chmod(0o755)
    shutil.copyfile(TEMPLATES / "tile_residual.v", tmp_path / "tile_residual.v")
    init = subprocess.run(
        [str(MCY), "init", "-f"], cwd=tmp_path, capture_output=True, text=True, timeout=120, env=_env()
    )
    assert init.returncode == 0, init.stdout + init.stderr
    run = subprocess.run(
        [str(MCY), "run", "-j", "4"], cwd=tmp_path, capture_output=True, text=True, timeout=600, env=_env()
    )
    text = run.stdout + run.stderr
    match = re.search(r"Coverage: ([0-9.]+)%", text)
    assert match is not None, text
    score = float(match.group(1))
    print(f"MUTATION score={score:.1f}", file=__import__("sys").__stdout__)
    assert run.returncode == 0
    assert score >= 95.0, text
