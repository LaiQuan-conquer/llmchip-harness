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

from . import chain, chip, macro, rom, views

TEMPLATE_VERSION = "taalas-tile-templates/4"
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

    # The bit-exact linear stage of the first bank and, for a slice, its top. The mantissas and
    # biases are the numeric spec's own arrays, so the stage rounds exactly as GraphL2.linear.
    entries = _entries(spec, slice_report, layers)
    layer0, node0, r0, r1, c0, c1 = entries[0]
    key0 = f"{layer0}.{node0}"
    rows, columns = r1 - r0, c1 - c0
    group = int(spec["weights"]["groupSize"])
    groups0 = c0 // group
    groups1 = -(-c1 // group)
    ngroups = groups1 - groups0
    chunks = -(-columns // macs)
    cpg = group // macs
    log2cpg = cpg.bit_length() - 1
    if cpg < 1 or 1 << log2cpg != cpg:
        raise ValueError(f"groupSize {group} is not a power-of-two multiple of MACS {macs}")
    mant = arrays[f"{key0}.m"].astype(np.int64)[r0:r1, groups0:groups1]
    bias = arrays.get(f"{key0}.b")
    bias = np.zeros(rows, dtype=np.int64) if bias is None else bias.astype(np.int64)[r0:r1]
    (out / "slice_m.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in mant.ravel()) + "\n")
    (out / "slice_b.hex").write_text("\n".join(f"{int(v) & 0xFFFF:04x}" for v in bias.ravel()) + "\n")
    kinds_out = {
        "q_proj": "q",
        "k_proj": "k",
        "v_proj": "v",
        "o_proj": "x",
        "gate_proj": "h1",
        "up_proj": "h3",
        "down_proj": "x",
    }
    kinds_in = {
        "q_proj": "xn",
        "k_proj": "xn",
        "v_proj": "xn",
        "o_proj": "v",
        "gate_proj": "xn",
        "up_proj": "xn",
        "down_proj": "g",
    }
    layer_frac = spec["activations"].get("layerFrac") or {}
    frac = layer_frac.get(str(layer0), spec["activations"]["frac"])
    frac_in, frac_out = int(frac[kinds_in[node0]]), int(frac[kinds_out[node0]])
    exponent = int(spec["weights"]["exponents"][key0])
    linear = {
        "matrix": key0,
        "rows": rows,
        "columns": columns,
        "quantGroups": ngroups,
        "groupSize": group,
        "macs": macs,
        "chunks": chunks,
        "chunksPerGroup": cpg,
        "fracIn": frac_in,
        "fracOut": frac_out,
        "exponent": exponent,
        "mantissaHex": "slice_m.hex",
        "biasHex": "slice_b.hex",
        "mantissaDigest": hashlib.sha256(mant.astype("<i8").tobytes()).hexdigest(),
        "biasDigest": hashlib.sha256(bias.astype("<i8").tobytes()).hexdigest(),
    }
    (rtl / "tile_linear.v").write_text((_templates_dir() / "tile_linear.v").read_text())
    if slice_report is not None:
        slice_top = (_templates_dir() / "tile_slice_top.v").read_text()
        replacements = {
            "/*MACS*/ 8": f"/*MACS*/ {macs}",
            "/*BITS*/ 4": f"/*BITS*/ {bits}",
            "/*ROWS*/ 8": f"/*ROWS*/ {rows}",
            "/*CHUNKS*/ 4": f"/*CHUNKS*/ {chunks}",
            "/*LOG2CHUNKS*/ 2": f"/*LOG2CHUNKS*/ {max(1, (chunks - 1).bit_length())}",
            "/*LOG2ROWS*/ 3": f"/*LOG2ROWS*/ {max(1, (rows - 1).bit_length())}",
            "/*LOG2ADDR*/ 5": f"/*LOG2ADDR*/ {max(2, (banks[0]['words'] - 1).bit_length())}",
            "/*LOG2MACS*/ 3": f"/*LOG2MACS*/ {max(1, (macs - 1).bit_length())}",
            "/*LOG2CPG*/ 1": f"/*LOG2CPG*/ {log2cpg}",
            "/*NGROUPS*/ 2": f"/*NGROUPS*/ {ngroups}",
            "/*FRAC_IN*/ 12": f"/*FRAC_IN*/ {frac_in}",
            "/*FRAC_OUT*/ 15": f"/*FRAC_OUT*/ {frac_out}",
            "/*EXP*/ 21": f"/*EXP*/ {exponent}",
        }
        for old, new in replacements.items():
            slice_top = slice_top.replace(old, new)
        (rtl / "tile_slice_top.v").write_text(slice_top)

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
    (rtl / "tile_engine2.v").write_text((_templates_dir() / "tile_engine2.v").read_text())
    (rtl / "tile_engine_b.v").write_text((_templates_dir() / "tile_engine_b.v").read_text())
    (rtl / "tile_attention.v").write_text((_templates_dir() / "tile_attention.v").read_text())
    (rtl / "tile_silu.v").write_text((_templates_dir() / "tile_silu.v").read_text())
    (rtl / "tile_residual.v").write_text((_templates_dir() / "tile_residual.v").read_text())
    (rtl / "tile_layer.v").write_text((_templates_dir() / "tile_layer.v").read_text())
    (rtl / "tile_host.v").write_text((_templates_dir() / "tile_host.v").read_text())
    top = (_templates_dir() / "tile_top.v").read_text()
    top = top.replace("/*MACS*/ 128", f"/*MACS*/ {macs}").replace("/*BITS*/ 4", f"/*BITS*/ {bits}")
    (rtl / "tile_top.v").write_text(top)
    # The Q-full top's layer count is the model's; a slice bundle still carries the template (the
    # layer chain is only exercised by a Q-full bundle).
    qfull_layers = max(2, int(spec["hyper"]["layers"]))
    qfull = (_templates_dir() / "tile_qfull_top.v").read_text()
    qfull = (
        qfull.replace("/*LAYERS*/ 2", f"/*LAYERS*/ {qfull_layers}")
        .replace("/*MACS*/ 8", f"/*MACS*/ {macs}")
        .replace("/*BITS*/ 4", f"/*BITS*/ {bits}")
    )
    (rtl / "tile_qfull_top.v").write_text(qfull)

    # One 4-bank file set per decoder layer: the layer's seven matrices in schedule order, row
    # major, padded to the banked ROM's power-of-two depth. These are the inputs of the Q-full
    # top's per-layer engines and of a banked hard ROM macro; `rom_bank0..3.hex` remains the
    # slice's set.
    layer_banks = []
    if slice_report is None:
        by_layer: dict[int, dict] = {}
        for layer, node, r0, r1, c0, c1 in _entries(spec, None, layers):
            if node == CLASSIFIER:
                continue
            key = f"{layer}.{node}"
            q = arrays[f"{key}.q"].astype(np.int64)[r0:r1, c0:c1]
            entry = by_layer.setdefault(layer, {"matrices": [], "words": []})
            entry["matrices"].append(key)
            entry["words"].extend(rom.to_words(q, macs, bits))
        for layer in sorted(by_layer):
            words = by_layer[layer]["words"]
            addr_bits = max(2, (len(words) - 1).bit_length())
            per = 1 << (addr_bits - 2)
            padded = words + [0] * (4 * per - len(words))
            files = []
            digests = []
            for bank in range(4):
                chunk = padded[bank * per : (bank + 1) * per]
                name = f"layer{layer}_bank{bank}.hex"
                (out / name).write_text("\n".join(rom.hexfile(chunk, macs, bits)) + "\n")
                files.append(name)
                digests.append(rom.digest(chunk, macs, bits))
            layer_banks.append(
                {
                    "layer": layer,
                    "matrices": by_layer[layer]["matrices"],
                    "words": len(words),
                    "addrBits": addr_bits,
                    "perBank": per,
                    "files": files,
                    "digests": digests,
                }
            )

    # The banked Q-full top: one explicit engine per layer, each loading that layer's four bank
    # files. A slice bundle has no layer banks, so it keeps only the generic `tile_qfull_top.v`.
    if len(layer_banks) >= 2:
        banked = (_templates_dir() / "tile_qfull_banked.v").read_text()
        instances = []
        for index, entry in enumerate(layer_banks):
            layer = int(entry["layer"])
            instances.append(
                f"""  tile_engine #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS), .ROWS(ROWS), .CHUNKS(CHUNKS),
    .LOG2CHUNKS(LOG2CHUNKS), .LOG2ROWS(LOG2ROWS), .LOG2ADDR(LOG2ADDR), .LOG2MACS(LOG2MACS),
    .INIT0("layer{layer}_bank0.hex"), .INIT1("layer{layer}_bank1.hex"),
    .INIT2("layer{layer}_bank2.hex"), .INIT3("layer{layer}_bank3.hex")
  ) engine{index} (
    .clk(clk), .rst_n(rst_n), .start(start_w[{index}]),
    .x_wr_en(x_wr_en), .x_wr_chunk(x_wr_chunk), .x_wr_lane(x_wr_lane), .x_wr_data(x_wr_data),
    .y(y_w[{index}*ACC +: ACC]), .y_valid(y_valid_w[{index}]), .done(done_w[{index}]),
    .word_sum(word_sum_w[{index}*ACC +: ACC]), .word_valid(word_valid_w[{index}]),
    .word_row(word_row_w[{index}*LOG2ROWS +: LOG2ROWS]),
    .word_chunk(word_chunk_w[{index}*LOG2CHUNKS +: LOG2CHUNKS])
  );"""
            )
        banked = (
            banked.replace("/*LAYERS*/ 2", f"/*LAYERS*/ {len(layer_banks)}")
            .replace("/*MACS*/ 8", f"/*MACS*/ {macs}")
            .replace("/*BITS*/ 4", f"/*BITS*/ {bits}")
            .replace("/*LOG2ADDR*/ 10", f"/*LOG2ADDR*/ {layer_banks[0]['addrBits']}")
            .replace("/*ENGINES*/", "\n".join(instances))
        )
        (rtl / "tile_qfull_banked.v").write_text(banked)

    chain_manifest = None
    chip_manifest = None
    if slice_report is None:
        chosen = layers if layers is not None else list(range(int(spec["hyper"]["layers"])))
        (rtl / "tile_embed.v").write_text((_templates_dir() / "tile_embed.v").read_text())
        chain_manifest = chain.generate(out, spec, arrays, macs, chosen)
        chip_manifest = chip.generate(out, spec, arrays, macs)

    via_manifest = views.write_view3(out, width=macs * bits)

    macro_manifest = macro.generate(out, width=macs * bits)

    manifest = {
        "format": "llmchip-rtl-bundle/2",
        "architecture": "taalas-tile/1",
        "mapping": {"rule": rom.MAPPING_VERSION, "macs": macs, "bits": bits},
        "templates": {
            "version": TEMPLATE_VERSION,
            "files": [
                "tile_rom.v",
                "tile_mac.v",
                "tile_top.v",
                "tile_attention.v",
                "tile_qfull_top.v",
                "tile_linear.v",
                "tile_slice_top.v",
                "tile_silu.v",
                "tile_residual.v",
                "tile_layer.v",
                "tile_host.v",
                "tile_chain.v",
                "tile_chip.v",
                "tile_embed.v",
            ],
        },
        "layerBanks": layer_banks,
        "chain": chain_manifest,
        "chip": chip_manifest,
        "linear": linear,
        "macro": macro_manifest,
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
            "3": "via-programmed array (rom_view3.v, rom_via.json)",
        },
        "via": via_manifest,
        "files": {str(p.relative_to(out)): _hash(p) for p in sorted(rtl.iterdir()) if p.suffix == ".v"},
    }
    if chain_manifest is None:
        for name in ("tile_chain.v", "tile_chip.v", "tile_embed.v"):
            manifest["templates"]["files"].remove(name)
    (out / "design.json").write_text(json.dumps(manifest, indent=2))
    return manifest
