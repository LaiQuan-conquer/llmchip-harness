"""IF-2 entry point of the packager plugin: tapeout package (S8) and its integrity check."""

import json
import tarfile
from pathlib import Path

from llmchip_sdk import InvokeContext, Output, Result, run_plugin

from . import bonding_svg, build, check, import_external, test_plan

SOURCES = ("gds", "chip", "pads", "drc", "lvs", "sta", "constraints", "exceptions", "measurements")


def handle_build(ctx: InvokeContext) -> Result:
    template = json.loads(Path(ctx.input("template").path).read_text())
    inputs = {name: (Path(ctx.input(name).path).read_bytes(), ctx.input(name).hash) for name in SOURCES}
    chip = json.loads(inputs["chip"][0])
    pads = json.loads(inputs["pads"][0])
    with tarfile.open(ctx.input("layout").path) as tar:
        layers = tar.extractfile("views/layers.map").read()
    with tarfile.open(ctx.input("rtl").path) as tar:
        design = json.loads(tar.extractfile("bundle/design.json").read())
    l2 = json.loads(Path(ctx.input("l2").path).read_text())
    generated = {
        "layer map": json.dumps(
            {"topCell": template["topCell"], "layerMap": layers.decode(errors="replace")}, indent=2
        ).encode(),
        "bonding diagram": bonding_svg(pads).encode(),
        "test plan": test_plan(chip, l2, design).encode(),
        "readme": (
            f"# Tapeout package: {template['process']}\n\nTop cell `{template['topCell']}`. "
            "MANIFEST.json lists every file with its hash and the workbench artifact it came from; "
            "the export archive adds the full lineage up to the model weights.\n"
        ).encode(),
    }
    data, manifest = build(template, inputs, generated)
    out = Path(ctx.out_dir)
    (out / "package.tar").write_bytes(data)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return Result(
        outputs=[
            Output("package", "package.tar", "tapeout.package", "application/x-tar"),
            Output("manifest", "manifest.json", "package.manifest", "application/json"),
        ],
        metrics={"files": len(manifest["files"])},
        parse={"status": "complete", "parser": "llmchip-packager/build"},
    )


def handle_check(ctx: InvokeContext) -> Result:
    template = json.loads(Path(ctx.input("template").path).read_text())
    result = check(Path(ctx.input("package").path).read_bytes(), template, ctx.input("gds").hash)
    Path(ctx.out_dir, "integrity.json").write_text(json.dumps(result, indent=2))
    metrics = {k: v for k, v in result.items() if not isinstance(v, list)}
    return Result(
        outputs=[Output("report", "integrity.json", "package.integrity", "application/json")],
        metrics=metrics,
        parse={"status": "complete", "parser": "llmchip-packager/check"},
    )


def handle_import(ctx: InvokeContext) -> Result:
    """Imports an external signoff report and judges whether it belongs to the current GDS."""
    report = json.loads(Path(ctx.input("report").path).read_text())
    result = import_external(report, ctx.input("gds").hash)
    status = "with the current design" if result["matches_current_design"] else "does not match the current design"
    Path(ctx.out_dir, "external.json").write_text(json.dumps({**result, "status": status, "report": report}, indent=2))
    return Result(
        outputs=[Output("record", "external.json", "external.signoff", "application/json")],
        metrics={k: v for k, v in result.items() if not isinstance(v, list)},
        parse={"status": "complete", "parser": "llmchip-packager/import"},
    )


if __name__ == "__main__":
    run_plugin("packager", "0.1.0", {"build": handle_build, "check": handle_check, "import_external": handle_import})
