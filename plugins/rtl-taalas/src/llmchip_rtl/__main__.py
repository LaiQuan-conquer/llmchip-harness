"""IF-2 entry point of the rtl-taalas plugin: RTL generation (S4) and compiler verification."""

import json
from pathlib import Path

import numpy as np
from llmchip_model import Tokenizer
from llmchip_model.evalset import load_evalset
from llmchip_numeric import L2Model
from llmchip_sdk import InvokeContext, Output, Result, run_plugin

from .bundle import bundle_files, write_tar
from .compiler import MAPPING_RULE, Compiled
from .isa import Machine, modes
from .spatial.bundle import bundle_files as spatial_bundle_files
from .spatial.generator import ARCHITECTURE as SPATIAL
from .spatial.generator import Spatial
from .spatial.sp_model import Machine as SpatialModel

SPATIAL_RULE = (
    "taalas-spatial/1: each matrix row is a bit-serial adder tree; weight q at input i adds the input "
    "shifted by each CSD digit of |q| with the sign of q; the group mantissa m adds the group sum "
    "shifted by each CSD digit of m; read back by evaluating the netlist's linear map (= q * m)"
)


def generate(ctx: InvokeContext) -> Result:
    spec_in, weights_in = ctx.input("spec"), ctx.input("weights")
    chip = json.loads(Path(ctx.input("chip").path).read_text())
    if (
        chip.get("numericSpec", {}).get("hash") != spec_in.hash
        or chip.get("weights", {}).get("hash") != weights_in.hash
    ):
        raise ValueError("the chip spec was designed from a different numeric spec or weights file")
    l2 = L2Model.load(spec_in.path, weights_in.path)
    if chip.get("architecture") == SPATIAL:
        return generate_spatial(ctx, l2, spec_in.hash, weights_in.hash, chip["dataflow"].get("dividers", 16))
    flow = chip["dataflow"]
    options = {
        "operandBuffers": flow.get("operandBuffers", "none"),
        "memoryInterface": flow.get("memoryInterface", "spi"),
    }
    compiled = Compiled(l2.spec, l2.a, flow["lanes"], options)
    restored = compiled.unmap()
    mismatched = sorted(k for k in l2.a if k not in restored or not np.array_equal(restored[k], l2.a[k]))
    if mismatched:
        raise ValueError(f"storage mapping round trip failed for {mismatched[:5]}")
    out = Path(ctx.out_dir)
    write_tar(
        bundle_files(compiled, {"numericSpec": spec_in.hash, "weights": weights_in.hash, "mappingRule": MAPPING_RULE}),
        out / "rtl.tar",
    )
    storage = compiled.storage_map(weights_in.hash) | {"roundTrip": {"arrays": len(l2.a), "identical": True}}
    (out / "storage-map.json").write_text(json.dumps(storage, indent=2))
    return Result(
        outputs=[
            Output("rtl", "rtl.tar", "rtl.bundle", "application/x-tar"),
            Output("storage", "storage-map.json", "storage.map", "application/json"),
        ],
        metrics={
            "lanes": compiled.lanes,
            "program_ops": len(compiled.program),
            "weight_rom_words": len(compiled.wrom),
            "data_memory_words": compiled.dmem_words,
            "mapping_round_trip": True,
            "operand_buffers": options["operandBuffers"],
            "memory_interface": options["memoryInterface"],
        },
        parse={"status": "complete", "parser": "llmchip-rtl/generate"},
    )


def generate_spatial(ctx: InvokeContext, l2: L2Model, spec_hash: str, weights_hash: str, dividers: int) -> Result:
    """The spatial engine: every matrix becomes circuits; the bundle check reads every circuit
    back from the generated netlist and compares its linear map with the weights (FR-RTL-02)."""
    sp = Spatial(l2.spec, l2.a, dividers=int(dividers))
    out = Path(ctx.out_dir)
    files = spatial_bundle_files(sp, {"numericSpec": spec_hash, "weights": weights_hash, "mappingRule": SPATIAL_RULE})
    write_tar(files, out / "rtl.tar")
    circuits = {
        m.name: {"rows": int(m.q.shape[0]), "adders": m.adders, "delayBits": m.delays, "streamBits": m.length}
        for m in sp.matrices.values()
    }
    storage = {
        "format": "llmchip-storage-map/2",
        "architecture": SPATIAL,
        "weights": weights_hash,
        "mappingRule": SPATIAL_RULE,
        "circuits": circuits,
        "roundTrip": {"matrices": len(circuits), "identical": True, "method": "linear map read back from the netlist"},
    }
    (out / "storage-map.json").write_text(json.dumps(storage, indent=2))
    return Result(
        outputs=[
            Output("rtl", "rtl.tar", "rtl.bundle", "application/x-tar"),
            Output("storage", "storage-map.json", "storage.map", "application/json"),
        ],
        metrics={
            "architecture": SPATIAL,
            "mapping_round_trip": True,
            "serial_adders": sum(c["adders"] for c in circuits.values()),
            "delay_bits": sum(c["delayBits"] for c in circuits.values()),
            "output_rows": sum(c["rows"] for c in circuits.values()),
            "cycles_per_token": sp.cycles_per_token(),
            "memory_interface": "none",
            "dividers": sp.dividers,
        },
        parse={"status": "complete", "parser": "llmchip-rtl/generate"},
    )


def check_isa(ctx: InvokeContext) -> Result:
    """Compiler verification: the shipped program on the instruction-level simulator equals L2."""
    import io
    import tarfile

    with tarfile.open(ctx.input("rtl").path) as tar:
        design = json.loads(tar.extractfile("bundle/design.json").read())
        if design.get("architecture") == SPATIAL:
            # The spatial bundle's model of its circuits (sp_model.py) against L2.
            with np.load(io.BytesIO(tar.extractfile("bundle/sp-image.npz").read())) as z:
                machine = SpatialModel({k: z[k] for k in z.files}, design["params"])
        else:
            image = np.load(io.BytesIO(tar.extractfile("bundle/isa-image.npz").read()))
    p = design["params"]
    if design.get("architecture") != SPATIAL:
        machine = Machine(
            [int(w) for w in image["program"]],
            image["wrom"],
            image["srom"],
            image["crom"],
            p["LANES"],
            p["NDMEM"],
            p["CTX"],
            **modes(p),
        )
    l2 = L2Model.load(ctx.input("spec").path, ctx.input("weights").path)
    tokenizer = Tokenizer(Path(ctx.input("tokenizer").path).read_bytes(), l2.vocab)
    evalset = load_evalset(ctx.input("evalset").path)
    values = mismatches = steps_total = 0
    first = None
    for i, prompt in enumerate(evalset["prompts"]):
        ctx.check_cancelled()
        ids = tokenizer.encode(prompt)
        steps: list = []
        l2.generate(ids, evalset["steps"], steps)
        token = ids[0]
        for pos, expected in enumerate(steps):
            traces: list = []
            out = machine.step(token, pos, traces)
            got = {(t["point"], t["layer"]): t["values"] for t in traces}
            pairs = [(got.get((1, layer)), v) for layer, v in enumerate(expected["attn_residual"])]
            pairs += [(got.get((2, layer)), v) for layer, v in enumerate(expected["ffn_residual"])]
            pairs += [(got.get((3, 0)), expected["logits"])]
            for a, b in pairs:
                values += len(b)
                if a is None or not np.array_equal(a, b):
                    mismatches += 1
                    first = first or {"prompt": i, "pos": pos}
            nxt = ids[pos + 1] if pos < len(ids) - 1 else out
            if nxt != expected["token"]:
                mismatches += 1
                first = first or {"prompt": i, "pos": pos}
            token = expected["token"]
            steps_total += 1
    Path(ctx.out_dir, "isa-check.json").write_text(
        json.dumps({"values": values, "mismatches": mismatches, "first": first})
    )
    return Result(
        outputs=[Output("report", "isa-check.json", "isa.check", "application/json")],
        metrics={"prompts": len(evalset["prompts"]), "steps": steps_total, "values": values, "mismatches": mismatches},
        parse={"status": "complete", "parser": "llmchip-rtl/check-isa"},
    )


if __name__ == "__main__":
    run_plugin("rtl-taalas", "0.1.0", {"generate": generate, "check_isa": check_isa})
