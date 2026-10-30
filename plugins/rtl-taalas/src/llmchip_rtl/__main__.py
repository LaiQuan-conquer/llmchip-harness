"""IF-2 entry point of the rtl-taalas plugin: RTL generation (S4) and compiler verification."""

import json
import os
import tarfile
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
from .tile import checks as tile_checks
from .tile import generator as tile_generator
from .tile import verify as tile_verify

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


def tile_generate(ctx: InvokeContext) -> Result:
    """The `taalas-tile/1` RTL bundle of a slice: templates, view-1 ROM contents, manifest (FR-RTL-01/02)."""
    spec = json.loads(Path(ctx.input("spec").path).read_text())
    slice_report = json.loads(Path(ctx.input("slice").path).read_text())
    with np.load(ctx.input("weights").path, allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files if k != "meta"}
    bundle = Path(ctx.work_dir, "bundle")
    manifest = tile_generator.generate(
        spec, arrays, slice_report, macs=ctx.params.get("macsPerEngine", 128), out=bundle
    )
    Path(ctx.out_dir, "design.json").write_text(json.dumps(manifest, indent=2))
    tar = Path(ctx.out_dir, "tile-rtl.tar")
    with tarfile.open(tar, "w") as archive:
        archive.add(bundle, arcname=".")
    return Result(
        outputs=[
            Output("rtl", "tile-rtl.tar", "rtl.bundle", "application/x-tar"),
            Output("manifest", "design.json", "rtl.manifest", "application/json", "internal"),
        ],
        metrics={
            "banks": len(manifest["banks"]),
            "words": sum(bank["words"] for bank in manifest["banks"]),
            "weights": manifest["slice"]["weights"],
            "macs": manifest["mapping"]["macs"],
            "bits": manifest["mapping"]["bits"],
        },
        parse={"status": "complete", "parser": "llmchip-rtl/tile-generate"},
    )


def verify_tile(ctx: InvokeContext) -> Result:
    """R4: the verification report of a tile bundle: templates, the S-1 slice, the clock domains, the
    layer harness, coverage, formal, mutation, DFT readiness and the tiny full-model equivalence."""
    bundle = Path(ctx.work_dir, "bundle")
    bundle.mkdir(parents=True, exist_ok=True)
    with tarfile.open(ctx.input("rtl").path) as archive:
        archive.extractall(bundle)
    report = tile_verify.report(
        bundle,
        run_unit=bool(ctx.params.get("unit", True)),
        run_slice=bool(ctx.params.get("slice", True)),
        run_layer=bool(ctx.params.get("layer", True)),
        run_directed=bool(ctx.params.get("directed", True)),
    )
    Path(ctx.out_dir, "verification.json").write_text(json.dumps(report, indent=2))
    items = {item["id"]: item for item in report["items"]}

    def passed(item_id: str) -> int:
        return int(items.get(item_id, {}).get("status") == "passed")

    metrics = {
        "ver_unit": passed("ver.unit"),
        "ver_slice": passed("ver.slice"),
        "ver_cdc": passed("ver.cdc"),
        "ver_layer": passed("ver.layer"),
        "ver_directed": passed("ver.directed"),
        "ver_directed_cases": int(items.get("ver.directed", {}).get("metrics", {}).get("cases", 0)),
        "ver_coverage": passed("ver.coverage"),
        "ver_formal": passed("ver.formal"),
        "ver_mutation": passed("ver.mutation"),
        "ver_dft": passed("ver.dft"),
        "rtl_equivalence": passed("rtl.tile.equivalence"),
        "rtl_equivalence_logits": int(items.get("rtl.tile.equivalence", {}).get("metrics", {}).get("logits", 0)),
        "ver_unit_tests": int(items.get("ver.unit", {}).get("metrics", {}).get("tests", 0)),
        "ver_slice_tests": int(items.get("ver.slice", {}).get("metrics", {}).get("tests", 0)),
        "ver_cdc_domains": int(items.get("ver.cdc", {}).get("metrics", {}).get("domains", 0)),
        "ver_cdc_violations": int(items.get("ver.cdc", {}).get("metrics", {}).get("violations", 0)),
        "ver_layer_layers": int(items.get("ver.layer", {}).get("metrics", {}).get("layers", 0)),
        "ver_layer_observations": int(items.get("ver.layer", {}).get("metrics", {}).get("observations", 0)),
        "ver_layer_tests": int(items.get("ver.layer", {}).get("metrics", {}).get("tests", 0)),
        "ver_coverage_line": float(items.get("ver.coverage", {}).get("metrics", {}).get("line", 0.0)),
        "ver_formal_tests": int(items.get("ver.formal", {}).get("metrics", {}).get("tests", 0)),
        "ver_mutation_score": float(items.get("ver.mutation", {}).get("metrics", {}).get("score", 0.0)),
        "ver_dft_tests": int(items.get("ver.dft", {}).get("metrics", {}).get("tests", 0)),
        "ver_not_run": len(report["summary"]["notRun"]),
        "ver_complete": int(bool(report["summary"]["complete"])),
    }
    return Result(
        outputs=[Output("report", "verification.json", "verification.report", "application/json")],
        metrics=metrics,
        parse={"status": "complete", "parser": "llmchip-rtl/tile-verify"},
    )


def tile_check(ctx: InvokeContext) -> Result:
    """The RTL checks of a tile bundle: roundtrip, rules, the three linters, Yosys (FR-RTL-03)."""
    bundle = Path(ctx.work_dir, "bundle")
    bundle.mkdir(parents=True, exist_ok=True)
    with tarfile.open(ctx.input("rtl").path) as archive:
        archive.extractall(bundle)
    tools = Path(os.environ.get("LLMCHIP_TOOLS", Path.home() / "projects" / "deepseek-harness" / "tools"))
    verilator = tools / "oss-cad-suite" / "bin" / "verilator"
    yosys = tools / "oss-cad-suite" / "bin" / "yosys"
    verible = tools / "verible" / "bin" / "verible-verilog-lint"
    slang = tools / "oss-cad-suite" / "bin" / "slang-tidy"
    report = tile_checks.all_checks(
        bundle,
        verilator if verilator.is_file() else None,
        yosys if yosys.is_file() else None,
        verible if verible.is_file() else None,
        slang if slang.is_file() else None,
    )
    Path(ctx.out_dir, "rtl-checks.json").write_text(json.dumps(report, indent=2))
    return Result(
        outputs=[Output("report", "rtl-checks.json", "rtl.checks", "application/json")],
        metrics={
            "banks": len(tile_checks.manifest(bundle)["banks"]),
            "roundtrip_mismatches": report["roundtrip"]["roundtrip_mismatches"],
            "rule_violations": report["rules"]["violations"],
            "lint_warnings": report["lint"].get("warnings", -1),
            "lint_returncode": report["lint"].get("returncode", -1),
            "lint_tools": report["lint"].get("tools", 0),
            "synthesizable": int(report["synthesizable"].get("returncode", -1) == 0),
            "rom_views": int(bool(report["romViews"].get("equivalent"))),
            "rom_views3": int(bool(report["romViews"].get("equivalent3"))),
            "checked": int(bool(report["passed"])),
        },
        parse={"status": "complete", "parser": "llmchip-rtl/tile-check"},
    )


if __name__ == "__main__":
    run_plugin(
        "rtl-taalas",
        "0.2.0",
        {
            "generate": generate,
            "check_isa": check_isa,
            "tile_generate": tile_generate,
            "tile_check": tile_check,
            "tile_verify": verify_tile,
        },
    )
