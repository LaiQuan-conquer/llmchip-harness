"""OpenSTA timing of the synthesized S-1 netlist."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from synth import PDKS


def timing(netlist: Path, out: Path, pdk: str, period: float = 100.0) -> dict:
    if pdk not in PDKS:
        raise ValueError(f"unknown PDK {pdk!r}")
    lib = Path(PDKS[pdk]["lib"])
    if not lib.exists():
        raise FileNotFoundError(f"liberty file for {pdk} not found: {lib}")
    work = out / "timing"
    work.mkdir(parents=True, exist_ok=True)
    stripped = work / "netlist.v"
    stripped.write_text(re.sub(r"\bsigned\s+", "", netlist.read_text()))
    script = f"""read_liberty {lib}
read_verilog {stripped.name}
link_design tile_slice_top
create_clock -name clk -period {period} [get_ports clk]
set_output_delay 10 -clock clk [all_outputs]
report_worst_slack -max
report_tns -max
exit
"""
    (work / "sta.tcl").write_text(script)
    result = subprocess.run(
        ["sta", "-no_init", "-exit", "sta.tcl"], cwd=work, capture_output=True, text=True, timeout=300
    )
    text = result.stdout + result.stderr
    wns = re.search(r"worst slack max\s+(-?[0-9.]+)", text)
    tns = re.search(r"tns max\s+(-?[0-9.]+)", text)
    report = {
        "format": "llmchip-timing-report/1",
        "pdk": pdk,
        "period": period,
        "wns": float(wns.group(1)) if wns else 0.0,
        "tns": float(tns.group(1)) if tns else 0.0,
        "ok": bool(wns and float(wns.group(1)) >= 0.0),
        "log": text[-2000:],
    }
    (out / "timing-report.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--netlist", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--pdk", required=True)
    parser.add_argument("--period", type=float, default=100.0)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(json.dumps(timing(Path(args.netlist), out, args.pdk, args.period)))


if __name__ == "__main__":
    main()
