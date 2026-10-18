"""Numeric specification and quantization (requirements §3.4.2, FR-QNT-01..03).

The numeric spec fixes every numeric behavior of the chip: weight and
activation formats, scaling, rounding, saturation, accumulator width,
nonlinear-function tables, decoding rule and observation points. The L2
model (`l2.py`) executes exactly this spec with integer arithmetic, so the
RTL (L3) and the silicon (L7) can be compared with it bit for bit.

All activations are signed 16-bit fixed point with a per-tensor-kind number of
fraction bits chosen by calibration on the FP32 model. Weights are quantized
per group along the input dimension; group scales are 16-bit mantissas with
one exponent per matrix, so a dot product is `sum_g m_g * sum_i x_i * q_i`
followed by one rounding shift.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import asdict

import numpy as np
from llmchip_model import Config, Model, Tokenizer

SPEC_FORMAT = "llmchip-numeric-spec/1"
WEIGHTS_FORMAT = "llmchip-numeric-weights/1"

WEIGHT_FORMATS = {
    "int8": {"bits": 8, "qmax": 127, "method": "rtn-absmax"},
    "int4": {"bits": 4, "qmax": 7, "method": "rtn-absmax"},
    "int3": {"bits": 3, "qmax": 3, "method": "rtn-absmax"},
    "ternary": {"bits": 2, "qmax": 1, "method": "absmean"},
}
MATRICES = ("wq", "wk", "wv", "wo", "w1", "w2", "w3")
ACT_KINDS = ("x", "xn", "q", "k", "v", "h1", "h3", "g")  # attention outputs are averages of v and use its format
SCORE_FRAC = 10  # attention scores before softmax, int32
EXP_STEPS = 32  # exp table entries per unit
EXP_RANGE = 16  # exp(-16) and smaller are treated as exp(-16)
SIG_STEPS = 16  # sigmoid table entries per unit
SIG_RANGE = 8  # sigmoid input clamped to [-8, 8)
RSQRT_INDEX_BITS = 6  # rsqrt table: mantissa in [1, 4) sampled with 2^6 steps per unit

# Tables (contents are part of the spec; their hashes are recorded in it).


def rsqrt_lut() -> np.ndarray:
    t = np.arange(64, 256)
    return np.round(32768.0 / np.sqrt((t + 0.5) / 64.0)).astype(np.int64)


def exp_lut() -> np.ndarray:
    i = np.arange(EXP_STEPS * EXP_RANGE)
    return np.round(32768.0 * np.exp(-(i + 0.5) / EXP_STEPS)).astype(np.int64)


def sigmoid_lut() -> np.ndarray:
    i = np.arange(2 * SIG_RANGE * SIG_STEPS)
    z = (i - SIG_RANGE * SIG_STEPS + 0.5) / SIG_STEPS
    return np.round(32768.0 / (1.0 + np.exp(-z))).astype(np.int64)


def rope_luts(cfg: Config, context: int) -> tuple[np.ndarray, np.ndarray]:
    j = np.arange(cfg.head_size // 2)
    freq = 1.0 / np.power(cfg.rope_theta, 2 * j / cfg.head_size)
    angle = np.arange(context)[:, None] * freq[None, :]
    return np.round(np.cos(angle) * 16384).astype(np.int64), np.round(np.sin(angle) * 16384).astype(np.int64)


def frac_bits(maxabs: float, total: int = 16, margin: float = 1.5) -> int:
    """Fraction bits so that maxabs * margin fits a signed `total`-bit integer."""
    if maxabs <= 0:
        return total - 1
    return int(min(max(total - 1 - math.ceil(math.log2(maxabs * margin)), 0), 24))


def array_hash(a: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(a).astype("<i8").tobytes() + str(a.shape).encode()).hexdigest()


def quantize_matrix(w: np.ndarray, fmt: str, group: int) -> tuple[np.ndarray, np.ndarray, int]:
    """Returns (q int, group mantissas m int, matrix exponent e): w[o, i] ~ q[o, i] * m[o, i // group] * 2^-e."""
    spec = WEIGHT_FORMATS[fmt]
    out, inp = w.shape
    ngroups = -(-inp // group)
    padded = np.zeros((out, ngroups * group), np.float64)
    padded[:, :inp] = w
    g = padded.reshape(out, ngroups, group)
    if spec["method"] == "absmean":
        counts = np.full(ngroups, group, dtype=np.float64)
        counts[-1] = inp - (ngroups - 1) * group
        scale = np.abs(g).sum(axis=2) / counts[None, :]
    else:
        scale = np.abs(g).max(axis=2) / spec["qmax"]
    scale = np.where(scale > 0, scale, 1e-12)
    e = math.floor(math.log2(32767.0 / scale.max()))
    m = np.clip(np.round(scale * 2.0**e), 1, 32767).astype(np.int64)
    effective = m * 2.0**-e
    q = np.clip(np.round(g / effective[:, :, None]), -spec["qmax"], spec["qmax"]).astype(np.int64)
    return q.reshape(out, ngroups * group)[:, :inp], m, e


def calibrate(model: Model, tokenizer: Tokenizer, prompts: list[str], tokens: int) -> dict[str, float]:
    stats: dict[str, float] = {}
    for prompt in prompts:
        model.reset()
        ids = model.generate(tokenizer.encode(prompt), tokens)
        model.reset()
        for pos, tok in enumerate(ids[: model.cfg.seq_len]):
            model.forward(tok, pos, stats=stats)
    return stats


def build(
    cfg: Config,
    weights: dict[str, np.ndarray],
    tokenizer: Tokenizer,
    calibration_prompts: list[str],
    *,
    weight_format: str = "int4",
    group_size: int = 32,
    context: int = 64,
    calibration_tokens: int = 64,
    ir_hash: str = "",
    tokenizer_hash: str = "",
) -> tuple[dict, bytes]:
    """Quantizes the model and writes the numeric spec. Returns (spec, weights npz bytes)."""
    if weight_format not in WEIGHT_FORMATS:
        raise ValueError(f"unknown weight format {weight_format}")
    context = min(context, cfg.seq_len)
    stats = calibrate(Model(cfg, weights), tokenizer, calibration_prompts, min(calibration_tokens, context))
    frac = {kind: frac_bits(stats.get(kind, 1.0)) for kind in ACT_KINDS}
    logits_frac = frac_bits(stats.get("logits", 1.0), total=32, margin=4.0)

    arrays: dict[str, np.ndarray] = {}
    exponents: dict[str, int] = {}
    q, m, e = quantize_matrix(weights["token_embedding"], weight_format, group_size)
    arrays |= {"emb.q": q, "emb.m": m}
    exponents["emb"] = e
    if not cfg.shared_classifier:
        q, m, e = quantize_matrix(weights["wcls"], weight_format, group_size)
        arrays |= {"cls.q": q, "cls.m": m}
        exponents["cls"] = e
    for layer in range(cfg.n_layers):
        for name in MATRICES:
            q, m, e = quantize_matrix(weights[name][layer], weight_format, group_size)
            arrays |= {f"{layer}.{name}.q": q, f"{layer}.{name}.m": m}
            exponents[f"{layer}.{name}"] = e
    norm_frac = frac_bits(max(float(np.abs(weights[k]).max()) for k in ("rms_att", "rms_ffn", "rms_final")), margin=1.0)
    for layer in range(cfg.n_layers):
        for name in ("rms_att", "rms_ffn"):
            arrays[f"{layer}.{name}"] = np.clip(np.round(weights[name][layer] * 2.0**norm_frac), -32768, 32767).astype(
                np.int64
            )
    arrays["rms_final"] = np.clip(np.round(weights["rms_final"] * 2.0**norm_frac), -32768, 32767).astype(np.int64)
    cos, sin = rope_luts(cfg, context)
    arrays |= {
        "lut.rsqrt": rsqrt_lut(),
        "lut.exp": exp_lut(),
        "lut.sigmoid": sigmoid_lut(),
        "lut.rope_cos": cos,
        "lut.rope_sin": sin,
    }

    fmt = WEIGHT_FORMATS[weight_format]
    # Accumulator bound: per group |sum x*q| <= 2^15 * qmax * group, then times a 15-bit mantissa, summed over groups.
    widest = max(cfg.dim, cfg.hidden_dim)
    groups = -(-widest // group_size)
    bound = groups * (2**15 * fmt["qmax"] * group_size) * 32767
    acc_bits = math.ceil(math.log2(bound + 1)) + 1

    spec = {
        "format": SPEC_FORMAT,
        "version": "1",
        "model": {"family": "llama", "config": asdict(cfg), "irHash": ir_hash},
        "tokenizer": {"hash": tokenizer_hash, "bos": 1, "eos": 2, "vocabSize": cfg.vocab_size},
        "context": {"maxTokens": context, "overflow": "generation stops at maxTokens positions"},
        "weights": {
            "format": weight_format,
            "storageBits": fmt["bits"],
            "levels": [-fmt["qmax"], fmt["qmax"]],
            "method": fmt["method"],
            "groupSize": group_size,
            "groupAxis": "input",
            "scale": {
                "mantissa": "unsigned 15-bit (1..32767), one per (output row, group)",
                "exponent": "one per matrix",
            },
            "exponents": exponents,
            "classifier": "shared with the embedding table" if cfg.shared_classifier else "separate matrix",
        },
        "normWeights": {"format": "int16", "frac": norm_frac},
        "activations": {
            "format": "int16",
            "frac": frac,
            "calibration": {"prompts": len(calibration_prompts), "maxAbs": stats},
        },
        "scores": {"format": "int32", "frac": SCORE_FRAC, "scale": "multiply by round(2^15 / sqrt(head_size)), shift"},
        "logits": {"format": "int32", "frac": logits_frac},
        "accumulator": {
            "bits": acc_bits,
            "overflow": "impossible by construction (bound below)",
            "bound": bound,
            "order": "results are independent of summation order because no intermediate overflows",
        },
        "rounding": {
            "mode": "round-half-up",
            "definition": "rshift(v, s) = (v + 2^(s-1)) >> s with arithmetic shift for s > 0; v << -s for s < 0",
        },
        "saturation": "every int16 result is clamped to [-32768, 32767]; int32 results to [-2^31, 2^31 - 1]",
        "operators": {
            "matmul": "y = sat(rshift(sum_g m[o,g] * sum_{i in g} x[i] * q[o,i], frac_in + e - frac_out))",
            "embedding": "x[i] = sat16(rshift(q[t,i] * m[t, i // groupSize], e_emb - frac_x))",
            "rmsnorm": (
                "v = floor(sum x^2 / dim) + round(eps * 2^(2 frac_x)); v = max(v, 1); "
                "k = bitlength(v) - 1; e2 = k - (k mod 2); "
                "t = v >> (e2 - 6) (or v << (6 - e2)); r = lut.rsqrt[t - 64]; "
                "out = sat16(rshift(x * w * r, frac_norm + 15 + e2 / 2 - frac_out))"
            ),
            "rope": (
                "pair (a, b) at position p, pair index j: "
                "a' = sat16(rshift(a cos - b sin, 14)), b' = sat16(rshift(a sin + b cos, 14))"
            ),
            "attention": (
                "s = sat32(rshift(sum q k * round(2^15/sqrt(hs)), frac_q + frac_k + 15 - 10)); d = max(s) - s; "
                "u = min(d, 16 * 2^10 - 1); e = lut.exp[u >> 5]; p = floor(e * 2^15 / sum e); "
                "out = sat16(rshift(sum p v, 15))"
            ),
            "silu": (
                "z = clamp(h1, -8 * 2^frac_h1, 8 * 2^frac_h1 - 1); i = floor(z * 16 / 2^frac_h1) + 128; "
                "s = rshift(h1 * lut.sigmoid[i], 15); g = sat16(rshift(s * h3, frac_h1 + frac_h3 - frac_g))"
            ),
            "residual": "x = sat16(x + y)",
        },
        "luts": {
            "rsqrt": {
                "entries": 192,
                "format": "unsigned Q15",
                "definition": "round(2^15 / sqrt((t + 0.5) / 64)), t = 64..255",
            },
            "exp": {
                "entries": EXP_STEPS * EXP_RANGE,
                "format": "unsigned Q15",
                "definition": "round(2^15 * exp(-(i + 0.5) / 32))",
            },
            "sigmoid": {
                "entries": 2 * SIG_RANGE * SIG_STEPS,
                "format": "unsigned Q15",
                "definition": "round(2^15 * sigmoid((i - 128 + 0.5) / 16))",
            },
            "rope": {
                "entries": [context, cfg.head_size // 2],
                "format": "signed Q14",
                "definition": "round(2^14 * cos|sin(p * theta^(-2j/hs)))",
            },
        },
        "decode": {
            "rule": "greedy argmax over logits, lowest token id on ties",
            "stop": "token 1 (BOS) delimits sequences",
        },
        "observationPoints": [
            {
                "id": "attn_residual",
                "description": "residual stream after each attention block",
                "shape": ["layers", "dim"],
                "format": "int16",
                "frac": frac["x"],
            },
            {
                "id": "ffn_residual",
                "description": "residual stream after each feed-forward block",
                "shape": ["layers", "dim"],
                "format": "int16",
                "frac": frac["x"],
            },
            {
                "id": "logits",
                "description": "logits of each step",
                "shape": ["vocab"],
                "format": "int32",
                "frac": logits_frac,
            },
            {"id": "token", "description": "token emitted at each step", "shape": [], "format": "uint"},
        ],
        "arrays": {name: array_hash(a) for name, a in sorted(arrays.items())},
    }
    buf = io.BytesIO()
    np.savez(buf, meta=np.array(json.dumps({"format": WEIGHTS_FORMAT})), **arrays)
    return spec, buf.getvalue()


def storage_bits(spec: dict, arrays: dict[str, np.ndarray]) -> dict[str, int]:
    """Bits the chip must store: quantized weights, scale mantissas, norm weights and tables."""
    wbits = spec["weights"]["storageBits"]
    out = {"weights": 0, "scales": 0, "norms": 0, "luts": 0}
    for name, a in arrays.items():
        if name.endswith(".q"):
            out["weights"] += a.size * wbits
        elif name.endswith(".m"):
            out["scales"] += a.size * 15
        elif name.startswith("lut."):
            out["luts"] += a.size * 16
        else:
            out["norms"] += a.size * 16
    return out
