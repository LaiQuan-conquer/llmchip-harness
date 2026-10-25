"""The two ROM views of a `taalas-tile/1` bundle and their formal equivalence (FR-SGN-07, FR-RTL-02).

View 1 is the behavioural `$readmemh` ROM of the bundle; view 2 is that ROM mapped to gates and
flip-flops by Yosys (`proc; memory_map; techmap`), the form synthesis and physical design take.
`equivalent` proves the two views equal with Yosys's own equivalence engine (`equiv_make`,
`equiv_simple`, `equiv_induct`, `equiv_status -assert`), after `async2sync` so the asynchronous
reset is modelled. View 3 (via-programmed ROM) is not built.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROM_MODULE = "tile_rom"
VIEW2_FILE = "rom_view2.v"
DEPTH_BITS = 6  # the equivalence runs on a 64-word instance: the mapping is the same at every depth


def _params(bundle: Path) -> tuple[int, int]:
    """(macs * bits, depth bits) of the bundle's first ROM bank."""
    import json

    manifest = json.loads((bundle / "design.json").read_text())
    width = int(manifest["mapping"]["macs"]) * int(manifest["mapping"]["bits"])
    words = len((bundle / "rom.hex").read_text().split())
    return width, max(2, (words - 1).bit_length())


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


def rom_views(bundle: Path, yosys: Path) -> dict:
    """View 2 of the ROM and the proof that it equals view 1."""
    view = synthesize_view2(bundle, yosys)
    if view["returncode"] != 0:
        return {"view2": view, "equivalent": False}
    proof = equivalent(bundle, yosys)
    return {"view2Returncode": view["returncode"], "equivalent": bool(proof["equivalent"]), "output": proof["output"]}
