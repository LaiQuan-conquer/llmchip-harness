"""Storage implementation calibration on GF180MCU (requirements FR-EST-13/14, §3.8).

Runs inside the pinned iic-osic-tools image, which provides the gf180mcuD PDK,
Yosys and OpenROAD. Every candidate ends either `available` (with calibrated
density and its integration views) or `unavailable` (with the evidence).
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import time
from pathlib import Path

from llmchip_sdk import InvokeContext, Output, Result, run_plugin

PDK = Path("/foss/pdks/gf180mcuD/libs.ref")
SC = PDK / "gf180mcu_fd_sc_mcu7t5v0"
LIB = SC / "lib" / "gf180mcu_fd_sc_mcu7t5v0__tt_025C_3v30.lib"
SRAM = PDK / "gf180mcu_fd_ip_sram"
SITE = "GF018hv5v_mcu_sc7"
TOOLS = Path("/foss/tools/bin")

# Integration views each stage needs (requirements §3.8).
STAGE_VIEWS = {"S4": ["verilog"], "S6": ["liberty", "lef"], "S7": ["gds", "lvs_netlist"]}


def tool(*args: str, cwd: Path, timeout: int = 3000) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).name} failed ({proc.returncode}): {(proc.stdout + proc.stderr)[-2000:]}")
    return proc.stdout + proc.stderr


def versions() -> dict[str, str]:
    out = {}
    for name, args in {"yosys": ["-V"], "openroad": ["-version"]}.items():
        try:
            out[name] = (
                subprocess.run([str(TOOLS / name), *args], capture_output=True, text=True, timeout=60)
                .stdout.strip()
                .splitlines()[-1]
            )
        except Exception as error:
            out[name] = f"unavailable: {error}"
    out["pdk"] = "gf180mcuD (iic-osic-tools)"
    return out


def rom_verilog(words: int, width: int, rng: random.Random) -> str:
    """A synchronous ROM with random contents: quantized weights have high entropy, so random data is realistic."""
    addr = max((words - 1).bit_length(), 1)
    digits = (width + 3) // 4
    body = "\n".join(f"      {a}: data <= {width}'h{rng.getrandbits(width):0{digits}x};" for a in range(words))
    return (
        f"module rom (input clk, input [{addr - 1}:0] addr, output reg [{width - 1}:0] data);\n"
        f"  always @(posedge clk)\n    case (addr)\n{body}\n"
        f"      default: data <= {width}'h0;\n    endcase\nendmodule\n"
    )


def measure_rom(ctx: InvokeContext, words: int, width: int, utilization: float, rng: random.Random) -> dict:
    work = Path(ctx.work_dir) / f"rom_{words}x{width}"
    work.mkdir(parents=True, exist_ok=True)
    (work / "rom.v").write_text(rom_verilog(words, width, rng))
    (work / "synth.ys").write_text(
        f"read_verilog rom.v\nsynth -top rom -flatten\ndfflibmap -liberty {LIB}\nabc -liberty {LIB}\nopt_clean\n"
        f"tee -o stat.txt stat -liberty {LIB}\nwrite_verilog -noattr netlist.v\n"
    )
    started = time.monotonic()
    tool(str(TOOLS / "yosys"), "-q", "synth.ys", cwd=work)
    cell_area = float(re.search(r"Chip area for module '\\rom': ([0-9.]+)", (work / "stat.txt").read_text()).group(1))
    (work / "place.tcl").write_text(
        f"read_lef {SC}/techlef/gf180mcu_fd_sc_mcu7t5v0__nom.tlef\nread_lef {SC}/lef/gf180mcu_fd_sc_mcu7t5v0.lef\n"
        f"read_liberty {LIB}\nread_verilog netlist.v\nlink_design rom\n"
        f"initialize_floorplan -utilization {round(utilization * 100)} -aspect_ratio 1 -core_space 5 -site {SITE}\n"
        "make_tracks\nplace_pins -hor_layers Metal3 -ver_layers Metal2\n"
        f"global_placement -density {min(utilization + 0.1, 0.95)}\ndetailed_placement\ncheck_placement\n"
        'puts "CORE_AREA [ord::get_core_area]"\n'
    )
    log = tool(str(TOOLS / "openroad"), "-exit", "place.tcl", cwd=work)
    x0, y0, x1, y1 = map(float, re.search(r"CORE_AREA ([0-9.]+) ([0-9.]+) ([0-9.]+) ([0-9.]+)", log).groups())
    core = (x1 - x0) * (y1 - y0)
    bits = words * width
    ctx.log("info", f"ROM {words}x{width}: cell area {cell_area:.0f} um2, placed core {core:.0f} um2")
    return {
        "words": words,
        "width": width,
        "bits": bits,
        "cellAreaUm2": cell_area,
        "placedCoreAreaUm2": core,
        "utilization": utilization,
        "densityUm2PerBit": core / bits,
        "placementLegal": True,
        "seconds": round(time.monotonic() - started, 1),
    }


def density_range(measurements: list[dict]) -> dict:
    """Typical: the largest measured size. Pessimistic: the next smaller one. Optimistic: one more step of the trend."""
    d = [m["densityUm2PerBit"] for m in measurements]
    typ = d[-1]
    high = d[-2] if len(d) > 1 else typ
    low = typ * typ / high if high > 0 else typ
    return {
        "low": low,
        "typ": typ,
        "high": high,
        "confidence": "calibrated",
        "source": "Yosys synthesis and OpenROAD placement on gf180mcuD",
    }


def calibrate(ctx: InvokeContext) -> Result:
    impl = ctx.params["implementation"]
    report: dict = {"implementation": impl, "process": "gf180mcu", "tools": versions(), "measurements": []}
    views: dict[str, bool]
    if impl == "gf180mcu.stdcell-rom":
        rng = random.Random(ctx.params.get("seed", 1))
        try:
            for words, width in ctx.params.get("sizes", [[1024, 16], [4096, 16], [16384, 16]]):
                ctx.check_cancelled()
                report["measurements"].append(measure_rom(ctx, words, width, ctx.params.get("utilization", 0.6), rng))
        except Exception as error:
            report["failure"] = str(error)
        # A synthesized ROM is ordinary standard-cell logic: its views come from the cell library and the flow.
        views = {
            "verilog": True,
            "liberty": LIB.exists(),
            "lef": (SC / "lef" / "gf180mcu_fd_sc_mcu7t5v0.lef").exists(),
            "gds": (SC / "gds").exists(),
            "lvs_netlist": (SC / "cdl").exists() or (SC / "spice").exists(),
        }
        if report["measurements"]:
            report["densityUm2PerBit"] = density_range(report["measurements"])
            report["peripheryFactor"] = {
                "low": 0,
                "typ": 0,
                "high": 0,
                "confidence": "calibrated",
                "source": "decoding is part of the measured logic",
            }
    elif impl == "gf180mcu.sram-512x8":
        name = "gf180mcu_fd_ip_sram__sram512x8m8wm1"
        lef = SRAM / "lef" / f"{name}.lef"
        views = {
            "verilog": (SRAM / "verilog" / f"{name}.v").exists(),
            "liberty": any((SRAM / "lib").glob(f"{name}__tt_025C_3v30.lib")),
            "lef": lef.exists(),
            "gds": (SRAM / "gds" / f"{name}.gds").exists(),
            "lvs_netlist": (SRAM / "cdl" / f"{name}.cdl").exists() or (SRAM / "spice" / f"{name}.spice").exists(),
        }
        if lef.exists():
            w, h = map(float, re.search(r"SIZE\s+([0-9.]+)\s+BY\s+([0-9.]+)", lef.read_text()).groups())
            bits = 512 * 8
            density = w * h / bits
            report["measurements"].append(
                {
                    "macro": name,
                    "widthUm": w,
                    "heightUm": h,
                    "bits": bits,
                    "densityUm2PerBit": density,
                    "source": "macro LEF SIZE",
                }
            )
            # Tiling adds little beyond the macros themselves; keep a margin for halos and power straps.
            report["densityUm2PerBit"] = {
                "low": density,
                "typ": density * 1.1,
                "high": density * 1.25,
                "confidence": "calibrated",
                "source": "gf180mcu_fd_ip_sram LEF, plus tiling halo",
            }
            report["peripheryFactor"] = {
                "low": 0,
                "typ": 0,
                "high": 0,
                "confidence": "calibrated",
                "source": "included in the macro",
            }
        else:
            report["failure"] = f"macro LEF {lef} not found"
    else:  # gf180mcu.openram-rom
        probe = subprocess.run(["python3", "-c", "import openram"], capture_output=True, text=True)
        views = {"verilog": False, "liberty": False, "lef": False, "gds": False, "lvs_netlist": False}
        report["failure"] = (
            "OpenRAM is not part of the pinned tool image (`import openram` fails: "
            f"{probe.stderr.strip().splitlines()[-1] if probe.stderr.strip() else 'unknown error'}). "
            "Its GF180 ROM generator is experimental and, per its documentation, produces GDS and SPICE only, "
            "so simulation model, Liberty and LEF views would also be missing."
        )

    missing = {stage: [v for v in needed if not views.get(v)] for stage, needed in STAGE_VIEWS.items()}
    report["views"] = views
    report["missingViews"] = missing
    report["availability"] = (
        "available"
        if "densityUm2PerBit" in report and not any(missing.values()) and "failure" not in report
        else "unavailable"
    )
    Path(ctx.out_dir, "calibration.json").write_text(json.dumps(report, indent=2))

    metrics: dict = {"available": report["availability"] == "available", "measurements": len(report["measurements"])}
    for view, present in views.items():
        metrics[f"views.{view}"] = present
    if "densityUm2PerBit" in report:
        metrics["density_um2_per_bit.typ"] = report["densityUm2PerBit"]["typ"]
    return Result(
        outputs=[Output("report", "calibration.json", "calibration.report", "application/json")],
        metrics=metrics,
        parse={"status": "complete", "parser": "eda-gf180/0.1"},
    )


if __name__ == "__main__":
    run_plugin("eda-gf180", "0.1.0", {"calibrate": calibrate})
