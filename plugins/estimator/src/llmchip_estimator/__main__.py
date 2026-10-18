"""IF-2 entry point of the estimator plugin."""

import json
from pathlib import Path

from llmchip_sdk import InvokeContext, Output, Result, run_plugin

from .estimate import apply_calibrations, estimate, summary_markdown


def handle_estimate(ctx: InvokeContext) -> Result:
    profile = json.loads(Path(ctx.input("profile").path).read_text())
    library = json.loads(Path(ctx.input("library").path).read_text())
    calibrations = [json.loads(Path(i.path).read_text()) for i in ctx.inputs if i.name == "calibration"]
    library = apply_calibrations(library, calibrations)
    params = dict(ctx.params)
    request = [i for i in ctx.inputs if i.name == "request"]
    if request:
        params |= json.loads(Path(request[0].path).read_text())
    for required in ("model", "weightStorage"):
        if required not in params:
            raise ValueError(f"estimate needs `{required}` in its params or request")
    report = estimate(params, profile, library)
    out = Path(ctx.out_dir)
    (out / "estimate.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (out / "estimate.md").write_text(summary_markdown(report))
    feasibility = report["conclusion"]["feasibility"]
    metrics: dict[str, float | int | str | bool] = {
        "feasibility": feasibility,
        "feasible_or_conditional": feasibility in ("feasible", "conditionally_feasible"),
        "missing_count": len(report["missing"]),
        "tokens_per_second": report["performance"]["tokensPerSecond"],
    }
    if "area" in report:
        metrics["die_area_mm2.typ"] = report["area"]["dieMm2"]["typ"]
    if report.get("cost", {}).get("cny"):
        metrics["cost_cny.typ"] = report["cost"]["cny"]["typ"]
    return Result(
        outputs=[
            Output("report", "estimate.json", "estimate.report", "application/json"),
            Output("summary", "estimate.md", "estimate.summary", "text/markdown"),
        ],
        metrics=metrics,
        parse={"status": "complete", "parser": "llmchip-estimator/0.1"},
    )


STAGE_VIEWS = {"S4": ["verilog"], "S6": ["liberty", "lef"], "S7": ["gds", "lvs_netlist"]}


def handle_check_views(ctx: InvokeContext) -> Result:
    """Integration views of a calibrated implementation, per stage (requirements §3.8, FR-PHY-08)."""
    report = json.loads(Path(ctx.input("calibration").path).read_text())
    views = report.get("views", {})
    metrics: dict[str, float | int | str | bool] = {"available": report.get("availability") == "available"}
    for stage, needed in STAGE_VIEWS.items():
        missing = [v for v in needed if not views.get(v)]
        metrics[f"views.{stage}.complete"] = not missing
        metrics[f"views.{stage}.missing"] = ", ".join(missing)
    return Result(metrics=metrics, parse={"status": "complete", "parser": "llmchip-estimator/check-views"})


if __name__ == "__main__":
    run_plugin("estimator", "0.1.0", {"estimate": handle_estimate, "check_views": handle_check_views})
