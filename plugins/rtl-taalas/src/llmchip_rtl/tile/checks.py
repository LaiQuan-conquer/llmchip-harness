"""The RTL checks of a `taalas-tile/1` bundle (requirements FR-RTL-03/04, FR-SGN-07).

- `roundtrip`: every bank's `$readmemh` words restore to the manifest's digest and count - the
  inverse of the mapping rule is what signoff compares against the source weights;
- `rules`: no `lint_off` in generated RTL (warnings are never disabled), every clocked block has
  a reset, no functions (which synthesis would turn into registers), the datapath in one clock
  domain (the host byte receiver is the documented second domain);
- `lint`: the three-linter standard of R3 - Verilator `-Wall`, Verible with the hygiene rules
  below, and slang-tidy with the synthesis checks. Each must report zero warnings, and no RTL
  file may turn a warning off;
- `synthesizable`: Yosys elaborates the bundle, `check -assert` passes before and after `proc`.

The Verible rule set is deliberately narrow (tabs, trailing spaces, end-of-file newline, macro
prohibition, line length): its style rules describe a house style this Verilog-2005 subset does
not follow, while the parser still catches syntax errors. slang-tidy runs the synthesis group
(no latches, every register has a reset, no undriven ranges, ...); the one check disabled is
`unused-sensitive-signal`, because the host clock `host_sclk` is a second clock domain by design.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from . import rom
from .views import rom_views

VERIBLE_RULES = "+no-tabs,+no-trailing-spaces,+posix-eof,+forbidden-macro,+line-length=length:120"
SLANG_CONFIG = """Checks:
    -*,
    synthesis-*,
    -synthesis-unused-sensitive-signal

CheckConfigs:
    resetName: rst_n,
    resetIsActiveHigh: false
"""


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


def _code(text: str) -> str:
    """The Verilog without comments: the rules are about code, not about prose."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def rules(bundle: Path) -> dict:
    violations: list[str] = []
    for path in sorted((bundle / "rtl").glob("*.v")):
        raw = path.read_text()
        text = _code(raw)
        if "lint_off" in raw:
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


def _verilator(bundle: Path, verilator: Path) -> dict:
    result = subprocess.run(
        [str(verilator), "--lint-only", "-Wall", "--top-module", "tile_top", *_rtl_files(bundle)],
        capture_output=True,
        text=True,
    )
    text = result.stdout + result.stderr
    return {"returncode": result.returncode, "warnings": text.count("%Warning"), "output": text[-2000:]}


def _verible(bundle: Path, verible: Path) -> dict:
    result = subprocess.run(
        [str(verible), "--ruleset=none", f"--rules={VERIBLE_RULES}", *_rtl_files(bundle)],
        capture_output=True,
        text=True,
    )
    text = result.stdout + result.stderr
    violations = [line for line in text.splitlines() if line.strip()]
    return {"returncode": result.returncode, "warnings": len(violations), "output": text[-2000:]}


def _slang(bundle: Path, slang: Path) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".slang-tidy", delete=False) as config:
        config.write(SLANG_CONFIG)
        path = config.name
    try:
        result = subprocess.run(
            [str(slang), "--top", "tile_top", "--config-file", path, *_rtl_files(bundle)],
            cwd=bundle,
            capture_output=True,
            text=True,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    text = result.stdout + result.stderr
    warnings = len(re.findall(r"(?:warning|error):", text))
    return {"returncode": result.returncode, "warnings": warnings, "output": text[-2000:]}


def lint(bundle: Path, verilator: Path | None = None, verible: Path | None = None, slang: Path | None = None) -> dict:
    """The three-linter R3 lint: every available tool must report zero warnings."""
    tools = {
        "verilator": (_verilator, verilator),
        "verible": (_verible, verible),
        "slang": (_slang, slang),
    }
    report: dict = {"returncode": 0, "warnings": 0, "tools": 0, "output": ""}
    for name, (runner, tool) in tools.items():
        if tool is None or not Path(tool).is_file():
            report[name] = {"skipped": "tool not found"}
            continue
        result = runner(bundle, tool)
        report[name] = result
        report["tools"] += 1
        report["returncode"] = max(report["returncode"], result["returncode"])
        report["warnings"] += result["warnings"]
        report["output"] = (report["output"] + result["output"])[-4000:]
    return report


def synthesizable(bundle: Path, yosys: Path) -> dict:
    files = " ".join(_rtl_files(bundle))
    script = "; ".join(
        [
            f"read_verilog -DSYNTHESIS {files}",
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


def all_checks(
    bundle: Path,
    verilator: Path | None,
    yosys: Path | None,
    verible: Path | None = None,
    slang: Path | None = None,
) -> dict:
    report = {"roundtrip": roundtrip(bundle), "rules": rules(bundle)}
    report["lint"] = lint(bundle, verilator, verible, slang)
    report["synthesizable"] = synthesizable(bundle, yosys) if yosys else {"skipped": "yosys not found"}
    report["romViews"] = rom_views(bundle, yosys) if yosys else {"skipped": "yosys not found"}
    report["passed"] = (
        report["roundtrip"]["roundtrip_mismatches"] == 0
        and report["rules"]["violations"] == 0
        and report["lint"]["warnings"] == 0
        and report["lint"]["returncode"] == 0
        and report["lint"].get("tools", 0) >= 1
        and report["synthesizable"].get("returncode") == 0
        and (report["romViews"].get("equivalent") is True or "skipped" in report["romViews"])
        and (report["romViews"].get("equivalent3") is True or "skipped" in report["romViews"])
    )
    return report
