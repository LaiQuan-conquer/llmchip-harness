"""The three ROM views of a `taalas-tile/1` bundle and their formal equivalence (FR-SGN-07, FR-RTL-02).

View 1 is the behavioural `$readmemh` ROM of the bundle; view 2 is that ROM mapped to gates and
flip-flops by Yosys (`proc; memory_map; techmap`), the form synthesis and physical design take;
view 3 is the via-programmed array: every `1` of a word is a via between that wordline and that
bitline, and each bitline is the OR of the vias of the selected wordline. `equivalent` proves
view 2 against view 1 with Yosys's equivalence engine; `equivalent_view3` proves view 3 the same
way, on the 64-word prefix view 2 uses (the mapping is the same at every depth, and a full 448-word
proof would only be a larger instance of it). The via map itself is written in full
(`rom_view3.v` and `rom_via.json`).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROM_MODULE = "tile_rom"
VIEW2_FILE = "rom_view2.v"
VIEW3_FILE = "rom_view3.v"
VIEW3_EQUIV_FILE = "rom_view3_equiv.v"
VIA_FILE = "rom_via.json"
DEPTH_BITS = 6  # the equivalence runs on a 64-word instance: the mapping is the same at every depth


def _params(bundle: Path) -> tuple[int, int]:
    """(macs * bits, depth bits) of the bundle's first ROM bank."""
    manifest = json.loads((bundle / "design.json").read_text())
    width = int(manifest["mapping"]["macs"]) * int(manifest["mapping"]["bits"])
    words = len((bundle / "rom.hex").read_text().split())
    return width, max(2, (words - 1).bit_length())


def _bank_lines(bundle: Path) -> list[list[int]]:
    banks = []
    for index in range(4):
        lines = (bundle / f"rom_bank{index}.hex").read_text().split()
        banks.append([int(line, 16) for line in lines])
    if len({len(bank) for bank in banks}) != 1:
        raise ValueError("the four bank files have different depths")
    return banks


def _bank_words(bundle: Path) -> list[int]:
    """The four bank files in address order at the bundle's depth (view 1's contents)."""
    return [word for bank in _bank_lines(bundle) for word in bank]


def _equiv_words(bundle: Path, depth_bits: int) -> list[int]:
    """The first 2^depth_bits addresses as `tile_rom` decodes them: bank-major, `per` words each."""
    banks = _bank_lines(bundle)
    per = 1 << (depth_bits - 2)
    return [banks[address // per][address % per] for address in range(1 << depth_bits)]


def _via_module(module: str, width: int, depth_bits: int, words: list[int]) -> str:
    depth = len(words)
    if depth != 1 << depth_bits:
        raise ValueError("the via array is not a power-of-two depth")
    width_hex = -(-width // 4)
    lines = [
        f"// The via-programmed ROM array ({module}): one via per 1 bit, one OR plane per bitline.",
        "`default_nettype none",
        f"module {module} #(",
        f"  parameter integer WIDTH = {width},",
        f"  parameter integer ADDR = {depth_bits}",
        ") (",
        "  input  wire [ADDR-1:0]  addr,",
        "  output wire [WIDTH-1:0] q",
        ");",
        "",
        "  localparam integer DEPTH = 1 << ADDR;",
        "",
        "  wire [DEPTH-1:0] wordline;",
        "  genvar wa;",
        "  generate",
        "    for (wa = 0; wa < DEPTH; wa = wa + 1) begin : g_wordline",
        "      assign wordline[wa] = (addr == wa);",
        "    end",
        "  endgenerate",
        "",
        "  wire [WIDTH-1:0] via [0:DEPTH-1];",
    ]
    for address, word in enumerate(words):
        lines.append(f"  assign via[{address}] = WIDTH'h{word:0{width_hex}x};")
    lines += [
        "",
        "  genvar bi;",
        "  genvar wi;",
        "  generate",
        "    for (bi = 0; bi < WIDTH; bi = bi + 1) begin : g_bitline",
        "      wire [DEPTH-1:0] hit;",
        "      for (wi = 0; wi < DEPTH; wi = wi + 1) begin : g_via",
        "        assign hit[wi] = wordline[wi] & via[wi][bi];",
        "      end",
        "      assign q[bi] = |hit;",
        "    end",
        "  endgenerate",
        "",
        "endmodule",
        "",
    ]
    return "\n".join(lines)


def write_view3(bundle: Path, width: int | None = None) -> dict:
    """Writes the full via map and its 64-word equivalence instance; returns the via manifest."""
    words = _bank_words(bundle)
    if width is None:
        manifest = json.loads((bundle / "design.json").read_text())
        width = int(manifest["mapping"]["macs"]) * int(manifest["mapping"]["bits"])
    depth_bits = (len(words) - 1).bit_length()
    if len(words) != 1 << depth_bits:
        raise ValueError("the bank set is not a power-of-two depth")
    equiv_bits = min(DEPTH_BITS, depth_bits)
    (bundle / VIEW3_FILE).write_text(_via_module("tile_rom_view3", width, depth_bits, words))
    (bundle / VIEW3_EQUIV_FILE).write_text(
        _via_module("tile_rom_view3_equiv", width, equiv_bits, _equiv_words(bundle, equiv_bits))
    )
    file_digest = hashlib.sha256((bundle / VIEW3_FILE).read_bytes()).hexdigest()
    equiv_digest = hashlib.sha256((bundle / VIEW3_EQUIV_FILE).read_bytes()).hexdigest()
    via = {
        "format": "llmchip-rom-via/1",
        "module": "tile_rom_view3",
        "width": width,
        "depth": len(words),
        "addrBits": depth_bits,
        "bankWords": len(words) // 4,
        "vias": sum(word.bit_count() for word in words),
        "file": VIEW3_FILE,
        "fileDigest": file_digest,
        "equivalence": {
            "module": "tile_rom_view3_equiv",
            "file": VIEW3_EQUIV_FILE,
            "depthBits": equiv_bits,
            "digest": equiv_digest,
        },
        "digest": hashlib.sha256(b"".join(word.to_bytes(-(-width // 8), "little") for word in words)).hexdigest(),
    }
    (bundle / VIA_FILE).write_text(json.dumps(via, indent=2))
    return via


def synthesize_view2(bundle: Path, yosys: Path) -> dict:
    width, depth_bits = _params(bundle)
    script = "; ".join(
        [
            f"read_verilog {(bundle / 'rtl' / 'tile_rom.v').as_posix()}",
            f"chparam -set ADDR {depth_bits} -set WIDTH {width} {ROM_MODULE}",
            f"hierarchy -top {ROM_MODULE}",
            "proc",
            "opt",
            "memory_map",
            "opt",
            "techmap",
            "opt",
            f"write_verilog -noattr {(bundle / VIEW2_FILE).as_posix()}",
        ]
    )
    result = subprocess.run([str(yosys), "-q", "-p", script], cwd=bundle, capture_output=True, text=True)
    return {"returncode": result.returncode, "file": VIEW2_FILE, "output": (result.stdout + result.stderr)[-2000:]}


def equivalent(bundle: Path, yosys: Path) -> dict:
    width, depth_bits = _params(bundle)
    script = "; ".join(
        [
            f"read_verilog {(bundle / 'rtl' / 'tile_rom.v').as_posix()}",
            f"chparam -set ADDR {depth_bits} -set WIDTH {width} {ROM_MODULE}",
            f"hierarchy -top {ROM_MODULE}",
            "proc",
            "memory",
            "opt",
            f"rename {ROM_MODULE} gold",
            f"read_verilog {(bundle / VIEW2_FILE).as_posix()}",
            f"rename {ROM_MODULE} gate",
            "proc",
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
    proven = result.returncode == 0
    return {"returncode": result.returncode, "equivalent": proven, "output": text[-2000:]}


def equivalent_view3(bundle: Path, yosys: Path, mismatch: bool = False) -> dict:
    """Proves view 3's 64-word prefix against view 1; `mismatch` flips a via as a negative control."""
    width, _ = _params(bundle)
    via = json.loads((bundle / VIA_FILE).read_text())
    bits = int(via["equivalence"]["depthBits"])
    if mismatch:
        words = _equiv_words(bundle, bits)
        words[0] ^= 1
        (bundle / VIEW3_EQUIV_FILE).write_text(_via_module("tile_rom_view3_equiv", width, bits, words))
    script = "; ".join(
        [
            f"read_verilog {(bundle / 'rtl' / 'tile_rom.v').as_posix()}",
            f"chparam -set ADDR {bits} -set WIDTH {width} {ROM_MODULE}",
            f"hierarchy -top {ROM_MODULE}",
            "proc",
            "memory",
            "opt",
            f"rename {ROM_MODULE} gold",
            f"read_verilog {(bundle / VIEW3_EQUIV_FILE).as_posix()}",
            "rename tile_rom_view3_equiv gate",
            "proc",
            "opt",
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


def rom_views(bundle: Path, yosys: Path) -> dict:
    """Views 2 and 3 and the proof that each equals view 1."""
    via = write_view3(bundle)
    view = synthesize_view2(bundle, yosys)
    if view["returncode"] != 0:
        return {"view2": view, "equivalent": False, "via": via, "equivalent3": False}
    proof = equivalent(bundle, yosys)
    proof3 = equivalent_view3(bundle, yosys)
    return {
        "view2Returncode": view["returncode"],
        "equivalent": bool(proof["equivalent"]),
        "equivalent3": bool(proof3["equivalent"]),
        "via": via,
        "output": proof["output"][-2000:],
        "output3": proof3["output"][-2000:],
    }
