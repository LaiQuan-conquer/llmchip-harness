"""Fault adapters: scan-chain insertion and verification on the synthesized S-1 netlist."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from synth import PDKS, synthesize

DFFS = {
    "sky130": [
        "sky130_fd_sc_hd__dfrtp_1",
        "sky130_fd_sc_hd__dfstp_1",
        "sky130_fd_sc_hd__edfxtp_1",
    ],
    "gf180mcuD": [
        "gf180mcu_fd_sc_mcu7t5v0__dffq_1",
        "gf180mcu_fd_sc_mcu7t5v0__dffrnq_1",
        "gf180mcu_fd_sc_mcu7t5v0__dffsnq_1",
    ],
    "ihp-sg13g2": ["sg13g2_dfrbpq_1"],
}


def scan_chain(netlist: Path, out: Path, pdk: str, dffs: list[str] | None = None) -> dict:
    if pdk not in PDKS:
        raise ValueError(f"unknown PDK {pdk!r}")
    lib = Path(PDKS[pdk]["lib"])
    if not lib.exists():
        raise FileNotFoundError(f"liberty file for {pdk} not found: {lib}")
    work = out / "dft"
    work.mkdir(parents=True, exist_ok=True)
    local = work / "netlist.v"
    shutil.copyfile(netlist, local)
    dff = ",".join(dffs if dffs is not None else DFFS[pdk])
    command = [
        "fault",
        "chain",
        "--liberty",
        str(lib),
        "--clock",
        "clk",
        "--reset",
        "rst_n",
        "--reset-active-low",
        "--dff",
        dff,
        str(local),
        "-o",
        "chained.v",
    ]
    result = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=600)
    text = result.stdout + result.stderr
    internal = re.search(r"Internal scan chain successfully constructed\. Length:\s*(\d+)", text)
    boundary = re.search(r"Boundary scan cells successfully chained\. Length:\s*(\d+)", text)
    total = re.search(r"Total scan-chain length:\s*(\d+)", text)
    internal_n = int(internal.group(1)) if internal else 0
    boundary_n = int(boundary.group(1)) if boundary else 0
    total_n = int(total.group(1)) if total else 0
    report = {
        "format": "llmchip-dft-report/1",
        "pdk": pdk,
        "library": str(lib),
        "dffs": dff.split(","),
        "internal": internal_n,
        "boundary": boundary_n,
        "total": total_n,
        "chained": (work / "chained.v").is_file(),
        "ok": internal_n > 0 and total_n > internal_n and (work / "chained.v").is_file(),
        "log": text[-2000:],
    }
    (out / "dft-report.json").write_text(json.dumps(report, indent=2))
    return report


def atpg(bundle: Path, out: Path, pdk: str) -> dict:
    """Stuck-at ATPG on the combinational residual wrapper (the S-1 cut netlist crashes Fault)."""
    if pdk not in PDKS:
        raise ValueError(f"unknown PDK {pdk!r}")
    wrapper = bundle / "rtl" / "tile_residual_dft.v"
    wrapper.write_text(
        "module tile_residual_dft(input clk, input signed [511:0] a, input signed [511:0] b,"
        " output signed [511:0] y);\n"
        "  tile_residual dut (.a(a), .b(b), .y(y));\n"
        "endmodule\n"
    )
    synthesize(bundle, out, pdk, "tile_residual_dft")
    work = out / "atpg"
    work.mkdir(parents=True, exist_ok=True)
    model = work / "cellmodel.v"
    model.write_text("\n".join(Path(path).read_text() for path in PDKS[pdk]["models"]))  # type: ignore[index]
    netlist = out / "syn" / "netlist.v"
    command = [
        "fault",
        "atpg",
        "--clock",
        "clk",
        "--cell-model",
        str(model),
        "--tv-count",
        "20",
        "--min-coverage",
        "90",
        str(netlist),
        "-o",
        "tv.json",
        "--output-coverage-metadata",
        "cov.yml",
    ]
    result = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=600)
    text = result.stdout + result.stderr
    coverage = work / "cov.yml"
    ratio = 0.0
    if coverage.is_file():
        match = re.search(r"ratio:\s*([0-9.eE+-]+)", coverage.read_text())
        ratio = float(match.group(1)) if match else 0.0
    report = {
        "format": "llmchip-atpg-report/1",
        "pdk": pdk,
        "target": "tile_residual_dft",
        "coverage": ratio,
        "vectors": (work / "tv.json").is_file(),
        "ok": ratio >= 0.95,
        "log": text[-2000:],
    }
    (out / "atpg-report.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--netlist", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--pdk", required=True)
    parser.add_argument("--dff", default="")
    parser.add_argument("--mode", choices=["scan", "atpg"], default="scan")
    parser.add_argument("--bundle", default="")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dffs = args.dff.split(",") if args.dff else None
    if args.mode == "atpg":
        print(json.dumps(atpg(Path(args.bundle), out, args.pdk)))
    else:
        print(json.dumps(scan_chain(Path(args.netlist), out, args.pdk, dffs)))


if __name__ == "__main__":
    main()
