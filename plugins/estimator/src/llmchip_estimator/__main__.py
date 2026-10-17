"""IF-2 entry point of the estimator plugin."""

import json
from pathlib import Path

from llmchip_sdk import InvokeContext, Output, Result, run_plugin

from .estimate import estimate, summary_markdown


def handle_estimate(ctx: InvokeContext) -> Result:
    profile = json.loads(Path(ctx.input("profile").path).read_text())
    library = json.loads(Path(ctx.input("library").path).read_text())
    report = estimate(ctx.params, profile, library)
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


if __name__ == "__main__":
    run_plugin("estimator", "0.1.0", {"estimate": handle_estimate})
