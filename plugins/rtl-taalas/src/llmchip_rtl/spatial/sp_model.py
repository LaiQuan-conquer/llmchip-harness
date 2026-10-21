"""Model of the spatial engine (taalas-spatial/1) at the level of its circuits, shipped in the
RTL bundle as the reference of the verification items that need one beyond the L2 trace
(sim-verilator: single token, full context). Standalone: Python 3.10 and NumPy only.

It follows the hardware, not L2's text: every matrix row is the sum of the shifted copies
its circuit adds (the CSD digits of the weights and of the group mantissas, `sp_mv_*.v`),
rounded and narrowed as the serial output stage does; the KV cache is the rotating ring
(rows beyond the position are masked, never cleared); the softmax divisions are the
restoring dividers, one quotient bit per step. The tests of rtl-taalas check it against L2.
"""

from __future__ import annotations

import numpy as np

POINT_ATTN, POINT_FFN, POINT_LOGITS = 1, 2, 3


def csd(m: int) -> list[tuple[int, int]]:
    """Canonical signed digits of a positive integer: [(shift, +1|-1)]."""
    out = []
    j = 0
    while m:
        if m & 1:
            d = 2 - (m & 3)
            out.append((j, d))
            m -= d
        m >>= 1
        j += 1
    return out


def rsh(v: int, s: int, mode: int) -> int:
    """Rounding shift by s >= 1 (0 half-up, 1 half-even, 2 truncate); s <= 0 shifts left."""
    if s <= 0:
        return v << -s
    if mode == 2:
        return v >> s
    if mode == 1:
        return (v + (1 << (s - 1)) - 1 + ((v >> s) & 1)) >> s
    return (v + (1 << (s - 1))) >> s


def narrow(v: int, bits: int, wrap: int) -> int:
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if wrap:
        return ((v - lo) & ((1 << bits) - 1)) + lo
    return min(max(v, lo), hi)


class Circuit:
    """One matrix as wired: per row, (input, shift, sign) terms, then the output stage."""

    def __init__(self, q: np.ndarray, m: np.ndarray, group: int, shift: int, width: int) -> None:
        self.rows = []
        for o in range(q.shape[0]):
            terms = []
            for i in range(q.shape[1]):
                if q[o, i]:
                    for jq, dq in csd(abs(int(q[o, i]))):
                        for jm, dm in csd(int(m[o, i // group])):
                            terms.append((i, jq + jm, dq * dm * (1 if q[o, i] > 0 else -1)))
            self.rows.append(terms)
        self.shift, self.width = shift, width

    def __call__(self, x: list[int], mode: int, wrap: int) -> list[int]:
        return [
            narrow(rsh(sum(s * (x[i] << j) for i, j, s in terms), self.shift, mode), self.width, wrap)
            for terms in self.rows
        ]


class Machine:
    """step(token, pos, traces) -> token, with the observation points appended to `traces` as
    {"point", "layer", "values"} (the interface of the instruction-level simulator)."""

    def __init__(self, image: dict, params: dict) -> None:
        p = params
        self.p = p
        self.mode, self.wrap = int(p["ROUND_MODE"]), int(p["OVERFLOW_WRAP"])
        self.dim, self.hidden, self.layers = p["DIM"], p["HIDDEN"], p["LAYERS"]
        self.hs, self.kv_dim, self.vocab, self.context = p["HS"], p["KV_DIM"], p["VOCAB"], p["CTX"]
        self.a = {k: np.asarray(image[k]).astype(np.int64) for k in image}
        g = p["GROUP"]
        self.mats = {}
        for name, shift, width, *_ in p["MATRICES"]:
            self.mats[name] = Circuit(self.a[f"{name}.q"], self.a[f"{name}.m"], g, shift, width)
        self.kring = np.zeros((self.layers, self.context, self.kv_dim), np.int64)
        self.vring = np.zeros((self.layers, self.context, self.kv_dim), np.int64)

    def rmsnorm(self, x: list[int], weights: np.ndarray) -> list[int]:
        p = self.p
        v = sum(e * e for e in x) // self.dim + p["EPS_Q"]
        v = max(v, 1)
        k = v.bit_length() - 1
        e2 = k - (k % 2)
        t = v >> (e2 - 6) if e2 >= 6 else v << (6 - e2)
        r = int(self.a["lut.rsqrt"][t - 64])
        s = p["FN"] + 15 + e2 // 2 - p["F_XN"]
        return [narrow(rsh(x[i] * int(weights[i]) * r, s, self.mode), 16, self.wrap) for i in range(self.dim)]

    def rope(self, v: list[int], pos: int) -> list[int]:
        cos, sin = self.a["lut.rope_cos"][pos], self.a["lut.rope_sin"][pos]
        out = []
        for i in range(len(v)):
            j = (i % self.hs) // 2
            a, b = v[i - (i % 2)], v[i - (i % 2) + 1]
            c, s = int(cos[j]), int(sin[j])
            val = a * s + b * c if i % 2 else a * c - b * s
            out.append(narrow(rsh(val, 14, self.mode), 16, self.wrap))
        return out

    def attention(self, q: list[int], layer: int, pos: int) -> list[int]:
        p = self.p
        hs, cap, ish = self.hs, p["EXP_CAP"], p["EXP_SHIFT"]
        out = []
        valid = range(pos + 1)
        for h in range(2):
            qh = q[h * hs : (h + 1) * hs]
            dots = [sum(qh[j] * int(self.kring[layer, t, j]) for j in range(hs)) for t in valid]
            scores = [narrow(rsh(d * p["ATT_SCALE"], p["SSH"], self.mode), 32, self.wrap) for d in dots]
            mx = max(scores)
            e = [int(self.a["lut.exp"][min(mx - s, cap) >> ish]) for s in scores]
            total = sum(e)
            probs = []
            for et in e:  # restoring division of et * 2^15 by the sum, one quotient bit per step
                rem, quotient = et, 0
                for _ in range(16):
                    bit = int(rem >= total)
                    quotient = (quotient << 1) | bit
                    rem = (rem - total * bit) << 1
                probs.append(quotient)
            for j in range(hs):
                acc = sum(probs[t] * int(self.vring[layer, t, j]) for t in valid)
                out.append(narrow(rsh(acc, 15, self.mode), 16, self.wrap))
        return out

    def silu(self, h1: list[int], h3: list[int]) -> list[int]:
        p = self.p
        fh, lim = p["F_H1"], p["SIG_LIMIT"]
        out = []
        for a, b in zip(h1, h3, strict=True):
            z = min(max(a, -lim), lim - 1)
            idx = (z >> (fh - 4) if fh >= 4 else z << (4 - fh)) + p["SIG_OFFSET"]
            s = rsh(a * int(self.a["lut.sigmoid"][idx]), 15, self.mode)
            out.append(narrow(rsh(s * b, p["SILU_SHIFT"], self.mode), 16, self.wrap))
        return out

    def step(self, token: int, pos: int, traces: list | None = None, mode: int = 1) -> int:
        if pos >= self.context:
            raise ValueError(f"position {pos} exceeds the context of {self.context}")
        traces = traces if traces is not None else []
        x = [int(v) for v in self.a["emb.table"][token]]
        for layer in range(self.layers):
            xn = self.rmsnorm(x, self.a[f"{layer}.rms_att"])
            q = self.rope(self.mats[f"{layer}.wq"](xn, self.mode, self.wrap), pos)
            self.kring[layer, pos] = self.rope(self.mats[f"{layer}.wk"](xn, self.mode, self.wrap), pos)
            self.vring[layer, pos] = self.mats[f"{layer}.wv"](xn, self.mode, self.wrap)
            att = self.attention(q, layer, pos)
            o = self.mats[f"{layer}.wo"](att, self.mode, self.wrap)
            x = [narrow(a + b, 16, self.wrap) for a, b in zip(x, o, strict=True)]
            traces.append({"point": POINT_ATTN, "layer": layer, "values": np.array(x, np.int64)})
            xn = self.rmsnorm(x, self.a[f"{layer}.rms_ffn"])
            h1 = self.mats[f"{layer}.w1"](xn, self.mode, self.wrap)
            h3 = self.mats[f"{layer}.w3"](xn, self.mode, self.wrap)
            f = self.mats[f"{layer}.w2"](self.silu(h1, h3), self.mode, self.wrap)
            x = [narrow(a + b, 16, self.wrap) for a, b in zip(x, f, strict=True)]
            traces.append({"point": POINT_FFN, "layer": layer, "values": np.array(x, np.int64)})
        xn = self.rmsnorm(x, self.a["rms_final"])
        logits = self.mats["cls"](xn, self.mode, self.wrap)
        traces.append({"point": POINT_LOGITS, "layer": 0, "values": np.array(logits, np.int64)})
        return int(np.argmax(logits))
