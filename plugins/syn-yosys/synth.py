"""Synthesis of a tile bundle with Yosys, inside the pinned iic-osic-tools image."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

PDKS = {
    "sky130": {
        "title": "sky130A sky130_fd_sc_hd",
        "lib": "/foss/pdks/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib",
        "models": [
            "/foss/pdks/sky130A/libs.ref/sky130_fd_sc_hd/verilog/primitives.v",
            "/foss/pdks/sky130A/libs.ref/sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v",
        ],
    },
    "gf180mcuD": {
        "title": "GF180MCU gf180mcu_fd_sc_mcu7t5v0",
        "lib": "/foss/pdks/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu7t5v0/lib/gf180mcu_fd_sc_mcu7t5v0__tt_025C_3v30.lib",
        "models": [
            "/foss/pdks/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu7t5v0/verilog/primitives.v",
            "/foss/pdks/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu7t5v0/verilog/gf180mcu_fd_sc_mcu7t5v0.v",
        ],
    },
    "ihp-sg13g2": {
        "title": "IHP SG13G2 sg13g2_stdcell",
        "lib": "/foss/pdks/ihp-sg13g2/libs.ref/sg13g2_stdcell/lib/sg13g2_stdcell_typ_1p20V_25C.lib",
        "models": [
            "/foss/pdks/ihp-sg13g2/libs.ref/sg13g2_stdcell/verilog/sg13g2_udp.v",
            "/foss/pdks/ihp-sg13g2/libs.ref/sg13g2_stdcell/verilog/sg13g2_stdcell.v",
        ],
    },
}


def _parse_stat(text: str) -> dict:
    area = re.search(r"Chip area for module '\\?[^']+':\s*([0-9.]+)", text)
    cells = re.search(r"^\s*([0-9]+)\s+[0-9.E+]+\s+cells\s*$", text, re.M)
    dffs = re.findall(r"^\s*([0-9]+)\s+[0-9.E+]+\s+(\S*(?:df|edf|dlxtp)\S*)\s*$", text, re.M)
    return {
        "area": float(area.group(1)) if area else 0.0,
        "cells": int(cells.group(1)) if cells else 0,
        "flipFlops": sum(int(count) for count, _ in dffs),
    }


def synthesize(bundle: Path, out: Path, pdk: str, top: str = "tile_slice_top") -> dict:
    if pdk not in PDKS:
        raise ValueError(f"unknown PDK {pdk!r}; expected one of {sorted(PDKS)}")
    lib = Path(PDKS[pdk]["lib"])
    if not lib.exists():
        raise FileNotFoundError(f"liberty file for {pdk} not found: {lib}")
    work = out / "syn"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    for source in sorted((bundle / "rtl").glob("*.v")):
        shutil.copyfile(source, work / source.name)
    for source in sorted(bundle.glob("*.hex")):
        shutil.copyfile(source, work / source.name)
    files = " ".join(sorted(path.name for path in work.glob("*.v")))
    script = (
        f"read_verilog {files}\n"
        f"synth -top {top} -flatten\n"
        f"dfflibmap -liberty {lib}\n"
        f"abc -liberty {lib}\n"
        "opt_clean\n"
        f"tee -o stat.txt stat -liberty {lib}\n"
        "write_verilog -noattr netlist.v\n"
    )
    (work / "synth.ys").write_text(script)
    result = subprocess.run(["yosys", "-q", "-s", "synth.ys"], cwd=work, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)
    stat = (work / "stat.txt").read_text() if (work / "stat.txt").exists() else ""
    metrics = _parse_stat(stat)
    report = {
        "format": "llmchip-syn-report/1",
        "pdk": pdk,
        "library": str(lib),
        "top": top,
        "area": metrics["area"],
        "cells": metrics["cells"],
        "flipFlops": metrics["flipFlops"],
        "netlist": "netlist.v",
        "stat": stat[-4000:],
    }
    (out / "syn-report.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--pdk", required=True)
    parser.add_argument("--top", default="tile_slice_top")
    args = parser.parse_args()
    print(json.dumps(synthesize(Path(args.bundle), Path(args.out), args.pdk, args.top)))


if __name__ == "__main__":
    main()
