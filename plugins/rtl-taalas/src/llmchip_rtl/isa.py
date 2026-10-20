"""Instruction set and instruction-level simulator of the llmchip vector engine.

This file is self-contained (Python 3.10, NumPy) because it is shipped inside
every generated RTL bundle: the simulation plugin uses it in the EDA container
as the operation-level reference for locating divergences, and its scalar
functions are the reference for the RTL arithmetic unit tests.

Semantics follow the numeric spec operator by operator; `rtl-taalas` checks
on the host that this simulator reproduces the L2 model bit for bit.

Instruction word: opcode in bits [7:0], then 16 signed 32-bit fields F0..F15
(the RTL program ROM stores only the bits each field needs).
"""

from __future__ import annotations

import numpy as np

FIELDS = 16
WORD_BITS = 8 + 32 * FIELDS

OPS = {"END": 0, "EMB": 1, "MATVEC": 2, "RMS": 3, "ROPE": 4, "ATTN": 5, "SILU": 6, "ADD": 7, "TRACE": 8, "ARGMAX": 9}
OP_NAMES = {v: k for k, v in OPS.items()}
POINTS = {"attn_residual": 1, "ffn_residual": 2, "logits": 3, "op": 15}
FLAG_SAT32 = 1
FLAG_POS = 2
FLAG_DEBUG = 4  # TRACE executed only in debug mode
MODE_POINTS = 1  # step mode bit: copy the observation points of the numeric spec
MODE_DEBUG = 2  # step mode bit: also copy the output of every operation

# -- scalar functions (reference for the RTL arithmetic unit) -----------------


ROUND_MODES = ("half-up", "half-even", "truncate")  # the design parameter ROUND_MODE indexes this


def rsh(v, s: int, mode: str = "half-up"):
    """v / 2^s rounded per the numeric spec's rounding mode (arithmetic shifts):
    half-up (v + 2^(s-1)) >> s, half-even (ties to even), truncate v >> s; negative s shifts left."""
    if s < 0:
        return v << (-s)
    if s == 0:
        return v
    if mode == "half-up":
        return (v + (1 << (s - 1))) >> s
    if mode == "half-even":
        return (v + ((1 << (s - 1)) - 1) + ((v >> s) & 1)) >> s
    if mode == "truncate":
        return v >> s
    raise ValueError(f"unknown rounding mode {mode}")


def sat(v, bits: int, mode: str = "saturate"):
    """v narrowed to a signed `bits`-bit integer: clamped (saturate) or its low bits (wrap)."""
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if mode == "wrap":
        return ((v - lo) & ((1 << bits) - 1)) + lo
    return np.clip(v, lo, hi) if isinstance(v, np.ndarray) else min(max(v, lo), hi)


def modes(params: dict) -> dict:
    """Machine keyword arguments from the design parameters of an RTL bundle."""
    return {
        "rounding": ROUND_MODES[int(params.get("ROUND_MODE", 0))],
        "overflow": "wrap" if params.get("OVERFLOW_WRAP") else "saturate",
        "acc_bits": int(params["ACCB"]) if params.get("ACC_NARROW") else None,
    }


def rsqrt_index(v: int) -> tuple[int, int]:
    """(e2, t) of the rmsnorm normalization: e2 even, t = v / 2^(e2-6) in [64, 256)."""
    v = max(v, 1)
    k = v.bit_length() - 1
    e2 = k - (k % 2)
    t = v >> (e2 - 6) if e2 >= 6 else v << (6 - e2)
    return e2, t


def silu_index(h1, frac: int, clamp: int, offset: int):
    z = np.clip(h1, -clamp, clamp - 1) if isinstance(h1, np.ndarray) else min(max(h1, -clamp), clamp - 1)
    return (z >> (frac - 4) if frac >= 4 else z << (4 - frac)) + offset


def exp_index(d, cap: int, shift: int):
    return (np.minimum(d, cap) if isinstance(d, np.ndarray) else min(d, cap)) >> shift


# -- encoding -------------------------------------------------------------------


def encode(op: str, fields: list[int]) -> int:
    if len(fields) > FIELDS:
        raise ValueError("too many fields")
    word = OPS[op]
    for i, f in enumerate(fields):
        if not -(2**31) <= f < 2**31:
            raise ValueError(f"field {i} of {op} out of range: {f}")
        word |= (f & 0xFFFFFFFF) << (8 + 32 * i)
    return word


def decode(word: int) -> tuple[str, list[int]]:
    fields = []
    for i in range(FIELDS):
        f = (word >> (8 + 32 * i)) & 0xFFFFFFFF
        fields.append(f - (1 << 32) if f >= 1 << 31 else f)
    return OP_NAMES[word & 0xFF], fields


# -- machine ------------------------------------------------------------------


class Machine:
    """Executes a program on the ROM images. `wrom` is (words, lanes) of signed weights,
    `srom` (words, lanes) of scale mantissas, `crom` a vector of constants."""

    def __init__(
        self,
        program: list[int],
        wrom: np.ndarray,
        srom: np.ndarray,
        crom: np.ndarray,
        lanes: int,
        dmem_words: int,
        context: int,
        rounding: str = "half-up",
        overflow: str = "saturate",
        acc_bits: int | None = None,
    ):
        self.rounding, self.overflow, self.acc_bits = rounding, overflow, acc_bits
        self.program = [decode(w) for w in program]
        self.wrom, self.srom, self.crom = wrom.astype(np.int64), srom.astype(np.int64), crom.astype(np.int64)
        self.lanes = lanes
        self.context = context
        self.dmem = np.zeros(dmem_words, np.int64)

    def rows(
        self, wbase: int, sbase: int, inp: int, out: int, group: int, ngroups: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """Inverse of the storage mapping: the (out, in) weights and (out, ngroups) mantissas."""
        blocks = -(-out // self.lanes)
        w = (
            self.wrom[wbase : wbase + blocks * inp]
            .reshape(blocks, inp, self.lanes)
            .transpose(0, 2, 1)
            .reshape(blocks * self.lanes, inp)
        )
        m = (
            self.srom[sbase : sbase + blocks * ngroups]
            .reshape(blocks, ngroups, self.lanes)
            .transpose(0, 2, 1)
            .reshape(blocks * self.lanes, ngroups)
        )
        return w[:out], m[:out]

    def step(self, token: int, pos: int, traces: list | None = None, mode: int = MODE_POINTS) -> int:
        """Runs the program for one (token, position); returns the argmax token. TRACE
        copies an observation point into its trace buffer (data memory) when `mode` enables it."""
        if pos >= self.context:
            raise ValueError(f"position {pos} exceeds the context of {self.context}")
        d = self.dmem
        result = 0

        def rsh_(v, s):
            return rsh(v, s, self.rounding)

        def sat_(v, bits):
            return sat(v, bits, self.overflow)

        for index, (op, f) in enumerate(self.program):
            if op == "END":
                break
            if op == "EMB":
                dst, dim, wbase, sbase, group, ngroups, shift = f[:7]
                w, m = self.rows(wbase, sbase, dim, token + 1, group, ngroups)
                q, mm = w[token], m[token]
                d[dst : dst + dim] = sat_(rsh_(q * mm[np.arange(dim) // group], shift), 16)
            elif op == "MATVEC":
                x, dst, inp, out, wbase, sbase, group, ngroups, shift, flags, stride = f[:11]
                w, m = self.rows(wbase, sbase, inp, out, group, ngroups)
                xp = np.zeros(ngroups * group, np.int64)
                xp[:inp] = d[x : x + inp]
                wp = np.zeros((out, ngroups * group), np.int64)
                wp[:, :inp] = w
                acc = np.einsum("ogk,gk->og", wp.reshape(out, ngroups, group), xp.reshape(ngroups, group))
                terms = acc * m
                if self.acc_bits is None:
                    total = terms.sum(axis=1)
                else:
                    # A narrow accumulator adds the groups in index order, narrowing every sum.
                    total = np.zeros(out, np.int64)
                    for g in range(ngroups):
                        total = sat_(total + terms[:, g], self.acc_bits)
                y = sat_(rsh_(total, shift), 32 if flags & FLAG_SAT32 else 16)
                base = dst + (pos * stride if flags & FLAG_POS else 0)
                d[base : base + out] = y
            elif op == "RMS":
                x, dst, n, cbase, eps_lo, eps_hi, shift_base, lut = f[:8]
                v = int((d[x : x + n] * d[x : x + n]).sum()) // n + ((eps_hi << 32) | (eps_lo & 0xFFFFFFFF))
                e2, t = rsqrt_index(v)
                r = int(self.crom[lut + t - 64])
                d[dst : dst + n] = sat_(rsh_(d[x : x + n] * self.crom[cbase : cbase + n] * r, shift_base + e2 // 2), 16)
            elif op == "ROPE":
                addr, n, hs, cos, sin, flags, stride = f[:7]
                base = addr + (pos * stride if flags & FLAG_POS else 0)
                v = d[base : base + n].copy()
                j = (np.arange(0, n, 2) % hs) // 2
                c = self.crom[cos + pos * (hs // 2) + j]
                s = self.crom[sin + pos * (hs // 2) + j]
                a, b = v[0::2], v[1::2]
                d[base : base + n : 2] = sat_(rsh_(a * c - b * s, 14), 16)
                d[base + 1 : base + n : 2] = sat_(rsh_(a * s + b * c, 14), 16)
            elif op == "ATTN":
                q, kbase, vbase, dst, heads, kv_mul, hs, scale, shift, exp, scratch, kv_dim, cap, ishift = f[:14]
                for h in range(heads):
                    kvh = (h // kv_mul) * hs
                    keys = np.array(
                        [d[kbase + t * kv_dim + kvh : kbase + t * kv_dim + kvh + hs] for t in range(pos + 1)]
                    )
                    vals = np.array(
                        [d[vbase + t * kv_dim + kvh : vbase + t * kv_dim + kvh + hs] for t in range(pos + 1)]
                    )
                    s = sat_(rsh_((keys @ d[q + h * hs : q + (h + 1) * hs]) * scale, shift), 32)
                    e = self.crom[exp + exp_index(s.max() - s, cap, ishift)]
                    p = (e << 15) // e.sum()
                    d[scratch : scratch + pos + 1] = s
                    d[scratch + self.context : scratch + self.context + pos + 1] = p
                    d[dst + h * hs : dst + (h + 1) * hs] = sat_(rsh_(p @ vals, 15), 16)
            elif op == "SILU":
                h1, h3, dst, n, frac, sig, sh2, clamp, offset = f[:9]
                a = d[h1 : h1 + n]
                s = rsh_(a * self.crom[sig + silu_index(a, frac, clamp, offset)], 15)
                d[dst : dst + n] = sat_(rsh_(s * d[h3 : h3 + n], sh2), 16)
            elif op == "ADD":
                x, y, n = f[:3]
                d[x : x + n] = sat_(d[x : x + n] + d[y : y + n], 16)
            elif op == "TRACE":
                addr, n, point, layer, flags, stride, buffer = f[:7]
                if not mode & (MODE_DEBUG if flags & FLAG_DEBUG else MODE_POINTS):
                    continue
                base = addr + (pos * stride if flags & FLAG_POS else 0)
                d[buffer : buffer + n] = d[base : base + n]
                if traces is not None:
                    traces.append({"op": index, "point": point, "layer": layer, "values": d[base : base + n].copy()})
            elif op == "ARGMAX":
                addr, n = f[:2]
                result = int(np.argmax(d[addr : addr + n]))
            else:
                raise ValueError(f"unknown op {op}")
        return result
