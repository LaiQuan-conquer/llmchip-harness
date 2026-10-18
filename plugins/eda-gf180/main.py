"""IF-2 entry point of the eda-gf180 plugin (runs in the pinned EDA image).

Handlers: calibrate (FR-EST-13), constraints (SDC from the chip spec),
implement (S6: pad ring, LibreLane Chip flow), drc, lvs and sta (S7 signoff,
each on exactly the artifacts it judges).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import constraints
import physical
import signoff
from calibrate import calibrate
from llmchip_sdk import InvokeContext, Output, Result, run_plugin

HERE = Path(__file__).parent
# Signoff corners of the prerun: every parasitic extreme with every library corner.
SIGNOFF_CORNERS = [
    f"{rc}_{lib}" for rc in ("nom", "min", "max") for lib in ("tt_025C_5v00", "ss_125C_4v50", "ff_n40C_5v50")
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def handle_constraints(ctx: InvokeContext) -> Result:
    chip = json.loads(Path(ctx.input("chip").path).read_text())
    sdc = constraints.sdc_from_spec(chip)
    Path(ctx.out_dir, "constraints.sdc").write_text(sdc)
    return Result(
        outputs=[Output("constraints", "constraints.sdc", "timing.constraints", "text/plain")],
        metrics={
            "ports_constrained": len(chip["timing"]["inputs"]) + len(chip["timing"]["outputs"]),
            "timing_exceptions": 0,
        },
        parse={"status": "complete", "parser": "eda-gf180/constraints"},
    )


def implement(ctx: InvokeContext) -> Result:
    chip = json.loads(Path(ctx.input("chip").path).read_text())
    if not chip.get("targets", {}).get("die"):
        raise ValueError("the chip spec has no die size (targets.die)")
    sdc = Path(ctx.input("constraints").path).read_text()
    work = Path(ctx.work_dir)
    design, _config, plan = physical.design_dir(work, ctx.input("rtl").path, chip, sdc, ctx.params)
    ctx.progress(0.05, "LibreLane Chip flow")
    rundir = physical.librelane(design, "impl")
    state = physical.final_state(rundir)
    metrics = state.get("metrics", {})
    shutil.copy(rundir / "resolved.json", design / "resolved.json")

    # The layout bundle: design directory (to re-run signoff steps), views, simulation models, reports.
    files: dict[str, Path] = {}
    for p in design.rglob("*"):
        if p.is_file() and "runs" not in p.relative_to(design).parts:
            files[f"design/{p.relative_to(design)}"] = p
    views = {
        "def": "chip_top.def",
        "nl": "chip_top.nl.v",
        "pnl": "chip_top.pnl.v",
        "gds": "chip_top.gds",
        "render": "chip_top.png",
    }
    for key, name in views.items():
        if not state.get(key):
            raise RuntimeError(f"the flow produced no {key} view")
        files[f"views/{name}"] = Path(state[key])
    for rc, path in (state.get("spef") or {}).items():
        files[f"views/spef/{rc.rstrip('_*')}/chip_top.{rc.rstrip('_*')}.spef"] = Path(path)
    for corner, path in (state.get("sdf") or {}).items():
        files[f"views/sdf/chip_top.{corner}.sdf"] = Path(path)
    files["sim/pads.v"] = HERE / "sim" / "pads.v"
    files["views/layers.map"] = (
        Path(physical.PDK_ROOT) / physical.PDK / "libs.tech" / "klayout" / "tech" / "gf180mcu.map"
    )
    (work / "models.json").write_text(
        json.dumps(
            {
                "cells": [
                    f"{physical.PDK_ROOT}/{physical.PDK}/libs.ref/{physical.SCL}/verilog/primitives.v",
                    f"{physical.PDK_ROOT}/{physical.PDK}/libs.ref/{physical.SCL}/verilog/{physical.SCL}.v",
                ],
                "pads": "sim/pads.v",
                "defines": ["FUNCTIONAL"],
                "top": "chip_top",
            },
            indent=2,
        )
    )
    files["sim/models.json"] = work / "models.json"
    (work / "metrics.json").write_text(json.dumps(metrics, indent=2))
    files["reports/metrics.json"] = work / "metrics.json"
    record = physical.measurement_record(metrics, plan, chip)
    record["snapshot"] = {
        "rtl": ctx.input("rtl").hash,
        "chip": ctx.input("chip").hash,
        "constraints": ctx.input("constraints").hash,
    }
    record["implementation"] = chip.get("storage", {}).get("weights", {}).get("implementation")
    out = Path(ctx.out_dir)
    physical.tar_dir(files, out / "layout.tar")
    shutil.copy(state["gds"], out / "chip_top.gds")
    shutil.copy(state["render"], out / "chip_top.png")
    (out / "measurement.json").write_text(json.dumps(record, indent=2))
    shutil.copy(state["sdf"]["nom_tt_025C_5v00"], out / "chip_top.nom_tt.sdf")
    (out / "pads.json").write_text(
        json.dumps(
            {"die": chip["targets"]["die"], "sides": plan["sides"], "pads": plan["pads"] + plan["supplies"]}, indent=2
        )
    )
    area = metrics.get("design__instance__area__stdcell")
    core = metrics.get("design__core__area")
    summary = {
        "die_area_mm2": round(chip["targets"]["die"]["xMm"] * chip["targets"]["die"]["yMm"], 4),
        "core_area_um2": core,
        "stdcell_area_um2": area,
        "flow_setup_violations": metrics.get("timing__setup_vio__count", -1),
        "flow_hold_violations": metrics.get("timing__hold_vio__count", -1),
        "routing_drc_errors": metrics.get("route__drc_errors", -1),
        "gds_sha256": sha256(out / "chip_top.gds"),
    }
    return Result(
        outputs=[
            Output("layout", "layout.tar", "layout.bundle", "application/x-tar"),
            Output("gds", "chip_top.gds", "layout.gds", "application/octet-stream"),
            Output("render", "chip_top.png", "layout.image", "image/png"),
            Output("measurements", "measurement.json", "measurement.record", "application/json"),
            Output("pads", "pads.json", "pad.list", "application/json"),
            Output("sdf", "chip_top.nom_tt.sdf", "timing.sdf", "text/plain"),
        ],
        metrics={k: v for k, v in summary.items() if v is not None},
        parse={"status": "complete", "parser": "eda-gf180/implement"},
    )


def handle_drc(ctx: InvokeContext) -> Result:
    rules = json.loads(Path(ctx.input("rules").path).read_text())
    result = signoff.drc(ctx.input("gds").path, ctx.input("layout").path, rules, Path(ctx.work_dir))
    report = {
        "gds": ctx.input("gds").hash,
        "pdk": physical.PDK,
        "rules": {"hash": ctx.input("rules").hash, **rules},
        **result,
    }
    Path(ctx.out_dir, "drc.json").write_text(json.dumps(report, indent=2))
    return Result(
        outputs=[Output("report", "drc.json", "drc.report", "application/json")],
        metrics=result["metrics"]
        | {f"klayout_rule.{k}": v for k, v in result["rules"]["klayout_drc"].items()}
        | {f"klayout_excluded.{k}": v for k, v in result["rules"]["klayout_drc_excluded"].items()}
        | {"klayout_decks": rules["klayoutDrc"]["decks"]},
        parse={"status": "complete", "parser": "eda-gf180/drc"},
    )


def handle_lvs(ctx: InvokeContext) -> Result:
    result = signoff.lvs(ctx.input("gds").path, ctx.input("layout").path, Path(ctx.work_dir))
    Path(ctx.out_dir, "lvs.json").write_text(json.dumps({"gds": ctx.input("gds").hash, **result}, indent=2))
    return Result(
        outputs=[Output("report", "lvs.json", "lvs.report", "application/json")],
        metrics=result["metrics"],
        parse={"status": "complete" if result["metrics"]["lvs_complete"] else "partial", "parser": "eda-gf180/lvs"},
    )


def handle_sta(ctx: InvokeContext) -> Result:
    chip = json.loads(Path(ctx.input("chip").path).read_text())
    registry = json.loads(Path(ctx.input("exceptions").path).read_text())
    sdc = Path(ctx.input("constraints").path).read_text()
    # The signoff corners are fixed by the process (a hard constraint), never by run parameters.
    corners = SIGNOFF_CORNERS
    result = signoff.sta(ctx.input("layout").path, sdc, registry, chip, corners, Path(ctx.work_dir))
    Path(ctx.out_dir, "sta.json").write_text(json.dumps(result, indent=2, default=str))
    return Result(
        outputs=[Output("report", "sta.json", "sta.report", "application/json")],
        metrics=result["metrics"],
        parse={"status": "complete", "parser": "eda-gf180/sta"},
    )


if __name__ == "__main__":
    run_plugin(
        "eda-gf180",
        "0.2.0",
        {
            "calibrate": calibrate,
            "constraints": handle_constraints,
            "implement": implement,
            "drc": handle_drc,
            "lvs": handle_lvs,
            "sta": handle_sta,
        },
    )
