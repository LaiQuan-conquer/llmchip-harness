"""IF-2 entry point of the arch-taalas plugin."""

import json
from pathlib import Path

from llmchip_numeric import L2Model, storage_bits
from llmchip_sdk import InvokeContext, Output, Result, run_plugin

from . import design, explore, verify


def handle_design(ctx: InvokeContext) -> Result:
    spec_in, weights_in = ctx.input("spec"), ctx.input("weights")
    l2 = L2Model.load(spec_in.path, weights_in.path)  # checks that the weights belong to the spec
    profile = json.loads(Path(ctx.input("profile").path).read_text())
    library = json.loads(Path(ctx.input("library").path).read_text())
    chip, ppa = design(
        l2.spec, spec_in.hash, weights_in.hash, storage_bits(l2.spec, l2.a), profile, library, ctx.params, l2.a
    )
    out = Path(ctx.out_dir)
    (out / "chip-spec.json").write_text(json.dumps(chip, indent=2, ensure_ascii=False))
    (out / "ppa.json").write_text(json.dumps(ppa, indent=2, ensure_ascii=False))
    metrics = {
        "feasibility": ppa["conclusion"]["feasibility"],
        "tokens_per_second": chip["performance"]["tokensPerSecond"],
        "cycles_per_token": chip["performance"]["cyclesPerToken"],
        "cycles_per_token_with_traces": chip["performance"]["cyclesPerTokenWithTraces"],
        "memory_interface": chip["dataflow"]["memoryInterface"],
        "operand_buffers": chip["dataflow"]["operandBuffers"],
        "lanes": chip["dataflow"]["lanes"],
        "clock_mhz": chip["targets"]["clockMHz"],
        "stored_bits": sum(storage_bits(l2.spec, l2.a).values()),
    }
    if "area" in ppa:
        metrics["die_area_mm2.typ"] = ppa["area"]["dieMm2"]["typ"]
    return Result(
        outputs=[
            Output("chip", "chip-spec.json", "chip.spec", "application/json"),
            Output("ppa", "ppa.json", "ppa.estimate", "application/json"),
        ],
        metrics=metrics,
        parse={"status": "complete", "parser": "llmchip-arch/design"},
    )


def handle_verify(ctx: InvokeContext) -> Result:
    chip = json.loads(Path(ctx.input("chip").path).read_text())
    spec_in = ctx.input("spec")
    spec = json.loads(Path(spec_in.path).read_text())
    ppa = json.loads(Path(ctx.input("ppa").path).read_text())
    weights = next((i for i in ctx.inputs if i.name == "weights"), None)
    arrays = L2Model.load(spec_in.path, weights.path).a if weights else None
    return Result(
        metrics=verify(chip, spec, spec_in.hash, ppa, arrays),
        parse={"status": "complete", "parser": "llmchip-arch/verify"},
    )


def handle_explore(ctx: InvokeContext) -> Result:
    """Design-space exploration; for a graph numeric spec (an imported model, S2 analysis) the
    model is explored from its shape and the result is an analysis."""
    spec_in, weights_in = ctx.input("spec"), ctx.input("weights")
    spec = json.loads(Path(spec_in.path).read_text())
    profile = json.loads(Path(ctx.input("profile").path).read_text())
    library = json.loads(Path(ctx.input("library").path).read_text())
    if spec.get("format") == explore.GRAPH_SPEC_FORMAT:
        arrays, storage = None, explore.npz_storage_bits(spec, weights_in.path)
    else:
        l2 = L2Model.load(spec_in.path, weights_in.path)
        arrays, storage = l2.a, storage_bits(l2.spec, l2.a)
    ctx.progress(0.05, "exploring the design space")
    result = explore.explore(spec, arrays, storage, profile, library, ctx.params)
    Path(ctx.out_dir, "exploration.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    chosen = result["chosen"]
    return Result(
        outputs=[Output("exploration", "exploration.json", "arch.exploration", "application/json")],
        metrics={
            "points": len(result["points"]),
            "points_fitting": sum(p["fits"] for p in result["points"]),
            "analysis": result["analysis"],
            "tokens_per_second": chosen["tokensPerSecond"],
            "cycles_per_token": chosen["cyclesPerToken"],
            "fits": chosen["fits"],
            "limits": len(chosen["limits"]),
            **({"die_area_mm2.typ": chosen["dieMm2"]["typ"]} if chosen["dieMm2"] else {}),
        },
        parse={"status": "complete", "parser": "llmchip-arch/explore"},
    )


if __name__ == "__main__":
    run_plugin(
        "arch-taalas", "0.2.0", {"design": handle_design, "verify_spec": handle_verify, "explore": handle_explore}
    )
