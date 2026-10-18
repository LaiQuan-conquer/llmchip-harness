"""IF-2 entry point of the arch-taalas plugin."""

import json
from pathlib import Path

from llmchip_numeric import L2Model, storage_bits
from llmchip_sdk import InvokeContext, Output, Result, run_plugin

from . import design, verify


def handle_design(ctx: InvokeContext) -> Result:
    spec_in, weights_in = ctx.input("spec"), ctx.input("weights")
    l2 = L2Model.load(spec_in.path, weights_in.path)  # checks that the weights belong to the spec
    profile = json.loads(Path(ctx.input("profile").path).read_text())
    library = json.loads(Path(ctx.input("library").path).read_text())
    chip, ppa = design(
        l2.spec, spec_in.hash, weights_in.hash, storage_bits(l2.spec, l2.a), profile, library, ctx.params
    )
    out = Path(ctx.out_dir)
    (out / "chip-spec.json").write_text(json.dumps(chip, indent=2, ensure_ascii=False))
    (out / "ppa.json").write_text(json.dumps(ppa, indent=2, ensure_ascii=False))
    metrics = {
        "feasibility": ppa["conclusion"]["feasibility"],
        "tokens_per_second": chip["performance"]["tokensPerSecond"],
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
    return Result(
        metrics=verify(chip, spec, spec_in.hash, ppa), parse={"status": "complete", "parser": "llmchip-arch/verify"}
    )


if __name__ == "__main__":
    run_plugin("arch-taalas", "0.1.0", {"design": handle_design, "verify_spec": handle_verify})
