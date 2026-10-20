"""L2: the bit-exact integer reference model (requirements §3.4.3).

Executes a numeric spec with integer arithmetic only (NumPy int64, exact).
Every operation is spelled out in the spec's `operators` section; this file
is the executable form of that text, and the RTL must reproduce its
observation points bit for bit.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import arith
from .spec import (
    EXP_RANGE,
    GRAPH_SPEC_FORMAT,
    SCORE_FRAC,
    SIG_RANGE,
    SIG_STEPS,
    SPEC_FORMAT,
    WEIGHTS_FORMAT,
    array_hash,
)

I16 = (-32768, 32767)
I32 = (-(2**31), 2**31 - 1)


def rshift(v, s: int):
    """Round-half-up shift: (v + 2^(s-1)) >> s; a negative s shifts left (the default rounding)."""
    return arith.rshift(v, s, "half-up")


def sat16(v):
    return np.clip(v, *I16)


def sat32(v):
    return np.clip(v, *I32)


def check_format(spec: dict) -> None:
    if spec.get("format") == GRAPH_SPEC_FORMAT:
        raise ValueError(
            "this numeric spec is an analysis of a recovered graph; the vector engine "
            "implements the llama2.c structure only (numeric/quantize on a Model IR)"
        )
    if spec.get("format") != SPEC_FORMAT:
        raise ValueError("not a numeric spec")


class SpecMismatch(ValueError):
    """The weights file does not belong to the numeric spec."""


class L2Model:
    def __init__(self, spec: dict, arrays: dict[str, np.ndarray]) -> None:
        check_format(spec)
        for name, digest in spec["arrays"].items():
            if name not in arrays or array_hash(arrays[name]) != digest:
                raise SpecMismatch(f"array {name} does not match the numeric spec")
        self.spec = spec
        self.a = arrays
        c = spec["model"]["config"]
        self.dim, self.hidden, self.layers = c["dim"], c["hidden_dim"], c["n_layers"]
        self.heads, self.kv_heads, self.vocab = c["n_heads"], c["n_kv_heads"], c["vocab_size"]
        self.hs = self.dim // self.heads
        self.kv_dim = self.kv_heads * self.hs
        self.context = spec["context"]["maxTokens"]
        self.group = spec["weights"]["groupSize"]
        self.exp = spec["weights"]["exponents"]
        self.f = spec["activations"]["frac"]
        self.fn = spec["normWeights"]["frac"]
        self.fl = spec["logits"]["frac"]
        self.shared = c["shared_classifier"]
        # Norm inputs are always the residual stream x.
        self.eps_q = round(c["norm_eps"] * 2.0 ** (2 * self.f["x"]))
        self.att_scale = round(2**15 / np.sqrt(self.hs))
        # Rounding, overflow and accumulator width (defaults: half-up, saturate, no overflow).
        self.rounding = spec.get("rounding", {}).get("mode", "half-up")
        self.overflow = spec.get("overflow", {}).get("mode", "saturate")
        acc = spec["accumulator"]
        self.acc_bits = acc["bits"] if acc.get("canOverflow") else None
        # Overflow events by kind: values that did not fit and were saturated or wrapped.
        self.saturations: dict[str, int] = {}
        self.reset()

    @classmethod
    def load(cls, spec_path: str | Path, weights_path: str | Path) -> L2Model:
        spec = json.loads(Path(spec_path).read_text())
        check_format(spec)
        with np.load(weights_path, allow_pickle=False) as z:
            if json.loads(str(z["meta"])).get("format") != WEIGHTS_FORMAT:
                raise ValueError("not a numeric weights file")
            arrays = {k: z[k].astype(np.int64) for k in z.files if k != "meta"}
        return cls(spec, arrays)

    def reset(self) -> None:
        self.kcache = np.zeros((self.layers, self.context, self.kv_dim), np.int64)
        self.vcache = np.zeros((self.layers, self.context, self.kv_dim), np.int64)

    # -- operators -------------------------------------------------------------

    def _count(self, kind: str, n: int) -> None:
        if n:
            self.saturations[kind] = self.saturations.get(kind, 0) + n

    def _sat16(self, v: np.ndarray, kind: str) -> np.ndarray:
        self._count(kind, arith.out_of_range(v, 16))
        return arith.overflow(v, 16, self.overflow)

    def _sat32(self, v: np.ndarray, kind: str) -> np.ndarray:
        self._count(kind, arith.out_of_range(v, 32))
        return arith.overflow(v, 32, self.overflow)

    def _rsh(self, v, s: int):
        return arith.rshift(v, s, self.rounding)

    def matmul(self, x: np.ndarray, name: str, f_in: int, f_out: int, kind: str, bits32: bool = False) -> np.ndarray:
        q, m = self.a[f"{name}.q"], self.a[f"{name}.m"]
        out, inp = q.shape
        ngroups = m.shape[1]
        xp = np.zeros(ngroups * self.group, np.int64)
        xp[:inp] = x
        qp = np.zeros((out, ngroups * self.group), np.int64)
        qp[:, :inp] = q
        acc = np.einsum("ogk,gk->og", qp.reshape(out, ngroups, self.group), xp.reshape(ngroups, self.group))
        total, events = arith.accumulate(acc * m, self.acc_bits, self.overflow)
        self._count("accumulator", events)
        y = self._rsh(total, f_in + self.exp[name] - f_out)
        return self._sat32(y, kind) if bits32 else self._sat16(y, kind)

    def rmsnorm(self, x: np.ndarray, weight: np.ndarray, f_out: int) -> np.ndarray:
        v = int((x * x).sum()) // x.size + self.eps_q
        v = max(v, 1)
        k = v.bit_length() - 1
        e2 = k - (k % 2)
        t = v >> (e2 - 6) if e2 >= 6 else v << (6 - e2)
        r = int(self.a["lut.rsqrt"][t - 64])
        return self._sat16(self._rsh(x * weight * r, self.fn + 15 + e2 // 2 - f_out), "norm")

    def rope(self, v: np.ndarray, pos: int, kind: str) -> np.ndarray:
        cos, sin = self.a["lut.rope_cos"][pos], self.a["lut.rope_sin"][pos]
        j = (np.arange(0, v.size, 2) % self.hs) // 2
        a, b = v[0::2], v[1::2]
        out = np.empty_like(v)
        out[0::2] = self._sat16(self._rsh(a * cos[j] - b * sin[j], 14), kind)
        out[1::2] = self._sat16(self._rsh(a * sin[j] + b * cos[j], 14), kind)
        return out

    def attention(self, q: np.ndarray, layer: int, pos: int) -> np.ndarray:
        f = self.f
        out = np.zeros(self.dim, np.int64)
        kv_mul = self.heads // self.kv_heads
        exp_lut = self.a["lut.exp"]
        cap = EXP_RANGE * 2**SCORE_FRAC - 1
        for h in range(self.heads):
            kvh = (h // kv_mul) * self.hs
            qh = q[h * self.hs : (h + 1) * self.hs]
            keys = self.kcache[layer, : pos + 1, kvh : kvh + self.hs]
            s = self._sat32(self._rsh((keys @ qh) * self.att_scale, f["q"] + f["k"] + 15 - SCORE_FRAC), "score")
            d = np.minimum(s.max() - s, cap)
            e = exp_lut[d >> (SCORE_FRAC - 5)]
            p = (e << 15) // e.sum()
            vals = self.vcache[layer, : pos + 1, kvh : kvh + self.hs]
            out[h * self.hs : (h + 1) * self.hs] = self._sat16(self._rsh(p @ vals, 15), "att")
        return out

    def silu_gate(self, h1: np.ndarray, h3: np.ndarray) -> np.ndarray:
        f = self.f
        fh = f["h1"]
        z = np.clip(h1, -SIG_RANGE * 2**fh, SIG_RANGE * 2**fh - 1)
        idx = (z >> (fh - 4) if fh >= 4 else z << (4 - fh)) + SIG_RANGE * SIG_STEPS
        s = self._rsh(h1 * self.a["lut.sigmoid"][idx], 15)
        return self._sat16(self._rsh(s * h3, fh + f["h3"] - f["g"]), "g")

    # -- forward ---------------------------------------------------------------

    def forward(self, token: int, pos: int, trace: dict | None = None) -> np.ndarray:
        """Integer logits (int32, frac `logits.frac`) for `token` at `pos`."""
        if pos >= self.context:
            raise ValueError(f"position {pos} exceeds the context of {self.context}")
        f = self.f
        g = self.group
        q, m = self.a["emb.q"][token], self.a["emb.m"][token]
        x = self._sat16(self._rsh(q * m[np.arange(self.dim) // g], self.exp["emb"] - f["x"]), "x")
        for layer in range(self.layers):
            xn = self.rmsnorm(x, self.a[f"{layer}.rms_att"], f["xn"])
            qv = self.rope(self.matmul(xn, f"{layer}.wq", f["xn"], f["q"], "q"), pos, "q")
            kv = self.rope(self.matmul(xn, f"{layer}.wk", f["xn"], f["k"], "k"), pos, "k")
            self.kcache[layer, pos] = kv
            self.vcache[layer, pos] = self.matmul(xn, f"{layer}.wv", f["xn"], f["v"], "v")
            att = self.attention(qv, layer, pos)
            x = self._sat16(x + self.matmul(att, f"{layer}.wo", f["v"], f["x"], "x"), "x")
            if trace is not None:
                trace.setdefault("attn_residual", []).append(x.copy())
            xn = self.rmsnorm(x, self.a[f"{layer}.rms_ffn"], f["xn"])
            h1 = self.matmul(xn, f"{layer}.w1", f["xn"], f["h1"], "h1")
            h3 = self.matmul(xn, f"{layer}.w3", f["xn"], f["h3"], "h3")
            x = self._sat16(x + self.matmul(self.silu_gate(h1, h3), f"{layer}.w2", f["g"], f["x"], "x"), "x")
            if trace is not None:
                trace.setdefault("ffn_residual", []).append(x.copy())
        xn = self.rmsnorm(x, self.a["rms_final"], f["xn"])
        return self.matmul(xn, "emb" if self.shared else "cls", f["xn"], self.fl, "logits", bits32=True)

    def generate(self, prompt: list[int], steps: int, traces: list | None = None) -> list[int]:
        """Greedy generation per the spec's decoding rule (argmax, lowest id on ties; BOS stops)."""
        self.reset()
        tokens = list(prompt)
        token = tokens[0]
        for pos in range(min(steps, self.context)):
            step: dict | None = {} if traces is not None else None
            logits = self.forward(token, pos, step)
            nxt = prompt[pos + 1] if pos < len(prompt) - 1 else int(np.argmax(logits))
            if traces is not None:
                traces.append({**step, "logits": logits, "token": nxt})
            if pos >= len(prompt) - 1:
                if nxt == 1:
                    break
                tokens.append(nxt)
            token = nxt
        return tokens

    def nll(self, tokens: list[int]) -> tuple[float, int]:
        """NLL of tokens[1:], with logits read as fixed point (evaluation only, not chip behavior)."""
        self.reset()
        total, count = 0.0, 0
        for pos in range(min(len(tokens), self.context) - 1):
            logits = self.forward(tokens[pos], pos).astype(np.float64) * 2.0**-self.fl
            mx = logits.max()
            total += mx + np.log(np.exp(logits - mx).sum()) - logits[tokens[pos + 1]]
            count += 1
        return total, count
