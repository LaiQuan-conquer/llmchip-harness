"""Writes a spatial design (taalas-spatial/1) as a self-contained RTL bundle (format 3).

The bundle holds the generated Verilog (the matrices as circuits, the core, the tables),
the library of bit-serial cells, the host interface and chip top, the testbench, the
observation map, the model of the design (`sp_model.py` with its image) and design.json.
"""

from __future__ import annotations

import io
import json
from importlib import resources

import numpy as np

from .generator import ARCHITECTURE, EXP_RANGE, ROUND, SCORE_FRAC, SIG_RANGE, SIG_STEPS, Spatial, check_round_trip

FORMAT = "llmchip-rtl-bundle/3"
LIBRARY = ("sp_lib.v", "sp_host.v", "sp_chip.v")
TESTBENCH = ("tb_top.v", "tb.cpp")
POINTS = {"attn_residual": 1, "ffn_residual": 2, "logits": 3}


def params(sp: Spatial) -> dict:
    """Parameters of the design: what the testbench, the model and the checks need."""
    f = sp.f
    return {
        "ARCHITECTURE": ARCHITECTURE,
        "DIM": sp.dim,
        "HIDDEN": sp.hidden,
        "LAYERS": sp.layers,
        "HEADS": sp.heads,
        "HS": sp.hs,
        "KV_DIM": sp.kv_dim,
        "VOCAB": sp.vocab,
        "CTX": sp.ctx,
        "GROUP": sp.group,
        "ROUND_MODE": ROUND[sp.rounding],
        "OVERFLOW_WRAP": int(sp.overflow == "wrap"),
        "EPS_Q": sp.eps_q,
        "FN": sp.fn,
        "F_XN": f["xn"],
        "F_H1": f["h1"],
        "ATT_SCALE": sp.att_scale,
        "SSH": f["q"] + f["k"] + 15 - SCORE_FRAC,
        "EXP_CAP": EXP_RANGE * 2**SCORE_FRAC - 1,
        "EXP_SHIFT": SCORE_FRAC - 5,
        "SIG_LIMIT": SIG_RANGE * 2 ** f["h1"],
        "SIG_OFFSET": SIG_RANGE * SIG_STEPS,
        "SILU_SHIFT": f["h1"] + f["h3"] - f["g"],
        # name, rounding shift, result width, stream length (bits each row's stream runs)
        "MATRICES": [[m.name, m.shift, m.width, m.length] for m in sp.matrices.values()],
        "SQ_BITS": sp.sq_bits,
        "DIVIDERS": sp.dividers,
        "CYCLES_PER_TOKEN": sp.cycles_per_token(),
        "OBS_WORDS": 2 * sp.layers * sp.dim + sp.vocab + 1,
    }


def trace_map(sp: Spatial) -> list[dict]:
    """The observation points as words of the host READ command (point, layer, count, address)."""
    out = []
    for layer in range(sp.layers):
        out.append({"point": POINTS["attn_residual"], "layer": layer, "n": sp.dim, "addr": 2 * layer * sp.dim})
        out.append({"point": POINTS["ffn_residual"], "layer": layer, "n": sp.dim, "addr": (2 * layer + 1) * sp.dim})
    out.append({"point": POINTS["logits"], "layer": 0, "n": sp.vocab, "addr": 2 * sp.layers * sp.dim})
    return [{"op": i, **t, "debug": 0} for i, t in enumerate(out)]


def image(sp: Spatial) -> dict[str, np.ndarray]:
    """The arrays the model of the design reads: weights per matrix, tables, norms, embedding."""
    a = sp.arrays
    out = {f"{m.name}.q": m.q for m in sp.matrices.values()} | {f"{m.name}.m": m.m for m in sp.matrices.values()}
    for k in ("lut.rsqrt", "lut.exp", "lut.sigmoid", "lut.rope_cos", "lut.rope_sin", "rms_final"):
        out[k] = a[k]
    for layer in range(sp.layers):
        out[f"{layer}.rms_att"] = a[f"{layer}.rms_att"]
        out[f"{layer}.rms_ffn"] = a[f"{layer}.rms_ffn"]
    out["emb.table"] = np.asarray(sp.emb_table)
    return out


def bundle_files(sp: Spatial, source: dict | None = None) -> dict[str, bytes]:
    generated = sp.files()
    check_round_trip(sp, generated)
    files = {f"rtl/{name}": text.encode() for name, text in generated.items()}
    pkg = resources.files("llmchip_rtl.spatial") / "rtl"
    for name in LIBRARY + TESTBENCH:
        files[f"rtl/{name}"] = (pkg / name).read_bytes()
    files["sp_model.py"] = (resources.files("llmchip_rtl.spatial") / "sp_model.py").read_bytes()
    buf = io.BytesIO()
    np.savez(buf, **image(sp))
    files["sp-image.npz"] = buf.getvalue()
    traces = trace_map(sp)
    files["rtl/trace.map"] = "".join(
        f"{t['op']} {t['point']} {t['layer']} {t['n']} {t['addr']} {t['debug']}\n" for t in traces
    ).encode()
    p = params(sp)
    cfg = sp.cfg
    design = {
        "format": FORMAT,
        "architecture": ARCHITECTURE,
        "top": "llmchip_chip",
        "source": source or {},
        "params": p,
        "model": {k: cfg[k] for k in ("dim", "hidden_dim", "n_layers", "n_heads", "n_kv_heads", "vocab_size")},
        "reference": {"module": "sp_model.py", "image": "sp-image.npz"},
        "schedule": [{"stage": name, "cycles": n} for name, n in sp.schedule()],
        "circuits": {
            m.name: {"rows": int(m.q.shape[0]), "adders": m.adders, "delayBits": m.delays, "streamBits": m.length}
            for m in sp.matrices.values()
        },
        "traceMap": traces,
        "sources": [f"rtl/{n}" for n in ("sp_lib.v", *sorted(generated), "sp_host.v", "sp_chip.v")],
        "includes": [],
        "testbench": {"top": "tb_top", "sources": ["rtl/tb_top.v"], "cpp": "rtl/tb.cpp"},
        "interfaces": {
            "host": "SPI slave mode 0, SCK <= clk/8: 0x10 START token[15:0] pos[15:0] mode[7:0]; "
            "0x20 STATUS -> busy error done 13'b0 token[15:0]; 0x30 READ addr[15:0] -> 32-bit observation "
            "words from addr on",
            "memory": "none: the weights are circuits, the KV cache is on chip",
        },
    }
    files["design.json"] = json.dumps(design, indent=2).encode()
    return files
