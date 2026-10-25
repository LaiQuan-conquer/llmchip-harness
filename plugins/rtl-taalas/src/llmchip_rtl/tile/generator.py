"""The `taalas-tile/1` RTL bundle for a slice or the full model (requirements FR-RTL-01/02/03).

The generator writes files, never patches them (DP-3): the hand-written templates under
`templates/` (a synchronous ROM with a `$readmemh` initialisation, a signed parallel MAC array,
and the top level with the host frames, the schedule marcher and the observation readback) plus
the generated ROM contents of view 1 (the behavioural model of FR-SGN-07) and a manifest with
the mapping rule's version and every file's hash. View 2 (the standard-cell ROM netlist) is the
synthesis of view 1 (`views.py`); view 3 (via-programmed) is not built.

A slice writes its matrices of one layer; `layers=None` writes the banks of every decoder layer
plus the tied classifier (Q-full). The assembled top level of Q-full (one engine per layer,
pipelined) is not generated yet: the template is the single-engine top of a slice.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from . import rom

TEMPLATE_VERSION = "taalas-tile-templates/1"
CLASSIFIER = "embed"  # the tied classifier: the embedding table's integers


def _shape(h: dict, node: str) -> tuple[int, int]:
    hd, hidden, inter = h["headDim"], h["hidden"], h["intermediate"]
    kv = h["kvHeads"] * hd
    return {
        "q_proj": (hidden, hidden),
        "k_proj": (kv, hidden),
        "v_proj": (kv, hidden),
        "o_proj": (hidden, h["heads"] * hd),
        "gate_proj": (inter, hidden),
        "up_proj": (inter, hidden),
        "down_proj": (hidden, inter),
        CLASSIFIER: (h["vocab"], hidden),
    }[node]


def _templates_dir() -> Path:
    return Path(__file__).with_name("templates")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entries(
    spec: dict, slice_report: dict | None, layers: list[int] | None
) -> list[tuple[int, str, int, int, int, int]]:
    """(layer, node, r0, r1, c0, c1) of the banks: the slice's pieces, or every matrix of a layer."""
    h = spec["hyper"]
    if slice_report is not None:
        layer = int(slice_report.get("layer", 0))
        return [
            (layer, entry["node"], entry["rows"][0], entry["rows"][1], entry["columns"][0], entry["columns"][1])
            for entry in slice_report["matrices"]
        ]
    chosen = layers if layers is not None else list(range(h["layers"]))
    out = []
    names = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")
    for layer in chosen:
        for node in names:
            rows, columns = _shape(h, node)
            out.append((layer, node, 0, rows, 0, columns))
    rows, columns = _shape(h, CLASSIFIER)
    out.append((int(chosen[-1]), CLASSIFIER, 0, rows, 0, columns))
    return out


def generate(
    spec: dict,
    arrays: dict[str, np.ndarray],
    slice_report: dict | None = None,
    macs: int = 128,
    layers: list[int] | None = None,
    out: Path | None = None,
) -> dict:
    """Writes the RTL and ROM files of the slice (or of the whole layer set) and returns the manifest."""
    bits = spec["weights"]["storageBits"]
    out = out or Path(".")
    rtl = out / "rtl"
    data = out / "rom"
    rtl.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)

    banks = []
    for layer, node, r0, r1, c0, c1 in _entries(spec, slice_report, layers):
        key = CLASSIFIER if node == CLASSIFIER else f"{layer}.{node}"
        q = arrays[f"{key}.q"].astype(np.int64)[r0:r1, c0:c1]
        rows, columns = q.shape
        words = rom.to_words(q, macs, bits)
        name = f"{key}.hex"
        (data / name).write_text("\n".join(rom.hexfile(words, macs, bits)) + "\n")
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

    luts = {}
    for name in ("rsqrt", "exp", "sigmoid", "rope_cos", "rope_sin"):
        array = arrays.get(f"lut.{name}")
        if array is None:
            continue
        digits = 4 if np.asarray(array).dtype.itemsize <= 2 else 8
        mask = (1 << (4 * digits)) - 1
        values = [int(value) & mask for value in np.asarray(array).ravel()]
        (data / f"lut.{name}.hex").write_text("\n".join(f"{value:0{digits}x}" for value in values) + "\n")
        luts[name] = {
            "entries": len(values),
            "bits": 4 * digits,
            "hex": f"rom/lut.{name}.hex",
            "digest": hashlib.sha256(np.asarray(array).tobytes()).hexdigest(),
        }

    if luts:
        first_lut = next(iter(luts))
        (out / "lut.hex").write_text((data / f"lut.{first_lut}.hex").read_text())
        for name, entry in luts.items():
            (out / f"{name}.hex").write_text((data / f"lut.{name}.hex").read_text())
            if entry["bits"] == 16:
                (out / f"{name}_16.hex").write_text((data / f"lut.{name}.hex").read_text())

    # Tools that read the ROM module with its default parameter (Yosys does) take this copy;
    # simulators use each instance's INIT from the generated top.
    (out / "rom.hex").write_text((data / f"{banks[0]['matrix']}.hex").read_text())
    # The banked ROM reads one file per bank: 4 banks of 2^(ADDR-2) words, zero padded.
    first_lines = (data / f"{banks[0]['matrix']}.hex").read_text().split()
    addr_bits = max(2, (len(first_lines) - 1).bit_length())
    per = 1 << (addr_bits - 2)
    width_chars = len(first_lines[0]) if first_lines else 128
    for bank in range(4):
        chunk = first_lines[bank * per : (bank + 1) * per]
        chunk += ["0" * width_chars] * (per - len(chunk))
        (out / f"rom_bank{bank}.hex").write_text("\n".join(chunk) + "\n")
    (rtl / "tile_rom.v").write_text((_templates_dir() / "tile_rom.v").read_text())
    (rtl / "tile_mac.v").write_text((_templates_dir() / "tile_mac.v").read_text())
    (rtl / "tile_lut.v").write_text((_templates_dir() / "tile_lut.v").read_text())
    (rtl / "tile_rmsnorm.v").write_text((_templates_dir() / "tile_rmsnorm.v").read_text())
    (rtl / "tile_rope.v").write_text((_templates_dir() / "tile_rope.v").read_text())
    (rtl / "tile_kv.v").write_text((_templates_dir() / "tile_kv.v").read_text())
    (rtl / "tile_engine.v").write_text((_templates_dir() / "tile_engine.v").read_text())
    top = (_templates_dir() / "tile_top.v").read_text()
    top = top.replace("/*MACS*/ 128", f"/*MACS*/ {macs}").replace("/*BITS*/ 4", f"/*BITS*/ {bits}")
    (rtl / "tile_top.v").write_text(top)

    manifest = {
        "format": "llmchip-rtl-bundle/2",
        "architecture": "taalas-tile/1",
        "mapping": {"rule": rom.MAPPING_VERSION, "macs": macs, "bits": bits},
        "templates": {"version": TEMPLATE_VERSION, "files": ["tile_rom.v", "tile_mac.v", "tile_top.v"]},
        "slice": (
            {
                "level": slice_report["level"],
                "layer": int(slice_report.get("layer", 0)),
                "weights": slice_report["total"]["weights"],
            }
            if slice_report is not None
            else {
                "level": "Q-full",
                "layers": (len(banks) - 1) // 7,
                "weights": int(sum(b["rows"] * b["columns"] for b in banks)),
            }
        ),
        "banks": banks,
        "luts": luts,
        "views": {
            "1": "behavioural $readmemh (this bundle)",
            "2": "standard-cell ROM netlist, synthesised by views.py",
            "3": "via-programmed, not built",
        },
        "files": {str(p.relative_to(out)): _hash(p) for p in sorted(rtl.iterdir()) if p.suffix == ".v"},
    }
    (out / "design.json").write_text(json.dumps(manifest, indent=2))
    return manifest
