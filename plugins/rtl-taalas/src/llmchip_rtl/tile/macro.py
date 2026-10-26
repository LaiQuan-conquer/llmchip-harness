"""The hard-macro view of the banked ROM of a `taalas-tile/1` bundle.

A hard macro is a fixed block: the parent synthesises it as a black box, and the physical flow
places and routes it once. This module builds that view from the bundle's four `rom_bank0..3.hex`
files, which already carry the contents (the slice's bank set):

- `tile_rom_macro.v` is the black box the parent synthesises (`(* blackbox *)`, no contents);
- `tile_rom_macro_sim.v` is the behavioural model with the four `$readmemh` banks, for simulation;
- `tile_rom_macro.lef` is a provisional LEF: its size comes from the calibrated standard-cell ROM
  density (2.578 um2/bit for gf180mcu, `process-library`), not from a placed and routed macro, so
  the flow can floorplan it before the hardening run replaces the estimate with the real size;
- `rom_macro.json` records the contents, the digests, the latency contract and the area estimate.

The three warnings this view must carry are in the manifest: the LEF is an estimate, the macro is
combinational (the consumer's S1 register is the read register), and no hardening run is included.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path

MACRO = "tile_rom_macro"
DENSITY_UM2_PER_BIT = 2.578  # gf180mcu.stdcell-rom, calibrated (process-library/data/library-180nm.json)
ASPECT = 1.6  # width / height of the estimated macro


def _lines(path: Path) -> list[str]:
    return path.read_text().split()


def _digest(words: list[str]) -> str:
    return hashlib.sha256("\n".join(words).encode()).hexdigest()


def _size(width: int, depth: int) -> tuple[float, float]:
    area = width * depth * DENSITY_UM2_PER_BIT
    height = math.sqrt(area / ASPECT)
    return area, height


def _pin(name: str, direction: str, use: str, rect: tuple[float, float, float, float]) -> list[str]:
    x1, y1, x2, y2 = (round(value, 3) for value in rect)
    return [
        f"  PIN {name}",
        f"    DIRECTION {direction} ;",
        f"    USE {use} ;",
        "    PORT",
        "      LAYER Metal2 ;",
        f"      RECT {x1} {y1} {x2} {y2} ;",
        "    END",
        f"  END {name}",
    ]


def lef(width: int, depth: int, addr_bits: int) -> str:
    """A provisional LEF: pins on the left (addr) and right (q) edges, power at the bottom."""
    _, height = _size(width, depth)
    area, _ = _size(width, depth)
    width_um = area / height
    lines = [
        "# PROVISIONAL LEF of the banked ROM hard macro.",
        f"# Area from the calibrated standard-cell ROM density {DENSITY_UM2_PER_BIT} um2/bit;",
        "# replace with the placed-and-routed macro's LEF after the hardening run.",
        "VERSION 1.0 ;",
        "NAMESCASESENSITIVE ON ;",
        'BUSBITCHARS "[]" ;',
        'DIVIDERCHAR "/" ;',
        f"MACRO {MACRO}",
        "  CLASS CORE ;",
        "  ORIGIN 0 0 ;",
        f"  FOREIGN {MACRO} 0 0 ;",
        f"  SIZE {width_um:.3f} BY {height:.3f} ;",
        "  SYMMETRY X Y ;",
    ]
    pitch = height / (addr_bits + 1)
    for index in range(addr_bits):
        y = (index + 0.5) * pitch
        lines += _pin(f"addr[{index}]", "INPUT", "SIGNAL", (0.0, y - 0.1, 0.3, y + 0.1))
    pitch = height / (width + 1)
    for index in range(width):
        y = (index + 0.5) * pitch
        lines += _pin(f"q[{index}]", "OUTPUT", "SIGNAL", (width_um - 0.3, y - 0.1, width_um, y + 0.1))
    lines += _pin("VDD", "INOUT", "POWER", (0.5, 0.0, 2.0, 0.3))
    lines += _pin("VSS", "INOUT", "GROUND", (width_um - 2.0, 0.0, width_um - 0.5, 0.3))
    lines += ["END " + MACRO, ""]
    return "\n".join(lines)


def blackbox(width: int, addr_bits: int) -> str:
    return f"""// The hard macro's synthesis view: a black box. The contents are the macro's mask, not RTL.
`default_nettype none
(* blackbox *)
module {MACRO} #(
  parameter integer WIDTH = {width},
  parameter integer ADDR = {addr_bits}
) (
  input  wire [ADDR-1:0]   addr,
  output wire [WIDTH-1:0]  q
);
endmodule
"""


def simulation(width: int, addr_bits: int) -> str:
    return f"""// The hard macro's simulation view: the four bank files the macro is built from.
`default_nettype none
module {MACRO} #(
  parameter integer WIDTH = {width},
  parameter integer ADDR = {addr_bits},
  parameter         INIT0 = "rom_bank0.hex",
  parameter         INIT1 = "rom_bank1.hex",
  parameter         INIT2 = "rom_bank2.hex",
  parameter         INIT3 = "rom_bank3.hex"
) (
  input  wire [ADDR-1:0]   addr,
  output wire [WIDTH-1:0]  q
);

  localparam integer PER = 1 << (ADDR - 2);

  localparam integer OFFBITS = (ADDR > 2) ? (ADDR - 2) : 1;
  wire [1:0]        bank = addr[ADDR-1 -: 2];
  wire [OFFBITS-1:0] off;
  generate
    if (ADDR > 2) begin : g_off
      assign off = addr[ADDR-3:0];
    end else begin : g_flat
      assign off = 1'b0;
    end
  endgenerate

  reg [WIDTH-1:0] mem0 [0:PER-1];
  reg [WIDTH-1:0] mem1 [0:PER-1];
  reg [WIDTH-1:0] mem2 [0:PER-1];
  reg [WIDTH-1:0] mem3 [0:PER-1];

  initial begin
    $readmemh(INIT0, mem0);
    $readmemh(INIT1, mem1);
    $readmemh(INIT2, mem2);
    $readmemh(INIT3, mem3);
  end

  assign q = (bank == 2'd0) ? mem0[off] : (bank == 2'd1) ? mem1[off] : (bank == 2'd2) ? mem2[off] : mem3[off];

endmodule
"""


def generate(bundle: Path, density: float = DENSITY_UM2_PER_BIT, width: int | None = None) -> dict:
    """Writes `rom_macro/` for the bundle's root bank set and returns its manifest."""
    if width is None:
        manifest = json.loads((bundle / "design.json").read_text())
        width = int(manifest["mapping"]["macs"]) * int(manifest["mapping"]["bits"])
    banks = [bundle / f"rom_bank{index}.hex" for index in range(4)]
    if not all(path.is_file() for path in banks):
        raise ValueError("the bundle has no rom_bank0..3.hex")
    words = [_lines(path) for path in banks]
    per = len(words[0])
    if any(len(bank) != per for bank in words):
        raise ValueError("the four bank files have different depths")
    addr_bits = max(2, (4 * per - 1).bit_length())
    if 4 * per != 1 << addr_bits:
        raise ValueError("the bank set is not a 4-bank power-of-two depth")
    out = bundle / "rom_macro"
    out.mkdir(exist_ok=True)
    (out / f"{MACRO}.v").write_text(blackbox(width, addr_bits))
    (out / f"{MACRO}_sim.v").write_text(simulation(width, addr_bits))
    (out / f"{MACRO}.lef").write_text(lef(width, 4 * per, addr_bits))
    area, height = _size(width, 4 * per)
    macro = {
        "format": "llmchip-rom-macro/1",
        "module": MACRO,
        "width": width,
        "addrBits": addr_bits,
        "depth": 4 * per,
        "bankWords": per,
        "latency": "combinational; the consumer registers the read in S1",
        "bankFiles": [path.name for path in banks],
        "bankDigests": [_digest(bank) for bank in words],
        "blackbox": f"rom_macro/{MACRO}.v",
        "simulation": f"rom_macro/{MACRO}_sim.v",
        "lef": f"rom_macro/{MACRO}.lef",
        "areaUm2": {
            "estimate": round(area, 1),
            "heightUm": round(height, 1),
            "widthUm": round(area / height, 1),
            "densityUm2PerBit": density,
            "confidence": "calibrated density, not placed or routed",
        },
        "hardening": "not run: the LEF is an estimate until the macro is hardened",
    }
    (out / "rom_macro.json").write_text(json.dumps(macro, indent=2))
    return macro


def equivalent(bundle: Path, yosys: Path) -> dict:
    """Proves the simulation view equals view 1 of `tile_rom` at the macro's parameters."""
    manifest = json.loads((bundle / "rom_macro" / "rom_macro.json").read_text())
    width, addr_bits = manifest["width"], manifest["addrBits"]
    script = "; ".join(
        [
            f"read_verilog {(bundle / 'rtl' / 'tile_rom.v').as_posix()}",
            f"chparam -set ADDR {addr_bits} -set WIDTH {width} tile_rom",
            "hierarchy -top tile_rom",
            "proc",
            "memory",
            "opt",
            "rename tile_rom gold",
            f"read_verilog {(bundle / 'rom_macro' / f'{MACRO}_sim.v').as_posix()}",
            f"rename {MACRO} gate",
            "proc",
            "memory",
            "opt",
            "async2sync",
            "equiv_make gold gate equiv",
            "hierarchy -top equiv",
            "equiv_simple",
            "equiv_induct",
            "equiv_status -assert",
        ]
    )
    result = subprocess.run([str(yosys), "-q", "-p", script], cwd=bundle, capture_output=True, text=True)
    text = result.stdout + result.stderr
    return {"returncode": result.returncode, "equivalent": result.returncode == 0, "output": text[-2000:]}
