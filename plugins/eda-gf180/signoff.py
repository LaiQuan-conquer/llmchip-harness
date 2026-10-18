"""Signoff checks on GF180MCU (S7, requirements FR-SGN-01/02/02a/02b).

Each check runs on exactly the artifacts it judges (the GDS, the netlist and
parasitics of a layout bundle, the constraints), so its verdict is evidence for
those hashes. DRC, antenna, density and LVS reuse the LibreLane steps of the
PDK (KLayout and Magic DRC decks, Netgen) through LibreLane's step runner on a
state built from the inputs. STA runs OpenSTA per signoff corner with the
extracted parasitics and also proves the constraints complete.
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from pathlib import Path

import constraints
import physical


def extract(bundle: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle) as tar:
        tar.extractall(dest)
    return dest


def librelane_steps(layout: Path, work: Path, tag: str, first: str, last: str, state: dict, overrides: dict) -> dict:
    """Runs the steps first..last of the Chip flow on the given initial state; returns
    the final metrics. The design directory and configuration come from the layout bundle;
    each step range gets its own copy, so its reports stay in place."""
    design = work / tag / "design"
    if design.exists():
        shutil.rmtree(design)
    shutil.copytree(layout / "design", design)
    config = json.loads((design / "config.json").read_text())
    (design / "config.json").write_text(json.dumps(config | overrides, indent=2))
    (work / f"{tag}.state.json").write_text(json.dumps(state))
    physical.librelane(
        design, tag, "--from", first, "--to", last, "--with-initial-state", str(work / f"{tag}.state.json")
    )
    return physical.final_state(design / "runs" / tag).get("metrics", {})


def drc(gds: str, layout: str, rules: dict, work: Path) -> dict:
    """KLayout DRC (decks from the signoff rules), Magic DRC, KLayout antenna and density on the GDS."""
    lay = extract(layout, work / "layout")
    shutil.copy(gds, work / "chip_top.gds")
    state = {"gds": str(work / "chip_top.gds"), "def": str(lay / "views" / "chip_top.def")}
    no_stop = {
        "ERROR_ON_MAGIC_DRC": False,
        "ERROR_ON_KLAYOUT_DRC": False,
        "RUN_MAGIC_DRC": bool(rules.get("magicDrc", True)),
        "RUN_KLAYOUT_DRC": True,
        "KLAYOUT_DRC_OPTIONS": {"variant": rules["klayoutDrc"]["variant"], "decks": rules["klayoutDrc"]["decks"]},
    }
    m = librelane_steps(lay, work, "drc", "Magic.DRC", "Checker.KLayoutDRC", state, no_stop)
    a = librelane_steps(lay, work, "antenna", "KLayout.Antenna", "KLayout.Antenna", state, {})
    d = librelane_steps(lay, work, "density", "KLayout.Density", "KLayout.Density", state, {})
    found = markers(work / "drc" / "design" / "runs" / "drc", ".lyrdb")
    pdk = Path(physical.PDK_ROOT) / physical.PDK / "libs.ref"
    places = footprints(lay / "views" / "chip_top.def", [pdk / physical.IO / "lef", pdk / physical.SCL / "lef"])
    counted, excluded = classify(found, rules.get("cellExclusions", []), places)
    antenna = markers(work / "antenna" / "design" / "runs" / "antenna", ".lyrdb")
    density = markers(work / "density" / "design" / "runs" / "density", ".lyrdb")
    total = int(m.get("klayout__drc_error__count", -1))
    return {
        "metrics": {
            "magic_drc_errors": int(m.get("magic__drc_error__count", -1)),
            # Every marker of the deck is listed; those an approved exclusion covers are counted apart.
            "klayout_drc_errors": len(counted) if total >= 0 and len(found) == total else total,
            "klayout_drc_excluded": len(excluded),
            "klayout_markers_parsed": len(found) == total,
            "antenna_errors": int(a.get("klayout__antenna_error__count", -1)),
            "density_errors": int(d.get("klayout__density_error__count", -1)),
            "magic_illegal_overlaps": int(m.get("magic__illegal_overlap__count", 0)),
        },
        "rules": {
            "klayout_drc": tally(counted),
            "klayout_drc_excluded": tally(excluded),
            "antenna": tally(antenna),
            "density": tally(density),
        },
        "markers": {"counted": counted[:2000], "excluded": excluded[:2000]},
    }


def markers(rundir: Path, suffix: str) -> list[dict]:
    """Violation markers of KLayout report databases under a run directory: rule, cell
    (the cell the marker lies in, without the instance orientation) and position."""
    import xml.etree.ElementTree as ET

    out: list[dict] = []
    for f in sorted(rundir.rglob(f"*{suffix}")):
        try:
            items = ET.parse(f).getroot().find("items")
        except ET.ParseError:
            continue
        for it in items if items is not None else []:
            values = " ".join(v.text or "" for v in it.iter("value"))
            nums = [float(n) for n in re.findall(r"-?\d+\.?\d*", values)]
            out.append(
                {
                    "rule": (it.findtext("category") or "?").strip("'"),
                    "cell": (it.findtext("cell") or "?").split(":")[0],
                    "at": [
                        round(sum(nums[0::2]) / max(len(nums[0::2]), 1), 2),
                        round(sum(nums[1::2]) / max(len(nums[1::2]), 1), 2),
                    ],
                }
            )
    return out


def footprints(def_path: Path, lef_dirs: list[Path]) -> list[tuple[str, float, float, float, float]]:
    """(cell, x0, y0, x1, y1) in µm of every placed instance whose LEF size is known."""
    sizes: dict[str, tuple[float, float]] = {}
    for d in lef_dirs:
        for lef in d.glob("*.lef"):
            for name, w, h in re.findall(
                r"MACRO (\S+).*?SIZE ([\d.]+) BY ([\d.]+)", lef.read_text(errors="replace"), re.S
            ):
                sizes.setdefault(name, (float(w), float(h)))
    text = def_path.read_text(errors="replace")
    units = float((re.search(r"UNITS DISTANCE MICRONS (\d+)", text) or [0, 1000])[1])
    out = []
    for cell, x, y, orient in re.findall(
        r"-\s+\S+\s+(\S+).*?\+\s+(?:PLACED|FIXED)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\S+)", text
    ):
        if cell not in sizes:
            continue
        w, h = sizes[cell]
        if orient.lstrip("F") in ("E", "W"):
            w, h = h, w
        x0, y0 = int(x) / units, int(y) / units
        out.append((cell, x0, y0, x0 + w, y0 + h))
    return out


def classify(found: list[dict], exclusions: list[dict], places: list | None = None) -> tuple[list[dict], list[dict]]:
    """Splits markers into counted and excluded ones. An exclusion names rules and cell name
    patterns; with `footprints` it also covers top-level markers inside the placed footprint of
    an instance of those cells. It is part of the signoff rules artifact, so only a human
    introduces it."""
    import fnmatch as f

    def covers(e: dict, m: dict) -> bool:
        if m["rule"] not in e.get("rules", []):
            return False
        if any(f.fnmatch(m["cell"], c) for c in e.get("cells", [])):
            return True
        x, y = m["at"]
        return bool(e.get("footprints")) and any(
            x0 <= x <= x1 and y0 <= y <= y1
            for cell, x0, y0, x1, y1 in places or []
            if any(f.fnmatch(cell, c) for c in e.get("cells", []))
        )

    counted, excluded = [], []
    for m in found:
        hit = next((e for e in exclusions if covers(e, m)), None)
        (excluded if hit else counted).append(m | ({"exclusion": hit.get("reason", "")} if hit else {}))
    return counted, excluded


def tally(items: list[dict]) -> dict[str, int]:
    import collections

    return dict(collections.Counter(f"{m['rule']}@{m['cell']}" for m in items))


def lvs(gds: str, layout: str, work: Path) -> dict:
    """Netgen LVS of the GDS (extracted by Magic) against the powered netlist."""
    lay = extract(layout, work / "layout")
    shutil.copy(gds, work / "chip_top.gds")
    state = {
        "gds": str(work / "chip_top.gds"),
        "def": str(lay / "views" / "chip_top.def"),
        "pnl": str(lay / "views" / "chip_top.pnl.v"),
    }
    m = librelane_steps(
        lay,
        work,
        "lvs",
        "Magic.SpiceExtraction",
        "Checker.LVS",
        state,
        {"RUN_LVS": True, "ERROR_ON_LVS_ERROR": False, "MAGIC_EXT_USE_GDS": True, "ERROR_ON_ILLEGAL_OVERLAPS": False},
    )
    keys = (
        "design__lvs_error__count",
        "design__lvs_device_difference__count",
        "design__lvs_net_difference__count",
        "design__lvs_property_fail__count",
        "design__lvs_unmatched_device__count",
        "design__lvs_unmatched_net__count",
        "design__lvs_unmatched_pin__count",
    )
    out = {k.replace("design__", "").replace("__count", ""): int(m.get(k, -1)) for k in keys}
    return {"metrics": out | {"lvs_complete": all(v >= 0 for v in out.values())}}


# -- STA ---------------------------------------------------------------------------------

STA_SCRIPT = """
set_cmd_units -time ns -capacitance pF -current mA -voltage V -resistance kOhm -distance um
foreach lib $libs {{ read_liberty $lib }}
read_verilog {netlist}
link_design chip_top
read_spef {spef}
read_sdc {sdc}
puts "LLMCHIP_CHECK_SETUP_BEGIN"
check_setup -verbose -unconstrained_endpoints -multiple_clock -no_clock -no_input_delay \\
  -no_output_delay -loops -generated_clocks
puts "LLMCHIP_CHECK_SETUP_END"
puts "LLMCHIP clocks [llength [all_clocks]]"
puts "LLMCHIP wns_max [worst_slack -max]"
puts "LLMCHIP wns_min [worst_slack -min]"
puts "LLMCHIP tns_max [total_negative_slack -max]"
puts "LLMCHIP tns_min [total_negative_slack -min]"
set n 0
foreach p [find_timing_paths -unique_paths_to_endpoint -path_delay max -group_path_count 100000 -slack_max 0] {{
  if {{ [get_property $p slack] < 0 }} {{ incr n }}
}}
puts "LLMCHIP setup_violations $n"
set n 0
foreach p [find_timing_paths -unique_paths_to_endpoint -path_delay min -group_path_count 100000 -slack_max 0] {{
  if {{ [get_property $p slack] < 0 }} {{ incr n }}
}}
puts "LLMCHIP hold_violations $n"
puts "LLMCHIP_DRV_BEGIN"
report_check_types -max_slew -max_capacitance -max_fanout -violators
puts "LLMCHIP_DRV_END"
report_checks -path_delay min_max -format full_clock_expanded -digits 3
"""

CHECK_SETUP_PATTERNS = {
    "unclocked": r"(\d+) unclocked register",
    "unconstrained_endpoints": r"(\d+) unconstrained endpoints?",
    "inputs_without_delay": r"(\d+) input ports? missing set_input_delay",
    "outputs_without_delay": r"(\d+) output ports? missing set_output_delay",
    "combinational_loops": r"(\d+) combinational loops?",
    "multiple_clocks": r"(\d+) register/latch pins? with multiple clocks",
    "generated_clocks_unconnected": r"(\d+) generated clocks? .*not connected",
}


def sta_corner(lay: Path, work: Path, corner: str, libs: list[str], sdc_path: Path) -> dict:
    rc = corner.split("_")[0]  # nom / min / max parasitics
    spef = next((lay / "views" / "spef" / rc).glob("*.spef"))
    script = (
        "set libs {"
        + " ".join(libs)
        + "}\n"
        + STA_SCRIPT.format(netlist=lay / "views" / "chip_top.nl.v", spef=spef, sdc=sdc_path)
    )
    (work / f"sta-{corner}.tcl").write_text(script)
    text = physical.run(["sta", "-no_splash", "-exit", f"sta-{corner}.tcl"], work, timeout=3600)
    (work / f"sta-{corner}.log").write_text(text)
    values = dict(re.findall(r"^LLMCHIP (\w+) (\S+)$", text, re.M))
    setup = text.split("LLMCHIP_CHECK_SETUP_BEGIN")[1].split("LLMCHIP_CHECK_SETUP_END")[0]
    drv = text.split("LLMCHIP_DRV_BEGIN")[1].split("LLMCHIP_DRV_END")[0]
    issues = {}
    for key, pattern in CHECK_SETUP_PATTERNS.items():
        m = re.search(pattern, setup)
        issues[key] = int(m.group(1)) if m else 0
    # On-chip electrical rules; the off-chip side of the pads (PAD pins) is judged against the
    # board requirements of the chip spec instead.
    rows = re.findall(r"^(\S+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+) \(VIOLATED\)", drv, re.M)
    sections = re.split(r"^max (?:slew|capacitance|fanout)\s*$", drv, flags=re.M)
    kinds = re.findall(r"^max (slew|capacitance|fanout)\s*$", drv, re.M)
    counts = {"slew": 0, "capacitance": 0, "fanout": 0}
    pad_slew = 0.0
    for kind, section in zip(kinds, sections[1:], strict=False):
        for pin, _limit, value, _slack in re.findall(
            r"^(\S+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+) \(VIOLATED\)", section, re.M
        ):
            if pin.endswith("/PAD"):
                if kind == "slew":
                    pad_slew = max(pad_slew, float(value))
                continue
            counts[kind] += 1
    drv_counts = {
        "max_slew_violations": counts["slew"],
        "max_cap_violations": counts["capacitance"],
        "max_fanout_violations": counts["fanout"],
        "pad_slew_max_ns": pad_slew,
        "drv_rows": len(rows),
    }
    return {
        "clocks": int(values.get("clocks", 0)),
        "wns_max": float(values.get("wns_max", "nan")),
        "wns_min": float(values.get("wns_min", "nan")),
        "tns_max": float(values.get("tns_max", "nan")),
        "tns_min": float(values.get("tns_min", "nan")),
        "setup_violations": int(values.get("setup_violations", -1)),
        "hold_violations": int(values.get("hold_violations", -1)),
        **issues,
        **drv_counts,
    }


def sta(layout: str, sdc_text: str, registry: dict, chip: dict, corners: list[str], work: Path) -> dict:
    """Multi-corner STA with parasitics, constraint completeness (FR-SGN-02a) and the
    timing exception registry (FR-SGN-02b)."""
    lay = extract(layout, work / "layout")
    resolved = json.loads((lay / "design" / "resolved.json").read_text())
    sdc_path = work / "constraints.sdc"
    sdc_path.write_text(sdc_text)
    results = {}
    for corner in corners:
        libs = [p for pattern, paths in resolved["CELL_LIBS"].items() if fnmatch(corner, pattern) for p in paths]
        libs += [
            p for pattern, paths in (resolved.get("PAD_LIBS") or {}).items() if fnmatch(corner, pattern) for p in paths
        ]
        if not libs:
            results[corner] = {"error": "no timing libraries for this corner"}
            continue
        results[corner] = sta_corner(lay, work, corner, libs, sdc_path)

    # Constraint completeness against the chip spec's interface timing.
    timing = chip["timing"]
    spec_ports = {"input": set(timing["inputs"]), "output": set(timing["outputs"])}
    delayed = constraints.delayed_ports(sdc_text)
    missing = sorted((spec_ports["input"] - delayed["input"]) | (spec_ports["output"] - delayed["output"]))
    values_ok = sdc_values_match(sdc_text, timing)
    exceptions = constraints.exceptions(sdc_text)
    registered = {
        e["command"].strip() for e in registry.get("exceptions", []) if e.get("reason") and e.get("approvedBy")
    }
    unregistered = [e for e in exceptions if e not in registered]
    clock_defined = any(clock_port_defined(c, timing["clock"]["port"]) for c in constraints.clocks(sdc_text))
    ok_corners = [c for c in corners if "error" not in results[c]]

    def total(key: str) -> int:
        return sum(max(results[c].get(key, 0), 0) for c in ok_corners)

    metrics = {
        "corners_required": len(corners),
        "corners_checked": len(ok_corners),
        "setup_violations": total("setup_violations"),
        "hold_violations": total("hold_violations"),
        "max_slew_violations": total("max_slew_violations"),
        "max_cap_violations": total("max_cap_violations"),
        "max_fanout_violations": total("max_fanout_violations"),
        "pad_slew_max_ns": max((results[c].get("pad_slew_max_ns", 0.0) for c in ok_corners), default=0.0),
        "pad_transition_within_spec": max((results[c].get("pad_slew_max_ns", 0.0) for c in ok_corners), default=0.0)
        <= (timing.get("board") or {}).get("maxPadTransitionNs", 0.05 * timing["clock"]["periodNs"]),
        "worst_setup_slack_ns": min((results[c]["wns_max"] for c in ok_corners), default=float("nan")),
        "worst_hold_slack_ns": min((results[c]["wns_min"] for c in ok_corners), default=float("nan")),
        # FR-SGN-02a: every clock defined, every port delayed from the spec, nothing unconstrained, no loops.
        "clock_defined": clock_defined,
        "unclocked_registers": total("unclocked"),
        "unconstrained_endpoints": total("unconstrained_endpoints"),
        "ports_without_delay": len(missing) + total("inputs_without_delay") + total("outputs_without_delay"),
        "port_delays_match_spec": values_ok,
        "combinational_loops": total("combinational_loops"),
        # FR-SGN-02b
        "timing_exceptions": len(exceptions),
        "unregistered_exceptions": len(unregistered),
    }
    return {"metrics": metrics, "corners": results, "missingPorts": missing, "unregistered": unregistered}


def fnmatch(name: str, pattern: str) -> bool:
    import fnmatch as f

    return f.fnmatch(name, pattern)


def clock_port_defined(command: str, port: str) -> bool:
    return bool(re.search(r"\[get_ports\s+\{?" + re.escape(port) + r"\}?\]", command))


def sdc_values_match(sdc: str, timing: dict) -> bool:
    """Every input/output delay in the SDC equals the chip spec's value for its port."""
    for c in constraints.sdc_commands(sdc):
        m = re.match(
            r"set_(input|output)_delay\s+(?:-clock\s+\S+\s+)?-(max|min)\s+([-\d.e]+)\s+\[get_ports\s+\{?(\w+)\}?\]", c
        )
        if not m:
            continue
        kind, which, value, port = m.groups()
        spec = timing["inputs" if kind == "input" else "outputs"].get(port)
        if spec is None or abs(float(value) - spec[f"{which}Ns"]) > 1e-6:
            return False
    return True
