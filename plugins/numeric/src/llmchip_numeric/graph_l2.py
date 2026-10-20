"""L2 of any recovered graph: quantization and bit-exact integer execution (S2 analysis).

`l2.py` executes the numeric spec on the llama2.c structure that the vector engine
implements. This module executes the same operators, tables, rounding, overflow and
accumulator rules (`arith.py`) on the computation graph recovered in S1
(`llmchip_model.graph`), so that every recovered model (Llama, Qwen2 with QKV
bias, Qwen3 with per-head QK norm, SmolLM, llama2.c) gets an integer model and its
accuracy against L0. On a llama2.c graph it computes exactly what `L2Model` does
(tests/test_graph_l2.py), which is what ties the two together.

Additions to the operators of the chip L2:
- linear with bias: y = narrow16(rshift(sum, s) + b), b int32 at the output's fraction bits;
- head_rmsnorm (QK norm): the rmsnorm of each head of q or k, input fraction bits of q / k;
- rope "half" (Hugging Face layout): element j of a head is rotated with element j + hd/2.

The whole sequence is executed at once with a causal mask (no KV cache): per position
this is the same arithmetic as the chip's token-by-token execution. Matrix products are
computed exactly with float32 BLAS: x = 256 * hi + lo splits each int16 activation into
parts of at most 8 bits, so every product and partial sum over a group of at most 256
weights stays an integer below 2^24, which float32 represents exactly.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable

import numpy as np
from llmchip_model.execute import forward as float_forward

from . import arith
from .spec import (
    EXP_RANGE,
    EXP_STEPS,
    GRAPH_SPEC_FORMAT,
    SCORE_FRAC,
    SIG_RANGE,
    SIG_STEPS,
    WEIGHT_FORMATS,
    accumulator_spec,
    exp_lut,
    frac_bits,
    group_scales,
    mantissas,
    matrix_exponent,
    overflow_spec,
    quantize_rows,
    rounding_spec,
    rsqrt_lut,
    sigmoid_lut,
    weight_example,
)

GRAPH_WEIGHTS_FORMAT = "llmchip-numeric-graph-weights/1"
KINDS = ("x", "xn", "q", "k", "qn", "kn", "v", "h1", "h3", "g")
# The tensor kind (and so the fraction bits) of each node's output, by node id.
KIND_OF = {
    "embed": "x",
    "attn_norm": "xn",
    "mlp_norm": "xn",
    "final_norm": "xn",
    "q_proj": "q",
    "k_proj": "k",
    "v_proj": "v",
    "q_norm": "qn",
    "k_norm": "kn",
    "o_proj": "x",
    "residual_attn": "x",
    "residual_mlp": "x",
    "gate_proj": "h1",
    "up_proj": "h3",
    "gate_mul": "g",
    "down_proj": "x",
}
EXACT_GROUP_MAX = 256  # 127 * 255 * 256 < 2^24: float32 sums of a group are exact
ROW_CHUNK_ELEMENTS = 1 << 24  # groups x positions x rows of one product chunk

Weights = Callable[[str], np.ndarray]


def array_hash(a: np.ndarray) -> str:
    """Hash of an array as stored (dtype, shape and bytes)."""
    h = hashlib.sha256(f"{a.dtype.str}{a.shape}".encode())
    h.update(np.ascontiguousarray(a).tobytes())
    return h.hexdigest()


def rope_tables(theta: float, hd: int, context: int) -> tuple[np.ndarray, np.ndarray]:
    """cos / sin of every (position, pair) in signed Q14, as `spec.rope_luts`."""
    j = np.arange(hd // 2)
    freq = 1.0 / np.power(theta, 2 * j / hd)
    angle = np.arange(context)[:, None] * freq[None, :]
    return np.round(np.cos(angle) * 16384).astype(np.int64), np.round(np.sin(angle) * 16384).astype(np.int64)


def _name(node: dict, key: str, layer: int | None) -> str:
    return node[key] if layer is None else node[key].format(i=layer)


def _key(node: dict, layer: int | None) -> str:
    return node["id"] if layer is None else f"{layer}.{node['id']}"


def _nodes(graph: dict):
    """(node, layer) in execution order."""
    n = graph["nodes"]
    for node in n["prologue"]:
        yield node, None
    for i in range(n["repeat"]):
        for node in n["block"]:
            yield node, i
    for node in n["epilogue"]:
        yield node, None


def calibrate(graph: dict, w: Weights, sequences: list[list[int]]) -> dict[str, float]:
    """The largest magnitude of every tensor kind on the float graph (L0): `stats[kind]` over the
    whole model and `stats["<layer>.<kind>"]` per decoder layer."""
    stats: dict[str, float] = {}

    def seen(kind: str, layer: int | None, value: np.ndarray) -> None:
        m = float(np.abs(value).max())
        stats[kind] = max(stats.get(kind, 0.0), m)
        if layer is not None:
            stats[f"{layer}.{kind}"] = max(stats.get(f"{layer}.{kind}", 0.0), m)

    kinds_in: dict[str, str] = {}

    def observe(node: dict, outputs: list[np.ndarray], layer: int | None) -> None:
        if node["op"] == "rope":
            # The rotation keeps the format of its inputs (q / k or qn / kn).
            for name, value in zip(node["inputs"], outputs, strict=True):
                seen(kinds_in[name], layer, value)
            return
        if node["op"] == "linear" and node["id"] == "lm_head":
            seen("logits", None, outputs[0])
            return
        kind = KIND_OF.get(node["id"])
        if kind:
            seen(kind, layer, outputs[0])
            kinds_in[node["outputs"][0]] = kind

    for ids in sequences:
        float_forward(graph, w, ids, observe=observe)
    return stats


def quantize_matrix_chunked(w: np.ndarray, fmt: str, group: int, chunk: int = 8192):
    """`spec.quantize_matrix` in row chunks (same result, bounded memory for large tables)."""
    rows = w.shape[0]
    scale = np.concatenate([group_scales(w[r : r + chunk], fmt, group) for r in range(0, rows, chunk)])
    e = matrix_exponent(scale)
    m = mantissas(scale, e)
    q = np.concatenate(
        [quantize_rows(w[r : r + chunk], m[r : r + chunk], e, fmt, group) for r in range(0, rows, chunk)]
    )
    return q, m, e


def build(
    graph: dict,
    peek: Weights,
    *,
    weight_format: str = "int8",
    group_size: int = 32,
    context: int = 256,
    rounding: str = "half-up",
    overflow: str = "saturate",
    accumulator_bits: int | None = None,
    per_layer: bool = False,
    residual_bits: int = 16,
    graph_hash: str = "",
    stats: dict[str, float] | None = None,
    frac: dict[str, int] | None = None,
    logits_frac: int | None = None,
    progress: Callable[[str], None] = lambda message: None,
) -> tuple[dict, dict[str, np.ndarray]]:
    """Quantizes a recovered graph: (graph numeric spec, arrays). `stats` comes from `calibrate`;
    `frac` / `logits_frac` override the calibration (used to compare with the chip L2).

    With `per_layer`, every decoder layer gets its own fraction bits for each tensor kind
    (the residual stream keeps one format: it runs through all layers). `residual_bits`
    widens the residual stream (16 or 24 bits); it never feeds a matrix product directly,
    so every product keeps its exact 16-bit inputs."""
    if weight_format not in WEIGHT_FORMATS:
        raise ValueError(f"unknown weight format {weight_format}")
    if rounding not in arith.ROUNDING or overflow not in arith.OVERFLOW:
        raise ValueError(f"unknown rounding {rounding} or overflow {overflow}")
    if group_size > EXACT_GROUP_MAX:
        raise ValueError(f"groups of more than {EXACT_GROUP_MAX} weights are not supported")
    if not graph.get("supported"):
        raise ValueError("the graph was not recovered")
    h = graph["hyper"]
    stats = stats or {}
    if residual_bits not in (16, 24):
        raise ValueError("the residual stream is 16 or 24 bits wide")
    bits = {kind: residual_bits if kind == "x" else 16 for kind in KINDS}
    if frac is None:
        frac = {kind: frac_bits(stats.get(kind, 1.0), total=bits[kind]) for kind in KINDS}
    layer_frac = None
    if per_layer:
        layer_frac = [
            {kind: frac_bits(stats.get(f"{i}.{kind}", stats.get(kind, 1.0))) for kind in KINDS if kind != "x"}
            for i in range(h["layers"])
        ]
    if logits_frac is None:
        logits_frac = frac_bits(stats.get("logits", 1.0), total=32, margin=4.0)

    arrays: dict[str, np.ndarray] = {}
    exponents: dict[str, int] = {}
    norms: dict[str, np.ndarray] = {}
    quantized: dict[str, str] = {}  # weight tensor -> array key (a tied output reuses the embedding)
    example = None
    for node, layer in _nodes(graph):
        key = _key(node, layer)
        if node["op"] in ("embedding", "linear"):
            name = _name(node, "weight", layer)
            if name in quantized:
                continue
            progress(f"quantize {name}")
            w = peek(name)
            q, m, e = quantize_matrix_chunked(w, weight_format, group_size)
            if example is None and node["op"] == "linear":
                example = weight_example(name, w[0], q[0], m[0], e, group_size)
            del w
            arrays[f"{key}.q"] = q.astype(np.int8)
            arrays[f"{key}.m"] = m.astype(np.int16)
            exponents[key] = e
            quantized[name] = key
            if node.get("bias"):
                kind = KIND_OF[node["id"]]
                fo = layer_frac[layer][kind] if layer_frac and layer is not None and kind != "x" else frac[kind]
                b = np.round(peek(_name(node, "bias", layer)).astype(np.float64) * 2.0**fo)
                arrays[f"{key}.b"] = np.clip(b, -(2**31), 2**31 - 1).astype(np.int32)
        elif node["op"] in ("rmsnorm", "head_rmsnorm"):
            norms[key] = peek(_name(node, "weight", layer)).astype(np.float64)
    norm_frac = frac_bits(max(float(np.abs(v).max()) for v in norms.values()), margin=1.0)
    for key, v in norms.items():
        arrays[f"{key}.w"] = np.clip(np.round(v * 2.0**norm_frac), -32768, 32767).astype(np.int16)
    hd = h["headDim"]
    cos, sin = rope_tables(h["ropeTheta"], hd, context)
    arrays |= {
        "lut.rsqrt": rsqrt_lut().astype(np.int32),
        "lut.exp": exp_lut().astype(np.int32),
        "lut.sigmoid": sigmoid_lut().astype(np.int32),
        "lut.rope_cos": cos.astype(np.int32),
        "lut.rope_sin": sin.astype(np.int32),
    }

    fmt = WEIGHT_FORMATS[weight_format]
    widest = max(h["hidden"], h["intermediate"], h["heads"] * hd)
    lm_head = graph["nodes"]["epilogue"][1]
    spec = {
        "format": GRAPH_SPEC_FORMAT,
        "version": "1",
        "target": "analysis of a recovered graph; the vector engine implements the llama2.c structure only",
        "model": {"family": graph["family"], "architecture": graph.get("architecture"), "graphHash": graph_hash},
        "hyper": {k: h[k] for k in ("layers", "hidden", "heads", "kvHeads", "headDim", "intermediate", "vocab")},
        "context": {"maxTokens": context},
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
            "classifier": "shared with the embedding table" if lm_head.get("tiedTo") else "separate matrix",
            "biases": "int32 at the fraction bits of the output",
        },
        "normWeights": {"format": "int16", "frac": norm_frac},
        "activations": {
            "format": "int16" if residual_bits == 16 else f"int16; residual stream int{residual_bits}",
            "bits": bits,
            "granularity": "layer" if per_layer else "kind",
            "frac": frac,
            **({"layerFrac": layer_frac} if layer_frac else {}),
            "calibration": {"maxAbs": stats},
        },
        "scores": {"format": "int32", "frac": SCORE_FRAC, "scale": "multiply by round(2^15 / sqrt(head_dim)), shift"},
        "logits": {"format": "int32", "frac": logits_frac},
        "accumulator": accumulator_spec(widest, fmt["qmax"], group_size, accumulator_bits, overflow),
        "rounding": rounding_spec(rounding),
        "overflow": overflow_spec(overflow),
        "operators": {
            "linear": "y = narrow(rshift(sum_g m[o,g] * sum_{i in g} x[i] q[o,i], frac_in + e - frac_out) + b[o])",
            "head_rmsnorm": "the rmsnorm of the numeric spec on every head of q or k (input fraction bits of q / k)",
            "rope": "interleaved pairs (llama2.c) or element j with j + head_dim/2 (Hugging Face), Q14 tables",
            "others": "embedding, rmsnorm, attention, silu, residual as in llmchip-numeric-spec/1",
        },
        "luts": {
            "rsqrt": {"entries": 192, "format": "unsigned Q15"},
            "exp": {"entries": EXP_STEPS * EXP_RANGE, "format": "unsigned Q15"},
            "sigmoid": {"entries": 2 * SIG_RANGE * SIG_STEPS, "format": "unsigned Q15"},
            "rope": {"entries": [context, hd // 2], "format": "signed Q14"},
        },
        "decode": {"rule": "greedy argmax over logits, lowest token id on ties"},
        "arrays": {name: array_hash(a) for name, a in sorted(arrays.items())},
        "example": example,
    }
    return spec, arrays


def storage_bits(spec: dict, arrays: dict[str, np.ndarray]) -> dict[str, int]:
    """Bits a chip would store: quantized weights, scale mantissas, biases, norm weights and tables."""
    wbits = spec["weights"]["storageBits"]
    out = {"weights": 0, "scales": 0, "biases": 0, "norms": 0, "luts": 0}
    for name, a in arrays.items():
        if name.endswith(".q"):
            out["weights"] += a.size * wbits
        elif name.endswith(".m"):
            out["scales"] += a.size * 15
        elif name.endswith(".b"):
            out["biases"] += a.size * 32
        elif name.startswith("lut."):
            out["luts"] += a.size * 16
        else:
            out["norms"] += a.size * 16
    return out


class GraphL2:
    """Executes a graph numeric spec with integer arithmetic (exact)."""

    def __init__(self, graph: dict, spec: dict, arrays: dict[str, np.ndarray], cache_bytes: float = 3e9) -> None:
        if spec.get("format") != GRAPH_SPEC_FORMAT:
            raise ValueError("not a graph numeric spec")
        for name, digest in spec["arrays"].items():
            if name not in arrays or array_hash(arrays[name]) != digest:
                raise ValueError(f"array {name} does not match the numeric spec")
        self.graph, self.spec, self.a = graph, spec, arrays
        act = spec["activations"]
        self.f = act["frac"]
        self.layer_frac = act.get("layerFrac")
        self.bits = act.get("bits", {})
        self.fn = spec["normWeights"]["frac"]
        self.fl = spec["logits"]["frac"]
        self.group = spec["weights"]["groupSize"]
        self.exp = spec["weights"]["exponents"]
        self.context = spec["context"]["maxTokens"]
        self.rounding = spec["rounding"]["mode"]
        self.overflow = spec["overflow"]["mode"]
        acc = spec["accumulator"]
        self.acc_bits = acc["bits"] if acc.get("canOverflow") else None
        self.att_scale = round(2**15 / math.sqrt(spec["hyper"]["headDim"]))
        self.tied = {}  # weight tensor -> array key
        for node, layer in _nodes(graph):
            if node["op"] in ("embedding", "linear"):
                self.tied.setdefault(_name(node, "weight", layer), _key(node, layer))
        # Float32 copies of the quantized matrices, laid out per group, while they fit the budget.
        self.cache_bytes = cache_bytes
        self._cache: dict[str, np.ndarray] = {}
        self._cached = 0
        self.saturations: dict[str, int] = {}

    # -- arithmetic -------------------------------------------------------------

    def _count(self, kind: str, n: int) -> None:
        if n:
            self.saturations[kind] = self.saturations.get(kind, 0) + n

    def _narrow(self, v: np.ndarray, bits: int, kind: str, where: np.ndarray | None = None) -> np.ndarray:
        self._count(kind, arith.out_of_range(v if where is None else np.where(where, v, 0), bits))
        return arith.overflow(v, bits, self.overflow)

    def _rsh(self, v, s):
        return arith.rshift(v, s, self.rounding)

    # -- operators --------------------------------------------------------------

    def _grouped(self, key: str, r0: int, r1: int) -> np.ndarray:
        """Rows r0..r1 of a quantized matrix as float32 (groups, group size, rows)."""
        cached = self._cache.get(key)
        if cached is not None:
            return cached[:, :, r0:r1]
        q = self.a[f"{key}.q"]
        out, inp = q.shape
        ngroups = -(-inp // self.group)
        full = q.size * 4 <= self.cache_bytes - self._cached
        lo, hi = (0, out) if full else (r0, r1)
        padded = np.zeros((hi - lo, ngroups * self.group), np.float32)
        padded[:, :inp] = q[lo:hi]
        grouped = np.ascontiguousarray(padded.reshape(hi - lo, ngroups, self.group).transpose(1, 2, 0))
        if full:
            self._cache[key] = grouped
            self._cached += grouped.nbytes
            return grouped[:, :, r0:r1]
        return grouped

    def linear(self, key: str, x: np.ndarray, f_in: int, f_out: int, kind: str, bits: int = 16) -> np.ndarray:
        q, m = self.a[f"{key}.q"], self.a[f"{key}.m"]
        out, inp = q.shape
        ngroups = m.shape[1]
        T = x.shape[0]
        xp = np.zeros((T, ngroups * self.group), np.int64)
        xp[:, :inp] = x
        # Exact split: x = 256 * hi + lo with hi in [-128, 127] and lo in [0, 255].
        hi = (xp >> 8).astype(np.float32).reshape(T, ngroups, self.group).transpose(1, 0, 2)
        lo = (xp & 255).astype(np.float32).reshape(T, ngroups, self.group).transpose(1, 0, 2)
        rows = max(1, ROW_CHUNK_ELEMENTS // max(1, ngroups * T))
        y = np.empty((T, out), np.int64)
        shift = f_in + self.exp[key] - f_out
        for r0 in range(0, out, rows):
            r1 = min(out, r0 + rows)
            wq = self._grouped(key, r0, r1)
            part = np.matmul(hi, wq).astype(np.int64) * 256 + np.matmul(lo, wq).astype(np.int64)  # (G, T, rows)
            terms = part * m[r0:r1].astype(np.int64).T[:, None, :]
            total, events = arith.accumulate(np.moveaxis(terms, 0, -1), self.acc_bits, self.overflow)
            self._count("accumulator", events)
            y[:, r0:r1] = self._rsh(total, shift)
        if f"{key}.b" in self.a:
            y = y + self.a[f"{key}.b"].astype(np.int64)
        return self._narrow(y, bits, kind)

    def rmsnorm(self, x: np.ndarray, weight: np.ndarray, f_in: int, f_out: int, eps: float) -> np.ndarray:
        """Rows of x (rows, n) normalized as in the numeric spec (rsqrt table, even exponent)."""
        eps_q = round(eps * 2.0 ** (2 * f_in))
        v = np.maximum((x * x).sum(axis=-1) // x.shape[-1] + eps_q, 1)
        k = np.frexp(v.astype(np.float64))[1].astype(np.int64) - 1  # floor(log2 v), exact below 2^53
        e2 = k - (k % 2)
        t = np.where(e2 >= 6, v >> np.maximum(e2 - 6, 0), v << np.maximum(6 - e2, 0))
        r = self.a["lut.rsqrt"].astype(np.int64)[t - 64]
        shift = (self.fn + 15 + e2 // 2 - f_out)[:, None]
        return self._narrow(self._rsh(x * weight.astype(np.int64)[None, :] * r[:, None], shift), 16, "norm")

    def rope(self, v: np.ndarray, heads: int, hd: int, style: str, kind: str) -> np.ndarray:
        T = v.shape[0]
        if self.context < T:
            raise ValueError(f"{T} positions exceed the context of {self.context}")
        t = v.reshape(T, heads, hd)
        c = self.a["lut.rope_cos"].astype(np.int64)[:T][:, None, :]
        s = self.a["lut.rope_sin"].astype(np.int64)[:T][:, None, :]
        out = np.empty_like(t)
        if style == "half":
            a, b = t[..., : hd // 2], t[..., hd // 2 :]
            out[..., : hd // 2] = self._narrow(self._rsh(a * c - b * s, 14), 16, kind)
            out[..., hd // 2 :] = self._narrow(self._rsh(b * c + a * s, 14), 16, kind)
        else:
            a, b = t[..., 0::2], t[..., 1::2]
            out[..., 0::2] = self._narrow(self._rsh(a * c - b * s, 14), 16, kind)
            out[..., 1::2] = self._narrow(self._rsh(a * s + b * c, 14), 16, kind)
        return out.reshape(T, heads * hd)

    def attention(self, q, k, v, fq: int, fk: int, heads: int, kv_heads: int, hd: int, window) -> np.ndarray:
        T = q.shape[0]
        group = heads // kv_heads
        qh = q.reshape(T, heads, hd).transpose(1, 0, 2)
        kh = np.repeat(k.reshape(T, kv_heads, hd).transpose(1, 0, 2), group, axis=0)
        vh = np.repeat(v.reshape(T, kv_heads, hd).transpose(1, 0, 2), group, axis=0)
        i, j = np.arange(T)[:, None], np.arange(T)[None, :]
        allowed = j <= i
        if window:
            allowed &= j > i - window
        allowed = np.broadcast_to(allowed[None], (heads, T, T))
        raw = self._rsh(np.matmul(qh, kh.transpose(0, 2, 1)) * self.att_scale, fq + fk + 15 - SCORE_FRAC)
        s = self._narrow(raw, 32, "score", allowed)
        smax = np.where(allowed, s, np.iinfo(np.int64).min).max(axis=-1, keepdims=True)
        d = np.where(allowed, np.minimum(smax - s, EXP_RANGE * 2**SCORE_FRAC - 1), 0)
        e = np.where(allowed, self.a["lut.exp"].astype(np.int64)[d >> (SCORE_FRAC - 5)], 0)
        p = (e << 15) // e.sum(axis=-1, keepdims=True)
        out = self._narrow(self._rsh(np.matmul(p, vh), 15), 16, "att")
        return out.transpose(1, 0, 2).reshape(T, heads * hd)

    def silu_gate(self, h1: np.ndarray, f1: int, h3: np.ndarray, f3: int, fg: int) -> np.ndarray:
        z = np.clip(h1, -SIG_RANGE * 2**f1, SIG_RANGE * 2**f1 - 1)
        idx = (z >> (f1 - 4) if f1 >= 4 else z << (4 - f1)) + SIG_RANGE * SIG_STEPS
        s = self._rsh(h1 * self.a["lut.sigmoid"].astype(np.int64)[idx], 15)
        return self._narrow(self._rsh(s * h3, f1 + f3 - fg), 16, "g")

    # -- forward ------------------------------------------------------------------

    def frac(self, kind: str, layer: int | None) -> int:
        """Fraction bits of a tensor kind in a layer (the residual stream has one format)."""
        if kind != "x" and layer is not None and self.layer_frac:
            return self.layer_frac[layer][kind]
        return self.f[kind]

    def forward(self, tokens: list[int], hidden: list[np.ndarray] | None = None) -> np.ndarray:
        """Integer logits (T, vocab; int32 values, fraction bits `logits.frac`) of every position.
        `hidden` receives the residual stream (fraction bits of x) after the embedding and each
        layer, and the output of the final norm (fraction bits of xn), as `execute.forward`."""
        ids = np.asarray(tokens, dtype=np.int64)
        bx = self.bits.get("x", 16)
        env: dict[str, np.ndarray] = {}
        fr: dict[str, int] = {}
        lazy: dict[str, str] = {}  # silu outputs, applied by the following multiplication
        for node, layer in _nodes(self.graph):
            op, a, key = node["op"], node.get("attrs", {}), _key(node, layer)
            ins, outs = node["inputs"], node["outputs"]
            if op == "embedding":
                q = self.a[f"{key}.q"][ids].astype(np.int64)
                m = self.a[f"{key}.m"][ids].astype(np.int64)
                idx = np.arange(q.shape[1]) // self.group
                env[outs[0]] = self._narrow(self._rsh(q * m[:, idx], self.exp[key] - self.f["x"]), bx, "x")
                fr[outs[0]] = self.f["x"]
            elif op == "rmsnorm":
                fo = self.frac("xn", layer)
                env[outs[0]] = self.rmsnorm(env[ins[0]], self.a[f"{key}.w"], fr[ins[0]], fo, a["eps"])
                fr[outs[0]] = fo
            elif op == "linear":
                wkey = self.tied[_name(node, "weight", layer)]
                if node["id"] == "lm_head":
                    env[outs[0]] = self.linear(wkey, env[ins[0]], fr[ins[0]], self.fl, "logits", bits=32)
                    fr[outs[0]] = self.fl
                else:
                    kind = KIND_OF[node["id"]]
                    fo = self.frac(kind, layer)
                    env[outs[0]] = self.linear(wkey, env[ins[0]], fr[ins[0]], fo, kind, self.bits.get(kind, 16))
                    fr[outs[0]] = fo
            elif op == "head_rmsnorm":
                x = env[ins[0]]
                rows = x.reshape(-1, a["headDim"])
                kind = KIND_OF[node["id"]]
                fo = self.frac(kind, layer)
                env[outs[0]] = self.rmsnorm(rows, self.a[f"{key}.w"], fr[ins[0]], fo, a["eps"]).reshape(x.shape)
                fr[outs[0]] = fo
            elif op == "rope":
                hd = a["headDim"]
                env[outs[0]] = self.rope(env[ins[0]], a["heads"], hd, a["style"], "q")
                env[outs[1]] = self.rope(env[ins[1]], a["kvHeads"], hd, a["style"], "k")
                fr[outs[0]], fr[outs[1]] = fr[ins[0]], fr[ins[1]]
            elif op == "attention":
                q, k, v = (env[n] for n in ins)
                env[outs[0]] = self.attention(
                    q, k, v, fr[ins[0]], fr[ins[1]], a["heads"], a["kvHeads"], a["headDim"], a.get("window")
                )
                fr[outs[0]] = fr[ins[2]]
            elif op == "add":
                env[outs[0]] = self._narrow(env[ins[0]] + env[ins[1]], bx, "x")
                fr[outs[0]] = fr[ins[0]]
            elif op == "silu":
                lazy[outs[0]] = ins[0]
                env[outs[0]] = env[ins[0]]
                fr[outs[0]] = fr[ins[0]]
            elif op == "mul":
                gate, up = ins
                if gate not in lazy:
                    raise ValueError("a multiplication without a preceding silu is not supported")
                fo = self.frac("g", layer)
                env[outs[0]] = self.silu_gate(env[lazy.pop(gate)], fr[gate], env[up], fr[up], fo)
                fr[outs[0]] = fo
            elif op == "argmax":
                continue
            else:
                raise ValueError(f"unknown op {op}")
            if hidden is not None:
                if node["id"] == "embed" or (node["id"] == "residual_mlp" and layer is not None):
                    hidden.append(env["x"])
                elif node["id"] == "final_norm":
                    hidden.append(env[outs[0]])
        return env["logits"]
