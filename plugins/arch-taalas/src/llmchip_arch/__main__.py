"""IF-2 entry point of the arch-taalas plugin."""

import json
from pathlib import Path

import numpy as np
from llmchip_numeric import L2Model, graph_l2, storage_bits
from llmchip_sdk import InvokeContext, Output, Result, run_plugin

from . import design, explore, spatial, tile, tlm, verify
from .slicing import slice_spec


def handle_design(ctx: InvokeContext) -> Result:
    spec_in, weights_in = ctx.input("spec"), ctx.input("weights")
    if ctx.params.get("architecture") == tile.ARCHITECTURE:
        spec = json.loads(Path(spec_in.path).read_text())
        with np.load(weights_in.path, allow_pickle=False) as z:
            arrays = {k: z[k] for k in z.files if k != "meta"}
        chip = tile.design(
            spec,
            ctx.params,
            graph_l2.storage_bits(spec, arrays),
            json.loads(Path(ctx.input("profile").path).read_text()),
            json.loads(Path(ctx.input("library").path).read_text()),
        )
        Path(ctx.out_dir, "chip-spec.json").write_text(json.dumps(chip, indent=2, ensure_ascii=False))
        outputs = [Output("chip", "chip-spec.json", "chip.spec", "application/json")]
        if chip.get("area"):
            Path(ctx.out_dir, "ppa.json").write_text(json.dumps(chip["area"], indent=2, ensure_ascii=False))
            outputs.append(Output("ppa", "ppa.json", "ppa.estimate", "application/json"))
        return Result(
            outputs=outputs,
            metrics={
                "architecture": chip["architecture"],
                "engines": chip["dataflow"]["engines"],
                "macs_per_engine": chip["dataflow"]["macsPerEngine"],
                "cycles_per_token": chip["performance"]["cyclesPerToken"],
                "tokens_per_second": chip["performance"]["tokensPerSecond"],
                "clock_mhz": chip["targets"]["clockMHz"],
            },
            parse={"status": "complete", "parser": "llmchip-arch/design-tile"},
        )
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
        "architecture": chip["architecture"],
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
    if "structure" in ppa:  # spatial: the circuits and the die chosen from their measured area
        s = ppa["structure"]
        die = ppa["area"]["chosenDie"]
        metrics |= {
            "serial_adders": s["serialAdders"],
            "kv_bits": s["kvBits"],
            "cell_area_mm2.typ": round(ppa["area"]["cellsMm2"]["typ"], 4),
            "die_x_mm": die["xMm"],
            "die_y_mm": die["yMm"],
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
    if chip.get("architecture") == tile.ARCHITECTURE:
        return Result(metrics=tile.verify(chip), parse={"status": "complete", "parser": "llmchip-arch/verify-tile"})
    spec_in = ctx.input("spec")
    spec = json.loads(Path(spec_in.path).read_text())
    ppa_input = next((i for i in ctx.inputs if i.name == "ppa"), None)
    ppa = json.loads(Path(ppa_input.path).read_text()) if ppa_input else {}
    weights = next((i for i in ctx.inputs if i.name == "weights"), None)
    arrays = L2Model.load(spec_in.path, weights.path).a if weights else None
    return Result(
        metrics=verify(chip, spec, spec_in.hash, ppa, arrays),
        parse={"status": "complete", "parser": "llmchip-arch/verify"},
    )


def handle_tlm(ctx: InvokeContext) -> Result:
    """The transaction-level trace of the tile on a token sequence: phases, points, cycles (FR-ARC)."""
    chip = json.loads(Path(ctx.input("chip").path).read_text())
    graph = json.loads(Path(ctx.input("graph").path).read_text())
    spec = json.loads(Path(ctx.input("spec").path).read_text())
    with np.load(ctx.input("weights").path, allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files if k != "meta"}
    tokens = ctx.params.get("tokens") or list(range(1, 9))
    report = tlm.trace(chip, graph, spec, arrays, [int(t) for t in tokens])
    Path(ctx.out_dir, "tlm.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return Result(
        outputs=[Output("trace", "tlm.json", "arch.tlm", "application/json")],
        metrics={
            "tokens": report["tokens"],
            "phases": report["phases"],
            "points": report["pointCount"],
            "mismatches": report["mismatches"],
            "cycles": report["cycles"],
            "cycles_agree": int(report["cyclesAgree"]),
            "deterministic": int(report["deterministic"]),
        },
        parse={"status": "complete", "parser": "llmchip-arch/tlm"},
    )


def handle_slice(ctx: InvokeContext) -> Result:
    """The slice of layer 0 at a level of the plan: its matrices, their alignment and its points."""
    graph = json.loads(Path(ctx.input("graph").path).read_text())
    spec = json.loads(Path(ctx.input("spec").path).read_text())
    report = slice_spec(
        graph, spec, ctx.params.get("level", "S-1"), ctx.params.get("layer", 0), ctx.params.get("channels", 128)
    )
    Path(ctx.out_dir, "slice.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return Result(
        outputs=[Output("slice", "slice.json", "arch.slice", "application/json")],
        metrics={
            "level": report["level"],
            "matrices": len(report["matrices"]),
            "weights": report["total"]["weights"],
            "bits": report["total"]["bits"],
            "observation_points": len(report["observationPoints"]),
        },
        parse={"status": "complete", "parser": "llmchip-arch/slice"},
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
    if ctx.params.get("architecture") == spatial.ARCHITECTURE:
        result = spatial.explore(spec, arrays, storage, profile, library, ctx.params)
        chosen = result["chosen"]
        Path(ctx.out_dir, "exploration.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return Result(
            outputs=[Output("exploration", "exploration.json", "arch.exploration", "application/json")],
            metrics={
                "architecture": spatial.ARCHITECTURE,
                "points": len(result["points"]),
                "points_fitting": sum(p["fits"] for p in result["points"]),
                "analysis": result["analysis"],
                "tokens_per_second": chosen["tokensPerSecond"],
                "cycles_per_token": chosen["cyclesPerToken"],
                "fits": chosen["fits"],
                "limits": len(result["limits"]),
                "die_area_mm2.typ": chosen["dieMm2"]["typ"],
            },
            parse={"status": "complete", "parser": "llmchip-arch/explore"},
        )
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
        "arch-taalas",
        "0.3.0",
        {
            "design": handle_design,
            "verify_spec": handle_verify,
            "explore": handle_explore,
            "slice": handle_slice,
            "tlm": handle_tlm,
        },
    )
