"""The `taalas-tile/1` RTL bundle for a slice or the full model (requirements FR-RTL-01/02/03).

The generator writes files, never patches them (DP-3): the hand-written templates under
`templates/` (a synchronous ROM with a `$readmemh` initialisation, a signed parallel MAC array,
and the top level with the host frames, the schedule marcher and the observation readback) plus
the generated ROM contents of view 1 (the behavioural model of FR-SGN-07) and a manifest with
the mapping rule's version and every file's hash. View 2 (the standard-cell ROM netlist) is the
synthesis of view 1 (R5); view 3 (via-programmed) is not built.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from . import rom

TEMPLATE_VERSION = "taalas-tile-templates/1"


def _templates_dir() -> Path:
    return Path(__file__).with_name("templates")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(
    spec: dict,
    arrays: dict[str, np.ndarray],
    slice_report: dict,
    macs: int = 128,
    out: Path | None = None,
) -> dict:
    """Writes the RTL and ROM files of a slice (`arch-taalas/slice`) and returns the manifest."""
    bits = spec["weights"]["storageBits"]
    layer = int(slice_report.get("layer", 0))
    out = out or Path(".")
    rtl = out / "rtl"
    data = out / "rom"
    rtl.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)

    banks = []
    for entry in slice_report["matrices"]:
        node = entry["node"]
        key = f"{layer}.{node}"
        q = arrays[f"{key}.q"].astype(np.int64)
        r0, r1 = entry["rows"]
        c0, c1 = entry["columns"]
        q = q[r0:r1, c0:c1]
        rows, columns = q.shape
        words = rom.to_words(q, macs, bits)
        name = f"{key}.hex"
        lines = rom.hexfile(words, macs, bits)
        (data / name).write_text("\n".join(lines) + "\n")
        banks.append(
            {
                "matrix": key,
                "rows": rows,
                "columns": columns,
                "words": len(words),
                "chunks": -(-rows // macs),
                "hex": f"rom/{name}",
                "digest": rom.digest(words, macs, bits),
                "restored": bool(rom.roundtrip(q, macs, bits)),
            }
        )

    (rtl / "tile_rom.v").write_text((_templates_dir() / "tile_rom.v").read_text())
    (rtl / "tile_mac.v").write_text((_templates_dir() / "tile_mac.v").read_text())
    top = (_templates_dir() / "tile_top.v").read_text()
    top = top.replace("/*MACS*/ 128", f"/*MACS*/ {macs}").replace("/*BITS*/ 4", f"/*BITS*/ {bits}")
    top = top.replace('parameter         INIT = "rom.hex"', f'parameter         INIT = "{banks[0]["hex"]}"')
    (rtl / "tile_top.v").write_text(top)

    manifest = {
        "format": "llmchip-rtl-bundle/2",
        "architecture": "taalas-tile/1",
        "mapping": {"rule": rom.MAPPING_VERSION, "macs": macs, "bits": bits},
        "templates": {"version": TEMPLATE_VERSION, "files": ["tile_rom.v", "tile_mac.v", "tile_top.v"]},
        "slice": {"level": slice_report["level"], "layer": layer, "weights": slice_report["total"]["weights"]},
        "banks": banks,
        "views": {
            "1": "behavioural $readmemh (this bundle)",
            "2": "standard-cell ROM netlist, from synthesis (R5)",
            "3": "via-programmed, not built",
        },
        "files": {str(p.relative_to(out)): _hash(p) for p in sorted(rtl.iterdir()) if p.suffix == ".v"},
    }
    (out / "design.json").write_text(json.dumps(manifest, indent=2))
    return manifest
