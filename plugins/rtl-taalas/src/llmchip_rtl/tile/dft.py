"""DFT readiness audit of a generated tile bundle.

The R4 DFT item is an audit, not a scan-insertion or MBIST run: every sequential block must have
the active-low reset, every clock must be one of the two documented domains, combinational blocks
must not infer latches, and `initial` blocks may only initialise ROM/LUT contents. The negative
controls in `tests/test_dft.py` feed a gated clock, a latch and a non-ROM initial block through the
same checker.
"""

from __future__ import annotations

import re
from pathlib import Path

ALLOWED_CLOCKS = {"clk", "host_sclk"}
SEQUENTIAL = re.compile(r"always\s*@\s*\(([^)]*)\)")
POSEDGE = re.compile(r"posedge\s+([A-Za-z_][A-Za-z0-9_]*)")
INITIAL = re.compile(r"\binitial\b")


def _issues_for_text(name: str, text: str) -> list[dict]:
    issues: list[dict] = []
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        for match in SEQUENTIAL.finditer(line):
            sensitivity = match.group(1)
            edges = POSEDGE.findall(sensitivity)
            for clock in edges:
                if clock not in ALLOWED_CLOCKS:
                    issues.append({"file": name, "line": number, "kind": "gated-clock", "detail": f"clock {clock}"})
            if edges and "rst_n" not in sensitivity:
                tail = " ".join(lines[number - 1 : number + 6])
                # RAM/ROM write ports are intentionally not reset; everything else needs rst_n.
                if not re.search(r"\b\w*(?:mem|rows|ram)\s*\[", tail):
                    issues.append(
                        {"file": name, "line": number, "kind": "missing-reset", "detail": sensitivity.strip()}
                    )
        stripped = line.strip()
        # A nonblocking assignment in a combinational block is the latch idiom in this code base.
        if (stripped.startswith("always @*") or stripped.startswith("always @(*)")) and "<=" in stripped:
            issues.append({"file": name, "line": number, "kind": "latch", "detail": stripped})
        if INITIAL.search(line):
            tail = " ".join(lines[number - 1 : number + 4])
            if "$readmemh" not in tail and "$readmemb" not in tail:
                issues.append({"file": name, "line": number, "kind": "initial-not-rom", "detail": stripped})
    return issues


def audit(bundle: Path) -> dict:
    files = sorted((bundle / "rtl").glob("*.v"))
    issues: list[dict] = []
    ffs = 0
    rom_init = 0
    scan_ports = 0
    clocks: set[str] = set()
    for path in files:
        text = path.read_text()
        issues.extend(_issues_for_text(path.name, text))
        ffs += len(POSEDGE.findall(text))
        rom_init += text.count("$readmemh") + text.count("$readmemb")
        scan_ports += len(re.findall(r"\b(?:scan_en|scan_in|scan_out|test_mode|mbist)\b", text))
        clocks.update(POSEDGE.findall(text))
    return {
        "files": len(files),
        "ffs": ffs,
        "rom_init": rom_init,
        "scan_ports": scan_ports,
        "clocks": sorted(clocks),
        "issues": issues,
        "ok": not issues and bool(files) and ffs > 0,
    }
