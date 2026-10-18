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
from .isa import Machine


def generate(ctx: InvokeContext) -> Result:
    spec_in, weights_in = ctx.input("spec"), ctx.input("weights")
    chip = json.loads(Path(ctx.input("chip").path).read_text())
    if (
        chip.get("numericSpec", {}).get("hash") != spec_in.hash
        or chip.get("weights", {}).get("hash") != weights_in.hash
    ):
        raise ValueError("the chip spec was designed from a different numeric spec or weights file")
    l2 = L2Model.load(spec_in.path, weights_in.path)
    compiled = Compiled(l2.spec, l2.a, chip["dataflow"]["lanes"])
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
        },
        parse={"status": "complete", "parser": "llmchip-rtl/generate"},
    )


def check_isa(ctx: InvokeContext) -> Result:
    """Compiler verification: the shipped program on the instruction-level simulator equals L2."""
    import io
    import tarfile

    with tarfile.open(ctx.input("rtl").path) as tar:
        image = np.load(io.BytesIO(tar.extractfile("bundle/isa-image.npz").read()))
        design = json.loads(tar.extractfile("bundle/design.json").read())
    p = design["params"]
    machine = Machine(
        [int(w) for w in image["program"]],
        image["wrom"],
        image["srom"],
        image["crom"],
        p["LANES"],
        p["NDMEM"],
        p["CTX"],
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
