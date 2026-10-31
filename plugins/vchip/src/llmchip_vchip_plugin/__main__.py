"""Kernel plugin `vchip`: the virtual-chip test harness on a project's artifacts (design L4).

Each run builds its chips in the run workspace from its inputs (`chips_from_artifacts`): the
L2 golden from the numeric spec and its weights (S2), the float reference from the Model IR
(S1) when given, and the RTL chip from an `rtl.bundle` (S4) when given, which then is the chip
under test. `bench` and `conform` run the harness on them and return reports and metrics.

The harness judges a chip by its policy when it runs on its own (ADR-010); here it does not:
the reports carry no verdict, and the flow's checks `vchip.l3` and `vchip.conformance` judge
the metrics (AP-7), so a verdict counts as evidence for exactly the run's artifacts.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

from llmchip_sdk import InvokeContext, Output, Result, run_plugin
from llmchip_vchip.adapters import process
from llmchip_vchip.bridges import verilator
from llmchip_vchip.build import GRAPH_SPEC, chips_from_artifacts, graph_chips
from llmchip_vchip.conform import conform as run_conform
from llmchip_vchip.prompts import PromptSet, inline
from llmchip_vchip.prompts import load as load_prompts
from llmchip_vchip.registry import Registry
from llmchip_vchip.runner import bench as run_bench

ID = "chip"


def _optional(ctx: InvokeContext, name: str) -> Path | None:
    return next((Path(i.path) for i in ctx.inputs if i.name == name), None)


def _chips(ctx: InvokeContext) -> tuple[Registry, dict]:
    chips_dir = Path(ctx.work_dir) / "chips"
    ctx.progress(0.05, "building the chips from the artifacts")
    if json.loads(Path(ctx.input("spec").path).read_text()).get("format") == GRAPH_SPEC:
        return _graph_chips(ctx, chips_dir)
    built = chips_from_artifacts(
        ID,
        chips_dir,
        tokenizer=Path(ctx.input("tokenizer").path),
        evalset=_optional(ctx, "evalset") or _empty_evalset(Path(ctx.work_dir)),
        spec=Path(ctx.input("spec").path),
        weights=Path(ctx.input("weights").path),
        rtl=_optional(ctx, "rtl"),
        ir=_optional(ctx, "ir"),
        chip_spec=_optional(ctx, "chip"),
        clock_mhz=ctx.params.get("clockMHz"),
    )
    return Registry([chips_dir]), built


def _graph_chips(ctx: InvokeContext, chips_dir: Path) -> tuple[Registry, dict]:
    """A recovered graph (Qwen): the golden is GraphL2, the chip is the taalas-tile/1 bundle."""
    from llmchip_model.pipeline import open_model

    model_dir, _ = open_model(ctx)
    tokenizer = model_dir / "tokenizer.json"
    if not tokenizer.is_file():
        raise ValueError("the model archive carries no tokenizer.json for the HF tokenizer")
    prompts = _prompts(ctx)
    prompts_path = Path(ctx.work_dir) / "prompts.jsonl"
    prompts_path.write_text(prompts.to_jsonl())
    built = graph_chips(
        ID,
        chips_dir,
        graph=Path(ctx.input("graph").path),
        spec=Path(ctx.input("spec").path),
        weights=Path(ctx.input("weights").path),
        tokenizer=tokenizer,
        prompts=prompts_path,
        rtl=_optional(ctx, "rtl"),
        clock_mhz=float(ctx.params.get("clockMHz") or 100.0),
        max_tokens=int(ctx.params.get("maxTokens") or 64),
    )
    return Registry([chips_dir]), built


def _empty_evalset(work: Path) -> Path:
    path = work / "no-evalset.json"
    path.write_text(json.dumps({"format": "llmchip-evalset/1", "prompts": []}))
    return path


def _prompts(ctx: InvokeContext) -> PromptSet:
    if ctx.params.get("prompts"):
        prompts = inline(list(ctx.params["prompts"]))
        evalset = _optional(ctx, "evalset")
        if evalset is not None:  # the evaluation set's passages still give the perplexity
            prompts.passages = load_prompts(evalset).passages
    elif _optional(ctx, "evalset") is not None:
        prompts = load_prompts(_optional(ctx, "evalset"))
    else:
        raise ValueError("give an evaluation set (input `evalset`) or prompts (param `prompts`)")
    if ctx.params.get("maxPrompts"):
        prompts.cases = prompts.cases[: int(ctx.params["maxPrompts"])]
    return prompts


def _dut(built: dict) -> str:
    return built["chips"].get("dut") or built["chips"]["golden"]


def _strip(report: dict) -> dict:
    """The report as an artifact: no policy and no verdict (the flow's checks judge), no paths
    of the run workspace."""
    return {k: v for k, v in report.items() if k not in ("policy", "verdict", "run_dir")}


def bench(ctx: InvokeContext) -> Result:
    registry, built = _chips(ctx)
    prompts = _prompts(ctx)
    dut = _dut(built)
    chips = built["chips"]
    ctx.progress(0.1, f"benching {registry.get(dut).manifest.data['backend']} on {len(prompts.cases)} prompts")
    ctx.check_cancelled()
    work = Path(ctx.work_dir) / "runs"
    report = run_bench(
        registry,
        dut,
        prompts,
        max_tokens=int(ctx.params.get("maxTokens", 64)),
        reference=chips.get("reference"),
        golden=chips["golden"] if dut != chips["golden"] else None,
        out_dir=work,
        traces=bool(ctx.params.get("traces", False)),
    )
    run_dir = Path(report["run_dir"])
    out = Path(ctx.out_dir)
    artifact = {**_strip(report), "built": built}
    (out / "report.json").write_text(json.dumps(artifact, indent=2, ensure_ascii=False))
    (out / "outputs.jsonl").write_bytes((run_dir / "outputs.jsonl").read_bytes())
    return Result(
        outputs=[
            Output("report", "report.json", "vchip.report", "application/json"),
            Output("outputs", "outputs.jsonl", "vchip.outputs", "application/x-ndjson"),
        ],
        metrics=bench_metrics(report, built),
        parse={"status": "complete", "parser": "vchip/bench"},
    )


def bench_metrics(report: dict, built: dict) -> dict:
    q, sp, m = report["quality"], report["speed"], report["measurement"]
    golden = report["comparisons"].get("golden", {})
    metrics = {
        "chip_backend": report["chip"]["backend"],
        "prompts": report["prompt_set"]["prompts"],
        "max_tokens": report["max_tokens"],
        "tokens": sp.get("tokens"),
        "errors": q["errors"],
        "comparison_errors": q["comparison_errors"],
        "golden_compared": "bit_exact" in golden,
        "bit_exact": golden.get("bit_exact"),
        "next_token_agreement": q.get("next_token_agreement"),
        "sequence_match": q.get("sequence_match"),
        "perplexity": q.get("perplexity"),
        "reference_perplexity": q.get("reference_perplexity"),
        "tok_s": sp.get("tok_s"),
        "cycles_per_token_min": sp.get("cycles_per_token_min"),
        "cycles_per_token_max": sp.get("cycles_per_token_max"),
        "host_cycles_per_token": sp.get("host_cycles_per_token"),
        "clock_mhz": report["chip"]["clock_mhz"],
        "measurement_trusted": m["trusted"],
        "cycles_match_design": m["cycles_match_design"],
        "design_cycles_per_token": built["cycles_per_token"],
        "design_cycles_source": built["sources"].get("cycles"),
        "clock_source": built["sources"]["clock"] if built["chips"].get("dut") else None,
    }
    # A metric the run could not measure is absent, not null: a check that needs it is then
    # indeterminate (DP-10), and `missing` in the report says why.
    return {k: v for k, v in metrics.items() if v is not None}


def conform(ctx: InvokeContext) -> Result:
    registry, built = _chips(ctx)
    prompts = _prompts(ctx)
    dut = _dut(built)
    quick = bool(ctx.params.get("quick", False))
    ctx.progress(0.1, f"conformance of {registry.get(dut).manifest.data['backend']}{' (quick)' if quick else ''}")
    ctx.check_cancelled()
    result = run_conform(registry, dut, prompts, int(ctx.params.get("maxTokens", 16)), quick=quick)
    checks = result["checks"]
    count = {v: sum(c["verdict"] == v for c in checks) for v in ("pass", "fail", "n/a", "not_run")}
    # The standard T-18/T-19 applied (the RTL chip's policy, written by `chips_from_artifacts`) and
    # the clock it measured at, so that the flow's check sees them: the check's completeness
    # holds them to the acceptance, and a lowered policy cannot pass unnoticed.
    policy = registry.get(dut).manifest.policy()
    rtl = built["chips"].get("dut") is not None
    acceptance = {
        "acceptance_tok_s_min": policy.get("speed", {}).get("tok_s_min") if rtl else None,
        "acceptance_bit_exact": policy.get("quality", {}).get("bit_exact") if rtl else None,
        "acceptance_max_tokens": policy.get("max_tokens") if rtl else None,
        "clock_source": built["sources"]["clock"] if rtl else "none",
    }
    report = {
        "format": "llmchip-vchip-conformance/1",
        "chip": {"id": dut, "backend": registry.get(dut).manifest.data["backend"]},
        "quick": quick,
        "prompts": len(prompts.cases),
        "max_tokens": int(ctx.params.get("maxTokens", 16)),
        "acceptance": {**acceptance, "policy": policy},
        "checks": checks,
        "built": built,
    }
    Path(ctx.out_dir, "conformance.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return Result(
        outputs=[Output("report", "conformance.json", "vchip.conformance", "application/json")],
        metrics={
            "chip_backend": report["chip"]["backend"],
            "quick": quick,
            "prompts": len(prompts.cases),
            **{k: v for k, v in acceptance.items() if v is not None},
            "checks": len(checks),
            "checks_passed": count["pass"],
            "checks_failed": count["fail"],
            "checks_not_applicable": count["n/a"],
            "checks_not_run": count["not_run"],
            "failed_checks": ",".join(c["id"] for c in checks if c["verdict"] == "fail"),
        },
        parse={"status": "complete", "parser": "vchip/conform"},
    )


def _stop(signum, _frame) -> None:
    """The kernel cancels a run with SIGTERM. The chips' simulators run in sessions and
    containers of their own, so end them before exiting, or they would outlive the run."""
    verilator.remove_builds()
    process.kill_all()
    os._exit(128 + signum)


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    run_plugin("vchip", "0.1.0", {"bench": bench, "conform": conform})


if __name__ == "__main__":
    main()
