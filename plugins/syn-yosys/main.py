"""IF-2 entry point of the syn-yosys plugin (runs in the pinned EDA image)."""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

from dft import atpg, scan_chain
from llmchip_sdk import InvokeContext, Output, Result, run_plugin
from synth import synthesize
from timing import timing


def _bundle(ctx: InvokeContext) -> Path:
    bundle = Path(ctx.out_dir) / "bundle"
    bundle.mkdir()
    with tarfile.open(ctx.input("rtl").path) as archive:
        archive.extractall(bundle)
    return bundle


def handle_synth(ctx: InvokeContext) -> Result:
    bundle = _bundle(ctx)
    pdk = str(ctx.params.get("pdk", "sky130"))
    top = str(ctx.params.get("top", "tile_slice_top"))
    report = synthesize(bundle, Path(ctx.out_dir), pdk, top)
    netlist = Path(ctx.out_dir) / "netlist.v"
    shutil.copyfile(Path(ctx.out_dir) / "syn" / "netlist.v", netlist)
    safe = pdk.replace("-", "_")
    metrics = {
        f"syn_{safe}": 1,
        f"syn_area_{safe}": report["area"],
        f"syn_cells_{safe}": report["cells"],
        f"syn_flip_flops_{safe}": report["flipFlops"],
    }
    return Result(
        outputs=[
            Output("report", "syn-report.json", "syn.report", "application/json"),
            Output("netlist", "netlist.v", "syn.netlist", "text/plain"),
        ],
        metrics=metrics,
    )


def handle_dft(ctx: InvokeContext) -> Result:
    bundle = _bundle(ctx)
    pdk = str(ctx.params.get("pdk", "sky130"))
    top = str(ctx.params.get("top", "tile_slice_top"))
    synthesize(bundle, Path(ctx.out_dir), pdk, top)
    report = scan_chain(Path(ctx.out_dir) / "syn" / "netlist.v", Path(ctx.out_dir), pdk)
    chained = Path(ctx.out_dir) / "dft" / "chained.v"
    output = Path(ctx.out_dir) / "chained.v"
    if chained.is_file():
        shutil.copyfile(chained, output)
    safe = pdk.replace("-", "_")
    metrics = {
        f"syn_dft_scan_{safe}": 1 if report["ok"] else 0,
        f"syn_dft_scan_length_{safe}": report["total"],
        f"syn_dft_internal_{safe}": report["internal"],
        f"syn_dft_boundary_{safe}": report["boundary"],
    }
    return Result(
        outputs=[
            Output("report", "dft-report.json", "syn.dft.report", "application/json"),
            Output("chained", "chained.v", "syn.netlist", "text/plain"),
        ],
        metrics=metrics,
    )


def handle_atpg(ctx: InvokeContext) -> Result:
    bundle = _bundle(ctx)
    pdk = str(ctx.params.get("pdk", "sky130"))
    report = atpg(bundle, Path(ctx.out_dir), pdk)
    safe = pdk.replace("-", "_")
    metrics = {
        f"syn_dft_atpg_{safe}": 1 if report["ok"] else 0,
        f"syn_dft_coverage_{safe}": report["coverage"],
    }
    return Result(
        outputs=[Output("report", "atpg-report.json", "syn.dft.report", "application/json")],
        metrics=metrics,
    )


def handle_timing(ctx: InvokeContext) -> Result:
    bundle = _bundle(ctx)
    pdk = str(ctx.params.get("pdk", "sky130"))
    top = str(ctx.params.get("top", "tile_slice_top"))
    period = float(ctx.params.get("period", 100.0))
    synthesize(bundle, Path(ctx.out_dir), pdk, top)
    report = timing(Path(ctx.out_dir) / "syn" / "netlist.v", Path(ctx.out_dir), pdk, period)
    safe = pdk.replace("-", "_")
    metrics = {
        f"syn_timing_{safe}": 1 if report["ok"] else 0,
        f"syn_timing_wns_{safe}": report["wns"],
        f"syn_timing_tns_{safe}": report["tns"],
    }
    return Result(
        outputs=[Output("report", "timing-report.json", "syn.timing.report", "application/json")],
        metrics=metrics,
    )


if __name__ == "__main__":
    run_plugin({"synth": handle_synth, "dft": handle_dft, "atpg": handle_atpg, "timing": handle_timing})
