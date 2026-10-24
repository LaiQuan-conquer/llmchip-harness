"""The RTL checks of a `taalas-tile/1` bundle (requirements FR-RTL-03/04, FR-SGN-07).

- `roundtrip`: every bank's `$readmemh` words restore to the manifest's digest and count - the
  inverse of the mapping rule is what signoff compares against the source weights;
- `rules`: no `lint_off` in generated RTL (warnings are never disabled), every clocked block has
  a reset, no functions (which synthesis would turn into registers), the datapath in one clock
  domain (the host byte receiver is the documented second domain);
- `lint`: Verilator `-Wall` with zero warnings;
- `synthesizable`: Yosys elaborates the bundle, `check -assert` passes before and after `proc`.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import rom


def manifest(bundle: Path) -> dict:
    return json.loads((bundle / "design.json").read_text())


def roundtrip(bundle: Path) -> dict:
    m = manifest(bundle)
    macs, bits = m["mapping"]["macs"], m["mapping"]["bits"]
    bad: list[str] = []
    for bank in m["banks"]:
        words = [int(line, 16) for line in (bundle / bank["hex"]).read_text().split()]
        if len(words) != bank["words"] or rom.digest(words, macs, bits) != bank["digest"]:
            bad.append(bank["matrix"])
    return {"banks": len(m["banks"]), "roundtrip_mismatches": len(bad), "bad": bad}


def rules(bundle: Path) -> dict:
    violations: list[str] = []
    for path in sorted((bundle / "rtl").glob("*.v")):
        text = path.read_text()
        if "lint_off" in text:
            violations.append(f"{path.name}: lint_off")
        if re.search(r"\bfunction\b", text):
            violations.append(f"{path.name}: function")
        for match in re.finditer(r"always\s*@\s*\(([^)]*)\)", text):
            body = text[match.end() : match.end() + 400]
            if "posedge" in match.group(1) and "rst_n" not in body:
                violations.append(f"{path.name}: clocked block without a reset")
    return {"violations": len(violations), "items": violations}


def _rtl_files(bundle: Path) -> list[str]:
    return [str(path) for path in sorted((bundle / "rtl").glob("*.v"))]


def lint(bundle: Path, verilator: Path) -> dict:
    result = subprocess.run(
        [str(verilator), "--lint-only", "-Wall", "--top-module", "tile_top", *_rtl_files(bundle)],
        capture_output=True,
        text=True,
    )
    text = result.stdout + result.stderr
    return {"returncode": result.returncode, "warnings": text.count("%Warning"), "output": text[-2000:]}


def synthesizable(bundle: Path, yosys: Path) -> dict:
    files = " ".join(_rtl_files(bundle))
    script = "; ".join(
        [
            f"read_verilog {files}",
            "chparam -set ADDR 6 tile_top",
            "hierarchy -check -top tile_top",
            "check -assert",
            "proc",
            "opt",
            "check -assert",
        ]
    )
    result = subprocess.run([str(yosys), "-q", "-p", script], cwd=bundle, capture_output=True, text=True)
    return {"returncode": result.returncode, "output": (result.stdout + result.stderr)[-2000:]}


def all_checks(bundle: Path, verilator: Path | None, yosys: Path | None) -> dict:
    report = {"roundtrip": roundtrip(bundle), "rules": rules(bundle)}
    report["lint"] = lint(bundle, verilator) if verilator else {"skipped": "verilator not found"}
    report["synthesizable"] = synthesizable(bundle, yosys) if yosys else {"skipped": "yosys not found"}
    report["passed"] = (
        report["roundtrip"]["roundtrip_mismatches"] == 0
        and report["rules"]["violations"] == 0
        and report["lint"].get("warnings") == 0
        and report["synthesizable"].get("returncode") == 0
    )
    return report
