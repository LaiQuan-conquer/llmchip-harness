"""R5 synthesis: the S-1 slice on the three PDKs in the pinned EDA image."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from llmchip_arch.slicing import slice_spec
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2
from llmchip_rtl.tile import generator

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path("/root/projects/deepseek-harness/tools")))
PODMAN = shutil.which("podman")
IMAGE = "docker.io/hpretl/iic-osic-tools@sha256:306585de35d198e70ea58950f77147c86f56c698726a19afea9f1c83009557be"
PLUGIN = Path(__file__).resolve().parents[1]
TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"
PDKS = ("sky130", "gf180mcuD", "ihp-sg13g2")


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    loader = WeightLoader([layout], {path.name: path})
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    stats = graph_l2.calibrate(graph, loader.get, [tokens])
    spec, arrays = graph_l2.build(graph, loader.peek, weight_format="int4", group_size=16, stats=stats, context=16)
    out = tmp_path_factory.mktemp("bundle")
    generator.generate(spec, arrays, slice_spec(graph, spec, "S-1"), macs=8, out=out)
    out.chmod(0o777)
    for path in out.rglob("*"):
        path.chmod(0o777 if path.is_dir() else 0o644)
    return out


def _synth(tmp_path, bundle: Path, pdk: str) -> subprocess.CompletedProcess:
    out = tmp_path / f"out-{pdk}"
    out.mkdir()
    out.chmod(0o777)
    command = [
        str(PODMAN),
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{bundle}:/bundle:ro,Z",
        "-v",
        f"{PLUGIN}:/plugin:ro,Z",
        "-v",
        f"{out}:/out:rw,Z",
        IMAGE,
        "--skip",
        "python3",
        "/plugin/synth.py",
        "--bundle",
        "/bundle",
        "--out",
        "/out",
        "--pdk",
        pdk,
    ]
    return subprocess.run(command, capture_output=True, text=True, timeout=600)


pytestmark = pytest.mark.skipif(PODMAN is None, reason="podman is not available")


def test_the_three_pdks_synthesize_the_slice(bundle, tmp_path):
    for pdk in PDKS:
        result = _synth(tmp_path, bundle, pdk)
        assert result.returncode == 0, result.stdout + result.stderr
        lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
        assert lines, result.stdout + result.stderr
        report = json.loads(lines[-1])
        assert report["pdk"] == pdk
        assert report["cells"] > 1000
        assert report["area"] > 0
        assert report["flipFlops"] > 0
        assert (tmp_path / f"out-{pdk}" / "syn" / "netlist.v").is_file()


def test_an_unknown_pdk_is_rejected(bundle, tmp_path):
    result = _synth(tmp_path, bundle, "nope")
    assert result.returncode != 0


def _dft(tmp_path, netlist: Path, pdk: str, dff: str = "") -> subprocess.CompletedProcess:
    out = tmp_path / f"dft-{pdk}-{dff or 'ok'}"
    out.mkdir()
    out.chmod(0o777)
    command = [
        str(PODMAN),
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{netlist}:/netlist.v:ro,Z",
        "-v",
        f"{PLUGIN}:/plugin:ro,Z",
        "-v",
        f"{out}:/out:rw,Z",
        IMAGE,
        "--skip",
        "python3",
        "/plugin/dft.py",
        "--netlist",
        "/netlist.v",
        "--out",
        "/out",
        "--pdk",
        pdk,
    ]
    if dff:
        command += ["--dff", dff]
    return subprocess.run(command, capture_output=True, text=True, timeout=600)


def _atpg(tmp_path, bundle: Path, pdk: str) -> subprocess.CompletedProcess:
    out = tmp_path / f"atpg-{pdk}"
    out.mkdir()
    out.chmod(0o777)
    command = [
        str(PODMAN),
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{bundle}:/bundle:rw,Z",
        "-v",
        f"{PLUGIN}:/plugin:ro,Z",
        "-v",
        f"{out}:/out:rw,Z",
        IMAGE,
        "--skip",
        "python3",
        "/plugin/dft.py",
        "--mode",
        "atpg",
        "--bundle",
        "/bundle",
        "--out",
        "/out",
        "--pdk",
        pdk,
    ]
    return subprocess.run(command, capture_output=True, text=True, timeout=600)


def test_stuck_at_atpg_reaches_95_percent(bundle, tmp_path):
    for pdk in PDKS:
        result = _atpg(tmp_path, bundle, pdk)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads([line for line in result.stdout.splitlines() if line.startswith("{")][-1])
        assert report["coverage"] >= 0.95, (pdk, report)
        assert report["ok"] is True


def test_a_tied_output_fails_atpg(bundle, tmp_path):
    bad = tmp_path / "bad-bundle"
    shutil.copytree(bundle, bad)
    wrapper = bad / "rtl" / "tile_residual_dft.v"
    if wrapper.exists():
        wrapper.unlink()
    (bad / "rtl" / "tile_residual.v").write_text(
        "module tile_residual(input signed [511:0] a, input signed [511:0] b,"
        " output signed [511:0] y); assign y = 512'd0; endmodule\n"
    )
    result = _atpg(tmp_path, bad, "sky130")
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines, result.stdout + result.stderr
    report = json.loads(lines[-1])
    assert report["coverage"] < 0.95
    assert report["ok"] is False


def _timing(tmp_path, netlist: Path, pdk: str, period: float) -> subprocess.CompletedProcess:
    out = tmp_path / f"timing-{pdk}-{period}"
    out.mkdir()
    out.chmod(0o777)
    command = [
        str(PODMAN),
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{netlist}:/netlist.v:ro,Z",
        "-v",
        f"{PLUGIN}:/plugin:ro,Z",
        "-v",
        f"{out}:/out:rw,Z",
        IMAGE,
        "--skip",
        "python3",
        "/plugin/timing.py",
        "--netlist",
        "/netlist.v",
        "--out",
        "/out",
        "--pdk",
        pdk,
        "--period",
        str(period),
    ]
    return subprocess.run(command, capture_output=True, text=True, timeout=600)


def test_timing_meets_a_100ns_clock_and_rejects_a_1ns_clock(bundle, tmp_path):
    result = _synth(tmp_path, bundle, "sky130")
    assert result.returncode == 0, result.stderr
    netlist = tmp_path / "out-sky130" / "syn" / "netlist.v"
    fast = _timing(tmp_path, netlist, "sky130", 100)
    slow = _timing(tmp_path, netlist, "sky130", 1)
    fast_report = json.loads([line for line in fast.stdout.splitlines() if line.startswith("{")][-1])
    slow_report = json.loads([line for line in slow.stdout.splitlines() if line.startswith("{")][-1])
    assert fast_report["ok"] is True
    assert fast_report["wns"] >= 0
    assert slow_report["ok"] is False
    assert slow_report["wns"] < 0


def test_the_scan_chain_covers_every_internal_flop(bundle, tmp_path):
    result = _synth(tmp_path, bundle, "sky130")
    assert result.returncode == 0, result.stderr
    netlist = tmp_path / "out-sky130" / "syn" / "netlist.v"
    dft = _dft(tmp_path, netlist, "sky130")
    assert dft.returncode == 0, dft.stdout + dft.stderr
    lines = [line for line in dft.stdout.splitlines() if line.startswith("{")]
    report = json.loads(lines[-1])
    assert report["internal"] == 1553
    assert report["total"] > report["internal"]
    assert report["ok"] is True


@pytest.mark.parametrize("pdk", ["gf180mcuD", "ihp-sg13g2"])
def test_other_pdk_scan_chain_rejects_a_wrong_dff_list(bundle, tmp_path, pdk):
    result = _synth(tmp_path, bundle, pdk)
    assert result.returncode == 0, result.stderr
    netlist = tmp_path / f"out-{pdk}" / "syn" / "netlist.v"
    dft = _dft(tmp_path, netlist, pdk, "bogus_dff")
    lines = [line for line in dft.stdout.splitlines() if line.startswith("{")]
    assert lines, dft.stdout + dft.stderr
    report = json.loads(lines[-1])
    assert report["ok"] is False
    assert report["internal"] == 0
