"""Physical implementation on GF180MCU (S6, requirements FR-PHY-01..05).

Wraps the chip function (`llmchip_chip` of an RTL bundle) in the pad ring of
the gf180mcu_fd_io library, sized by the chip spec's die and the PDK's pad
geometry, and runs the LibreLane Chip flow (Yosys, OpenROAD, OpenRCX, Magic and
KLayout stream-out, seal ring, fill). Signoff checks (DRC, LVS, STA) are not
run here: each runs separately on the GDS or netlist it judges (signoff.py).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

PDK_ROOT = "/foss/pdks"
PDK = "gf180mcuD"
SCL = "gf180mcu_fd_sc_mcu7t5v0"
IO = "gf180mcu_fd_io"

# Pad geometry of gf180mcu_fd_io (LEF) and the PDK's LibreLane pad configuration.
PAD_W, PAD_H, CORNER, EDGE = 75.0, 350.0, 355.0, 26.0
# The core ring needs this much room between the pad ring and the core rows.
CORE_MARGIN = 13.0

PDN_CFG = """# Chip-level PDN for GF180MCU with the gf180mcu_fd_io pad ring. The dvdd/dvss
# pads carry the core supply (VDD = DVDD, VSS = DVSS in a single 5 V domain)
# on their core-facing Metal2 fingers; the core ring connects to them.
source $::env(SCRIPTS_DIR)/openroad/common/set_global_connections.tcl
set_global_connections
add_global_connection -net VDD -pin_pattern {^DVDD$} -power
add_global_connection -net VSS -pin_pattern {^DVSS$} -ground
global_connect
set_voltage_domain -name CORE -power VDD -ground VSS
define_pdn_grid -name stdcell_grid -starts_with POWER -voltage_domain CORE
add_pdn_stripe -grid stdcell_grid -layer Metal4 -width 1.6 -pitch 76.8 -offset 16.32 -spacing 1.7 \\
    -starts_with POWER -extend_to_core_ring
add_pdn_stripe -grid stdcell_grid -layer Metal5 -width 1.6 -pitch 153.18 -offset 16.65 -spacing 1.7 \\
    -starts_with POWER -extend_to_core_ring
add_pdn_connect -grid stdcell_grid -layers "Metal4 Metal5"
add_pdn_stripe -grid stdcell_grid -layer Metal1 -width 0.6 -followpins
add_pdn_connect -grid stdcell_grid -layers "Metal1 Metal4"
add_pdn_ring -grid stdcell_grid -layers "Metal4 Metal5" -widths "4 4" -spacings "1.7 1.7" -core_offset "2 2" \\
    -connect_to_pads -connect_to_pad_layers Metal2
add_pdn_connect -grid stdcell_grid -layers "Metal2 Metal4"
add_pdn_connect -grid stdcell_grid -layers "Metal2 Metal5"
"""

_env: dict | None = None


def tools_env() -> dict:
    """Environment of the pinned tool image (its setup script), with the GF180 PDK selected."""
    global _env
    if _env is None:
        out = subprocess.run(
            ["bash", "-c", "source /etc/profile.d/iic-osic-tools-setup.sh >/dev/null 2>&1; env -0"],
            capture_output=True,
            timeout=120,
        ).stdout
        env = dict(item.split("=", 1) for item in out.decode().split("\0") if "=" in item)
        env.pop("PAD_CELL_LIBRARY", None)
        env |= {"PDK_ROOT": PDK_ROOT, "PDK": PDK, "STD_CELL_LIBRARY": SCL, "HOME": os.environ.get("HOME", "/tmp")}
        _env = env
    return _env


def run(cmd: list[str], cwd: Path, timeout: int = 7200) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=tools_env())
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({proc.returncode}):\n{(proc.stdout + proc.stderr)[-6000:]}")
    return proc.stdout + proc.stderr


def cpu_limit() -> int:
    """CPUs this container may use (the cgroup quota set by the scheduler, else all)."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    return max(1, len(os.sched_getaffinity(0)))


# -- pad ring --------------------------------------------------------------------------


def side_capacity(length_um: float) -> int:
    return int((length_um - 2 * CORNER - 2 * EDGE) // (PAD_W + 2))


def pad_plan(chip: dict, power_pairs: int) -> dict:
    """Pad instance per pin and per supply, and their sides (counter-clockwise from south)."""
    die = chip["targets"]["die"]
    x, y = die["xMm"] * 1000, die["yMm"] * 1000
    pads = [
        {"inst": f"p_{p['name']}", "pin": p["name"], "dir": p["dir"], "enable": p.get("enable")} for p in chip["pins"]
    ]
    supplies = []
    for k in range(power_pairs):
        supplies += [
            {"inst": f"p_vdd{k}", "pin": "VDD", "dir": "power"},
            {"inst": f"p_vss{k}", "pin": "VSS", "dir": "ground"},
        ]
    caps = {"south": side_capacity(x), "east": side_capacity(y), "north": side_capacity(x), "west": side_capacity(y)}
    if len(pads) + len(supplies) > sum(caps.values()):
        raise ValueError(
            f"{len(pads) + len(supplies)} pads do not fit the {die['xMm']} x {die['yMm']} mm die "
            f"({sum(caps.values())} sites)"
        )
    sides: dict[str, list[str]] = {s: [] for s in caps}
    # Supplies split over the long sides, signals fill the remaining sites in order.
    order = ["east", "west"] if y >= x else ["south", "north"]
    for k, s in enumerate(supplies):
        sides[order[(k // 2) % 2]].append(s["inst"])
    for p in pads:
        side = next(s for s in ("south", "west", "north", "east") if len(sides[s]) < caps[s])
        sides[side].append(p["inst"])
    return {"pads": pads, "supplies": supplies, "sides": sides, "capacity": caps}


def chip_top(plan: dict) -> str:
    """The padded top: gf180mcu_fd_io__in_c for inputs, bi_t for outputs (enabled by
    the chip's bus enable where the pin has one; strongest drive, fast slew, so the
    board load meets the pad's transition limit) and for bidirectional pins (output
    enable from the chip, input enabled only while the pad does not drive: the pad's
    A -> PAD timing arcs exist only with IE low, so a tied IE would leave the output
    untimed), dvdd/dvss for the supplies."""
    pp = "`ifdef USE_POWER_PINS\n    .DVDD(VDD), .DVSS(VSS), .VDD(VDD), .VSS(VSS),\n`endif\n"
    # The bus enable of an external memory (kv_oe); a chip without one (spatial) has no port.
    enabled = any(p.get("enable") for p in plan["pads"])
    ports = ["`ifdef USE_POWER_PINS", "  inout wire VDD,", "  inout wire VSS,", "`endif"]
    # Directed ports, so STA sees each pad's off-chip side as an input or an output.
    kinds = {"in": "input", "out": "output", "inout": "inout"}
    ports += [
        f"  {kinds[p['dir']]} wire {p['pin']}{',' if k < len(plan['pads']) - 1 else ''}"
        for k, p in enumerate(plan["pads"])
    ]
    lines = [
        "// Generated by eda-gf180 from the chip spec; do not edit.",
        "`timescale 1ns/1ps",
        "`default_nettype none",
        "module chip_top (",
        *ports,
        ");",
        *[
            f"  wire c_{p['pin']}_o, c_{p['pin']}_oe, c_{p['pin']}_i;"
            if p["dir"] == "inout"
            else f"  wire c_{p['pin']};"
            for p in plan["pads"]
        ],
        *(["  wire c_kv_oe;"] if enabled else []),
    ]
    for p in plan["pads"]:
        if p["dir"] == "in":
            # The pad's Y output may drive a single input (max_fanout 1 in its Liberty): a buffer
            # drives the core.
            lines.append(f"  wire y_{p['pin']};")
            lines.append(
                f"  (* keep *) {IO}__in_c {p['inst']} (\n{pp}"
                f"    .PAD({p['pin']}), .Y(y_{p['pin']}), .PU(1'b0), .PD(1'b0));"
            )
            lines.append(
                f"  (* keep *) {SCL}__buf_4 b_{p['pin']} (\n`ifdef USE_POWER_PINS\n"
                "    .VDD(VDD), .VSS(VSS), .VNW(VDD), .VPW(VSS),\n`endif\n"
                f"    .I(y_{p['pin']}), .Z(c_{p['pin']}));"
            )
        elif p["dir"] == "inout":
            lines.append(f"  wire y_{p['pin']};")
            lines.append(
                f"  (* keep *) {IO}__bi_t {p['inst']} (\n{pp}"
                f"    .PAD({p['pin']}), .A(c_{p['pin']}_o), .OE(c_{p['pin']}_oe), .IE(~c_{p['pin']}_oe), .CS(1'b0),"
                f" .SL(1'b0), .PU(1'b0), .PD(1'b0), .PDRV0(1'b1), .PDRV1(1'b1), .Y(y_{p['pin']}));"
            )
            lines.append(
                f"  (* keep *) {SCL}__buf_4 b_{p['pin']} (\n`ifdef USE_POWER_PINS\n"
                "    .VDD(VDD), .VSS(VSS), .VNW(VDD), .VPW(VSS),\n`endif\n"
                f"    .I(y_{p['pin']}), .Z(c_{p['pin']}_i));"
            )
        else:
            oe = "c_kv_oe" if p.get("enable") else "1'b1"
            lines.append(
                f"  (* keep *) {IO}__bi_t {p['inst']} (\n{pp}"
                f"    .PAD({p['pin']}), .A(c_{p['pin']}), .OE({oe}), .IE(1'b0), .CS(1'b0),"
                " .SL(1'b0), .PU(1'b0), .PD(1'b0), .PDRV0(1'b1), .PDRV1(1'b1), .Y());"
            )
    for s in plan["supplies"]:
        cell = "dvdd" if s["dir"] == "power" else "dvss"
        pins = ".DVDD(VDD), .DVSS(VSS), .VSS(VSS)" if cell == "dvdd" else ".DVDD(VDD), .DVSS(VSS), .VDD(VDD), .VSS(VSS)"
        lines.append(f"  (* keep *) {IO}__{cell} {s['inst']} (\n`ifdef USE_POWER_PINS\n    {pins}\n`endif\n  );")
    conns = ", ".join(
        f".{p['pin']}_o(c_{p['pin']}_o), .{p['pin']}_oe(c_{p['pin']}_oe), .{p['pin']}_i(c_{p['pin']}_i)"
        if p["dir"] == "inout"
        else f".{p['pin']}(c_{p['pin']})"
        for p in plan["pads"]
    )
    kv_oe = ", .kv_oe(c_kv_oe)" if enabled else ""
    lines += [f"  llmchip_chip u_chip ({conns}{kv_oe});", "endmodule", ""]
    return "\n".join(lines)


def core_area(chip: dict) -> list[float]:
    die = chip["targets"]["die"]
    x, y = die["xMm"] * 1000, die["yMm"] * 1000
    m = EDGE + PAD_H + CORE_MARGIN + 5
    return [m, m, x - m, y - m]


# -- implementation ----------------------------------------------------------------------


def design_dir(work: Path, rtl_tar: str, chip: dict, sdc: str, params: dict) -> tuple[Path, dict, dict]:
    """The LibreLane design directory: RTL, padded top, constraints, PDN script and config."""
    d = work / "design"
    (d / "src").mkdir(parents=True, exist_ok=True)
    with tarfile.open(rtl_tar) as tar:
        tar.extractall(work / "rtl", filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(work / "rtl")
    bundle = work / "rtl" / "bundle"
    rtl_design = json.loads((bundle / "design.json").read_text())
    for rel in rtl_design["sources"] + rtl_design["includes"]:
        (d / "src" / Path(rel).name).write_bytes((bundle / rel).read_bytes())
    plan = pad_plan(chip, int(params.get("powerPairs", 2)))
    (d / "src" / "chip_top.v").write_text(chip_top(plan))
    (d / "chip_top.sdc").write_text(sdc)
    (d / "pdn_cfg.tcl").write_text(PDN_CFG)
    die = chip["targets"]["die"]
    period = chip["timing"]["clock"]["periodNs"]
    config = {
        "DESIGN_NAME": "chip_top",
        "VERILOG_FILES": [f"dir::src/{Path(rel).name}" for rel in rtl_design["sources"]] + ["dir::src/chip_top.v"],
        "VERILOG_INCLUDE_DIRS": ["dir::src"],
        "CLOCK_PORT": chip["timing"]["clock"]["port"],
        "CLOCK_PERIOD": period,
        "PNR_SDC_FILE": "dir::chip_top.sdc",
        "SIGNOFF_SDC_FILE": "dir::chip_top.sdc",
        "FP_SIZING": "absolute",
        "DIE_AREA": [0, 0, die["xMm"] * 1000, die["yMm"] * 1000],
        "CORE_AREA": core_area(chip),
        "PAD_SOUTH": plan["sides"]["south"],
        "PAD_EAST": plan["sides"]["east"],
        "PAD_NORTH": plan["sides"]["north"],
        "PAD_WEST": plan["sides"]["west"],
        "VDD_NETS": ["VDD"],
        "GND_NETS": ["VSS"],
        "PDN_CFG": "dir::pdn_cfg.tcl",
        # Output pads drive the bond pad only; their input path (Y) is unused by design.
        "IGNORE_DISCONNECTED_MODULES": [f"{IO}__bi_t"],
        "PL_TARGET_DENSITY_PCT": int(params.get("placementDensityPct", 62)),
        "OPENROAD_THREADS": cpu_limit(),
        "SYNTH_STRATEGY": params.get("synthStrategy", "AREA 0"),
        "SYNTH_HIERARCHY_MODE": params.get("synthHierarchy", "flatten"),
        # Deferred flattening re-reads the hierarchical netlist: it must keep the `keep` attribute,
        # or the supply pads (cells without signal ports) are removed as unused.
        "SYNTH_WRITE_NOATTR": params.get("synthHierarchy", "flatten") != "deferred_flatten",
        # Repair slew, capacitance and timing again after global routing (signoff parasitics).
        "RUN_POST_GRT_DESIGN_REPAIR": True,
        "RUN_POST_GRT_RESIZER_TIMING": True,
        # Margins against the signoff extraction (max parasitics are well above the estimate).
        "DESIGN_REPAIR_MAX_SLEW_PCT": 40,
        "DESIGN_REPAIR_MAX_CAP_PCT": 40,
        "GRT_DESIGN_REPAIR_MAX_SLEW_PCT": 60,
        "GRT_DESIGN_REPAIR_MAX_CAP_PCT": 60,
        # Hold margin against the signoff extraction at the fast corner (the core leaves little
        # room for delay cells, so only after routing).
        "GRT_RESIZER_HOLD_SLACK_MARGIN": float(params.get("grtHoldMarginNs", 0.15)),
        # Hold margin of the repair after CTS. Designs of shift chains (the spatial engine) have
        # one hold path per link; a smaller margin there leaves the room the buffers would take.
        "PL_RESIZER_HOLD_SLACK_MARGIN": float(params.get("holdMarginNs", 0.1)),
        # Signoff runs separately on the final GDS and netlist (signoff.py).
        "RUN_MAGIC_DRC": False,
        "RUN_KLAYOUT_DRC": False,
        "RUN_LVS": False,
        # Timing and electrical rules are judged by the signoff STA (signoff.py), not here.
        "SETUP_VIOLATION_CORNERS": [""],
        "HOLD_VIOLATION_CORNERS": [""],
        "MAX_SLEW_VIOLATION_CORNERS": [""],
        "MAX_CAP_VIOLATION_CORNERS": [""],
    }
    (d / "config.json").write_text(json.dumps(config, indent=2))
    return d, config, plan


def librelane(design: Path, tag: str, *extra: str, timeout: int = 36000) -> Path:
    run(
        [
            "librelane",
            "--manual-pdk",
            "--pdk-root",
            PDK_ROOT,
            "--pdk",
            PDK,
            "--scl",
            SCL,
            "--flow",
            "Chip",
            "--run-tag",
            tag,
            "--overwrite",
            "--hide-progress-bar",
            *extra,
            "config.json",
        ],
        design,
        timeout=timeout,
    )
    return design / "runs" / tag


def final_state(rundir: Path) -> dict:
    steps = sorted(
        (p for p in rundir.iterdir() if p.is_dir() and p.name[:2].isdigit()), key=lambda p: int(p.name.split("-")[0])
    )
    for step in reversed(steps):
        if (step / "state_out.json").exists():
            return json.loads((step / "state_out.json").read_text())
    raise RuntimeError("the flow wrote no state")


def tar_dir(files: dict[str, Path], path: Path) -> None:
    """Deterministic archive of the given files (sorted names, fixed metadata)."""
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            data = files[name].read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))


def measurement_record(metrics: dict, plan: dict, chip: dict) -> dict:
    """FR-PHY-04: measured values of the implementation, for comparison with the estimate."""
    die = chip["targets"]["die"]
    corners = sorted({k.split("corner:")[1] for k in metrics if "corner:" in k})
    keys = (
        "design__instance__area__stdcell",
        "design__core__area",
        "design__die__area",
        "design__instance__utilization__stdcell",
    )
    return {
        "format": "llmchip-measurement/1",
        "die": {"xMm": die["xMm"], "yMm": die["yMm"], "areaMm2": round(die["xMm"] * die["yMm"], 4)},
        "area": {k: metrics.get(k) for k in keys},
        "power": {k: v for k, v in metrics.items() if k.startswith("power__")},
        "timing": {
            c: {
                m: metrics.get(f"timing__{m}__corner:{c}")
                for m in ("setup__ws", "hold__ws", "setup_vio__count", "hold_vio__count")
            }
            for c in corners
        },
        "cells": {
            k.split("class:")[1]: v for k, v in metrics.items() if k.startswith("design__instance__count__class:")
        },
        "pads": {side: names for side, names in plan["sides"].items()},
        "source": "LibreLane Chip flow on gf180mcuD (iic-osic-tools)",
        "confidence": "measured",
    }
