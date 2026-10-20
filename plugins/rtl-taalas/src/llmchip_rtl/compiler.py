"""Compiles a numeric spec into the vector engine's ROM images and microcode (requirements FR-RTL-01/02).

Storage mapping rule `lanes-interleaved/1` (reversible, FR-RTL-02):
- a matrix (out, in) is split into blocks of `lanes` output rows; word
  `wbase + b * in + i` of the weight ROM holds q[b * lanes + l, i] in lane l;
- its scale mantissas follow the same rule in the scale ROM, word
  `sbase + b * ngroups + g`, lane l holding m[b * lanes + l, g];
- missing rows of the last block are zero (weights) and one (mantissas);
- the constant ROM holds norm weights and the rsqrt, exp, sigmoid and RoPE
  tables as signed words of the width they need.
`unmap` restores the logical arrays from the ROM images; `generate` checks
that the round trip reproduces every array of the numeric weights file.
"""

from __future__ import annotations

import hashlib

import numpy as np

from .isa import FLAG_DEBUG, FLAG_POS, FLAG_SAT32, POINTS, decode, encode

FLAG_VB = 4  # MATVEC: the input is already in the vector buffer (operand buffers only; see perf.py)
BUFFER_PARTS = ("vector", "q", "acc")
# The choices of the chip spec: none, the attention buffers (q and the output accumulators), all.
BUFFER_CHOICES = {"none": (), "attention": ("q", "acc"), "all": BUFFER_PARTS}


def buffer_parts(value) -> tuple[str, ...]:
    """The operand buffer parts of an option value: a choice name, true (all), false (none) or a list."""
    if isinstance(value, str):
        if value not in BUFFER_CHOICES:
            raise ValueError(f"unknown operand buffers {value}")
        return BUFFER_CHOICES[value]
    if value is True:
        return BUFFER_PARTS
    if not value:
        return ()
    unknown = set(value) - set(BUFFER_PARTS)
    if unknown:
        raise ValueError(f"unknown operand buffer parts {sorted(unknown)}")
    return tuple(p for p in BUFFER_PARTS if p in value)


MAPPING_RULE = "lanes-interleaved/1"
MATRICES = ("wq", "wk", "wv", "wo", "w1", "w3", "w2")


def map_matrix(q: np.ndarray, m: np.ndarray, lanes: int) -> tuple[np.ndarray, np.ndarray]:
    out, inp = q.shape
    blocks = -(-out // lanes)
    qp = np.zeros((blocks * lanes, inp), np.int64)
    qp[:out] = q
    mp = np.ones((blocks * lanes, m.shape[1]), np.int64)
    mp[:out] = m
    words = qp.reshape(blocks, lanes, inp).transpose(0, 2, 1).reshape(blocks * inp, lanes)
    scales = mp.reshape(blocks, lanes, -1).transpose(0, 2, 1).reshape(-1, lanes)
    return words, scales


def unmap_matrix(
    words: np.ndarray, scales: np.ndarray, out: int, inp: int, ngroups: int, lanes: int
) -> tuple[np.ndarray, np.ndarray]:
    blocks = -(-out // lanes)
    q = words[: blocks * inp].reshape(blocks, inp, lanes).transpose(0, 2, 1).reshape(blocks * lanes, inp)[:out]
    m = (
        scales[: blocks * ngroups]
        .reshape(blocks, ngroups, lanes)
        .transpose(0, 2, 1)
        .reshape(blocks * lanes, ngroups)[:out]
    )
    return q, m


def rom_hash(a: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(a, dtype="<i8").tobytes()).hexdigest()


class Compiled:
    """ROM images, programs and layout of one design."""

    def __init__(self, spec: dict, arrays: dict[str, np.ndarray], lanes: int, options: dict | None = None) -> None:
        """`options` are the microarchitecture choices of the chip spec (`dataflow`):
        `operandBuffers` (the on-chip buffer parts: "vector", "q", "acc"; true means all) and
        `memoryInterface` (`spi` or `sqi`); they change the cycles, never the results."""
        options = options or {}
        self.buffer_parts = buffer_parts(options.get("operandBuffers", False))
        self.operand_buffers = bool(self.buffer_parts)
        self.memory_interface = options.get("memoryInterface", "spi")
        if self.memory_interface not in ("spi", "sqi"):
            raise ValueError(f"unknown memory interface {self.memory_interface}")
        if lanes < 1 or lanes & (lanes - 1):
            raise ValueError("lanes must be a power of two")
        self.spec, self.lanes = spec, lanes
        c = spec["model"]["config"]
        self.cfg = c
        dim, hidden, layers, vocab = c["dim"], c["hidden_dim"], c["n_layers"], c["vocab_size"]
        # Head dim: dim / heads in the llama2.c structure; a model of other proportions (an
        # analysis of an imported model, `shape`) gives it, and q / att are heads x hs long.
        hs = c.get("head_size") or dim // c["n_heads"]
        qdim = c["n_heads"] * hs
        kv_dim = c["n_kv_heads"] * hs
        ctx = spec["context"]["maxTokens"]
        group = spec["weights"]["groupSize"]
        f = spec["activations"]["frac"]
        fn, fl = spec["normWeights"]["frac"], spec["logits"]["frac"]
        exps = spec["weights"]["exponents"]
        self.wbits = spec["weights"]["storageBits"]
        self.context = ctx
        # Rounding, overflow and accumulator width of the numeric spec (defaults: today's behavior).
        self.rounding = str(spec.get("rounding", {}).get("mode", "half-up")).removeprefix("round-")
        self.overflow = spec.get("overflow", {}).get("mode", "saturate")
        acc_spec = spec["accumulator"]
        self.acc_bits = acc_spec["bits"] if acc_spec.get("canOverflow") else None

        # Weight and scale ROMs.
        wrom: list[np.ndarray] = []
        srom: list[np.ndarray] = []
        self.matrices: dict[str, dict] = {}
        wptr = sptr = 0
        names = (
            ["emb"]
            + ([] if c["shared_classifier"] else ["cls"])
            + [f"{layer}.{m}" for layer in range(layers) for m in MATRICES]
        )
        shapes = {"emb": (vocab, dim), "cls": (vocab, dim), "wq": (qdim, dim), "wk": (kv_dim, dim)}
        shapes |= {
            "wv": (kv_dim, dim),
            "wo": (dim, qdim),
            "w1": (hidden, dim),
            "w3": (hidden, dim),
            "w2": (dim, hidden),
        }
        self.shape_only = arrays is None
        self.limits: list[str] = []  # bounds a shape analysis exceeds (`_limit`)
        for name in names:
            if self.shape_only:
                # Only the program is wanted (cycles do not depend on the ROM contents).
                out, inp = shapes[name.split(".")[-1]]
                ngroups = -(-inp // group)
                nwords, nscales = -(-out // lanes) * inp, -(-out // lanes) * ngroups
            else:
                q, m = arrays[f"{name}.q"], arrays[f"{name}.m"]
                words, scales = map_matrix(q, m, lanes)
                out, inp, ngroups, nwords, nscales = q.shape[0], q.shape[1], m.shape[1], len(words), len(scales)
                wrom.append(words)
                srom.append(scales)
            self.matrices[name] = {
                "wbase": wptr,
                "sbase": sptr,
                "out": out,
                "in": inp,
                "ngroups": ngroups,
                "words": nwords,
            }
            wptr += nwords
            sptr += nscales
        self.wrom = np.concatenate(wrom) if wrom else np.zeros((0, lanes), np.int64)
        self.srom = np.concatenate(srom) if srom else np.zeros((0, lanes), np.int64)

        # Constant ROM.
        crom: list[int] = []
        self.consts: dict[str, int] = {}

        def put(name: str, values: np.ndarray) -> int:
            self.consts[name] = len(crom)
            crom.extend(int(v) for v in values.ravel())
            return self.consts[name]

        sizes = {"rms_att": dim, "rms_ffn": dim, "rms_final": dim, "lut.rsqrt": 192, "lut.exp": 512}
        sizes |= {"lut.sigmoid": 256, "lut.rope_cos": ctx * (hs // 2), "lut.rope_sin": ctx * (hs // 2)}

        def array(name: str) -> np.ndarray:
            if self.shape_only:
                return np.zeros(sizes[name.split(".", 1)[1] if name[0].isdigit() else name], np.int64)
            return arrays[name]

        for layer in range(layers):
            put(f"{layer}.rms_att", array(f"{layer}.rms_att"))
            put(f"{layer}.rms_ffn", array(f"{layer}.rms_ffn"))
        put("rms_final", array("rms_final"))
        for lut in ("rsqrt", "exp", "sigmoid", "rope_cos", "rope_sin"):
            put(f"lut.{lut}", array(f"lut.{lut}"))
        self.crom = np.array(crom, np.int64)

        # Data memory layout (words of 32 bits).
        layout: dict[str, int] = {}
        top = 0
        for name, size in (
            ("x", dim),
            ("xn", dim),
            ("q", qdim),
            ("att", qdim),
            ("y", dim),
            ("h1", hidden),
            ("h3", hidden),
            ("g", hidden),
            ("logits", vocab),
            ("scratch", 2 * ctx),
            ("kcache", layers * ctx * kv_dim),
            ("vcache", layers * ctx * kv_dim),
        ):
            layout[name] = top
            top += size
        self.layout, self.dmem_words = layout, top

        eps = round(c["norm_eps"] * 2.0 ** (2 * f["x"]))
        att_scale = round(2**15 / np.sqrt(hs))
        score_frac = spec["scores"]["frac"]
        cap = 16 * 2**score_frac - 1

        def matvec(name: str, x: str, dst: int, f_in: int, f_out: int, sat32: bool = False, stride: int = 0) -> int:
            mtx = self.matrices[name]
            flags = (FLAG_SAT32 if sat32 else 0) | (FLAG_POS if stride else 0)
            return encode(
                "MATVEC",
                [
                    layout[x],
                    dst,
                    mtx["in"],
                    mtx["out"],
                    mtx["wbase"],
                    mtx["sbase"],
                    group,
                    mtx["ngroups"],
                    f_in + exps.get(name, 0) - f_out,
                    flags,
                    stride,
                ],
            )

        eps_lo = eps & 0xFFFFFFFF
        eps_lo -= (
            (1 << 32) if eps_lo >= 1 << 31 else 0
        )  # fields are signed 32-bit; the RTL reassembles the 64-bit value

        def rms(cname: str, n: int) -> int:
            return encode(
                "RMS",
                [
                    layout["x"],
                    layout["xn"],
                    n,
                    self.consts[cname],
                    eps_lo,
                    eps >> 32,
                    fn + 15 - f["xn"],
                    self.consts["lut.rsqrt"],
                ],
            )

        def trace(point: str, layer: int, addr: int, n: int, stride: int = 0) -> int:
            return encode("TRACE", [addr, n, POINTS[point], layer, FLAG_POS if stride else 0, stride])

        program: list[tuple[str, int]] = []  # (description, word)
        emb = self.matrices["emb"]
        program.append(
            (
                "embedding",
                encode(
                    "EMB",
                    [layout["x"], dim, emb["wbase"], emb["sbase"], group, emb["ngroups"], exps.get("emb", 0) - f["x"]],
                ),
            )
        )
        for layer in range(layers):
            kb = layout["kcache"] + layer * ctx * kv_dim
            vb = layout["vcache"] + layer * ctx * kv_dim
            program += [
                (f"layer {layer} rmsnorm (attention)", rms(f"{layer}.rms_att", dim)),
                (f"layer {layer} q", matvec(f"{layer}.wq", "xn", layout["q"], f["xn"], f["q"])),
                (f"layer {layer} k", matvec(f"{layer}.wk", "xn", kb, f["xn"], f["k"], stride=kv_dim)),
                (f"layer {layer} v", matvec(f"{layer}.wv", "xn", vb, f["xn"], f["v"], stride=kv_dim)),
                (
                    f"layer {layer} rope q",
                    encode(
                        "ROPE", [layout["q"], qdim, hs, self.consts["lut.rope_cos"], self.consts["lut.rope_sin"], 0, 0]
                    ),
                ),
                (
                    f"layer {layer} rope k",
                    encode(
                        "ROPE",
                        [kb, kv_dim, hs, self.consts["lut.rope_cos"], self.consts["lut.rope_sin"], FLAG_POS, kv_dim],
                    ),
                ),
                (
                    f"layer {layer} attention",
                    encode(
                        "ATTN",
                        [
                            layout["q"],
                            kb,
                            vb,
                            layout["att"],
                            c["n_heads"],
                            c["n_heads"] // c["n_kv_heads"],
                            hs,
                            att_scale,
                            f["q"] + f["k"] + 15 - score_frac,
                            self.consts["lut.exp"],
                            layout["scratch"],
                            kv_dim,
                            cap,
                            score_frac - 5,
                        ],
                    ),
                ),
                (f"layer {layer} wo", matvec(f"{layer}.wo", "att", layout["y"], f["v"], f["x"])),
                (f"layer {layer} residual (attention)", encode("ADD", [layout["x"], layout["y"], dim])),
                (f"layer {layer} trace attn_residual", trace("attn_residual", layer, layout["x"], dim)),
                (f"layer {layer} rmsnorm (ffn)", rms(f"{layer}.rms_ffn", dim)),
                (f"layer {layer} w1", matvec(f"{layer}.w1", "xn", layout["h1"], f["xn"], f["h1"])),
                (f"layer {layer} w3", matvec(f"{layer}.w3", "xn", layout["h3"], f["xn"], f["h3"])),
                (
                    f"layer {layer} silu gate",
                    encode(
                        "SILU",
                        [
                            layout["h1"],
                            layout["h3"],
                            layout["g"],
                            hidden,
                            f["h1"],
                            self.consts["lut.sigmoid"],
                            f["h1"] + f["h3"] - f["g"],
                            8 * 2 ** f["h1"],
                            128,
                        ],
                    ),
                ),
                (f"layer {layer} w2", matvec(f"{layer}.w2", "g", layout["y"], f["g"], f["x"])),
                (f"layer {layer} residual (ffn)", encode("ADD", [layout["x"], layout["y"], dim])),
                (f"layer {layer} trace ffn_residual", trace("ffn_residual", layer, layout["x"], dim)),
            ]
        program += [
            ("final rmsnorm", rms("rms_final", dim)),
            (
                "classifier",
                matvec("emb" if c["shared_classifier"] else "cls", "xn", layout["logits"], f["xn"], fl, sat32=True),
            ),
            ("trace logits", trace("logits", 0, layout["logits"], vocab)),
            ("argmax", encode("ARGMAX", [layout["logits"], vocab])),
            ("end", encode("END", [])),
        ]
        # One program: after every computing operation a debug trace (executed in debug mode
        # only) copies its output; the trace's layer field is the index of that operation.
        dests = self._destinations(program)
        final: list[tuple[str, str, list[int]]] = []
        for index, (desc, word) in enumerate(program):
            op, fields = decode(word)
            final.append((desc, op, fields))
            if index in dests:
                addr, n, stride = dests[index]
                flags = FLAG_DEBUG | (FLAG_POS if stride else 0)
                final.append((f"debug trace of {desc}", "TRACE", [addr, n, POINTS["op"], -1, flags, stride, 0]))
        # Resolve the layer field of debug traces (index of the traced op) and give every
        # TRACE its buffer in the data memory, after the working data.
        self.data_words = top
        self.trace_map: list[dict] = []
        for index, (_desc, op, fields) in enumerate(final):
            if op != "TRACE":
                continue
            if fields[4] & FLAG_DEBUG:
                fields[3] = index - 1
            fields[6] = top
            self.trace_map.append(
                {
                    "op": index,
                    "point": fields[2],
                    "layer": fields[3],
                    "n": fields[1],
                    "addr": top,
                    "debug": bool(fields[4] & FLAG_DEBUG),
                }
            )
            top += fields[1]
        self.dmem_words = top
        if "vector" in self.buffer_parts:
            self._mark_buffered_inputs(final)
        self.program = [encode(op, fields) for _, op, fields in final]
        self.descriptions = [desc for desc, _, _ in final]
        self.debug_map = [t["layer"] for t in self.trace_map if t["debug"]]
        self.widths = self._widths(final, arrays, group)

    def _limit(self, message: str) -> None:
        """A bound the engine does not meet: an error for a design, a finding for a shape analysis."""
        if not self.shape_only:
            raise ValueError(message)
        self.limits.append(message)

    @classmethod
    def for_shape(cls, shape: dict, numeric: dict, lanes: int, options: dict | None = None) -> Compiled:
        """The program of the engine for a model of the given shape (dim, hidden_dim, n_layers,
        n_heads, n_kv_heads, head_size, vocab_size, shared_classifier) and numeric choices
        (weight levels, group size, context), without ROM contents: the cycles of a model the
        engine does not implement as such (an analysis of an imported model)."""
        spec = {
            "model": {"config": shape},
            "context": {"maxTokens": numeric["context"]},
            "weights": {
                "groupSize": numeric["groupSize"],
                "storageBits": numeric["storageBits"],
                "levels": numeric["levels"],
                "exponents": {},
            },
            "activations": {"frac": dict.fromkeys(("x", "xn", "q", "k", "v", "h1", "h3", "g"), 8)},
            "normWeights": {"frac": 8},
            "logits": {"frac": 16},
            "scores": {"frac": 10},
            "accumulator": {"bits": 48, "canOverflow": False},
        }
        spec["model"]["config"] = {"norm_eps": 1e-5, "shared_classifier": True} | shape
        return cls(spec, None, lanes, options)

    @property
    def params(self) -> dict:
        """The design parameters (llmchip_params.vh): sizes, widths and options."""
        return {
            "LANES": self.lanes,
            "LANES_LOG2": self.lanes.bit_length() - 1,
            "WBITS": self.wbits,
            "CTX": self.context,
            "VOCAB": self.cfg["vocab_size"],
            **self.widths,
            "NPROG": len(self.program),
            "NDMEM": self.dmem_words,
        }

    @staticmethod
    def _mark_buffered_inputs(program: list[tuple[str, str, list[int]]]) -> None:
        """Sets FLAG_VB on every MATVEC whose input is already in the vector buffer: after an
        RMS (its output stays there) or a MATVEC with the same input, unless something wrote
        that vector in between. ATTN uses the buffer for q, so it leaves nothing usable."""
        held: tuple[int, int] | None = None  # (address, words) of the vector in the buffer

        def overlaps(span: tuple[int, int, int] | None, vec: tuple[int, int] | None) -> bool:
            if not span or not vec:
                return False
            addr, n, stride = span
            end = addr + n if not stride else 1 << 62  # a strided output may land anywhere above
            return addr < vec[0] + vec[1] and vec[0] < end

        for _desc, op, f in program:
            hit = op == "MATVEC" and held is not None and f[0] == held[0] and f[2] <= held[1]
            if hit:
                f[9] |= FLAG_VB
            written = Compiled._destinations([("", encode(op, f))]).get(0)
            if overlaps(written, held):
                held = None
            if op == "MATVEC" and not hit:
                held = None if overlaps(written, (f[0], f[2])) else (f[0], f[2])  # loaded by the first block
            elif op == "RMS":
                held = (f[1], f[2])
            elif op == "ATTN":
                held = None

    @staticmethod
    def _destinations(program) -> dict[int, tuple[int, int, int]]:
        """Output (address, count, position stride) of every computing operation."""
        out: dict[int, tuple[int, int, int]] = {}
        for index, (_, word) in enumerate(program):
            op, f = decode(word)
            if op == "EMB":
                out[index] = (f[0], f[1], 0)
            elif op == "MATVEC":
                out[index] = (f[1], f[3], f[10] if f[9] & FLAG_POS else 0)
            elif op == "RMS":
                out[index] = (f[1], f[2], 0)
            elif op == "ROPE":
                out[index] = (f[0], f[1], f[6] if f[5] & FLAG_POS else 0)
            elif op == "ATTN":
                out[index] = (f[3], f[4] * f[6], 0)
            elif op == "SILU":
                out[index] = (f[2], f[3], 0)
            elif op == "ADD":
                out[index] = (f[0], f[2], 0)
        return out

    def _widths(self, program: list[tuple[str, str, list[int]]], arrays: dict[str, np.ndarray], group: int) -> dict:
        """Register and memory widths of the RTL, from worst-case bounds of every value the
        operations produce (no value can overflow; FR-RTL-01). Raises if a bound is not met."""

        def sbits(v: int) -> int:
            return max(int(abs(int(v))).bit_length() + 1, 2)

        x16, _x32 = 2**15, 2**31
        if self.shape_only:  # worst cases of the formats instead of the contents
            qmax, mmax, cmax = int(self.spec["weights"]["levels"][1]), 32767, 2**15
        else:
            qmax = int(np.abs(self.wrom).max()) if self.wrom.size else 1
            mmax = int(self.srom.max()) if self.srom.size else 1
            cmax = int(np.abs(self.crom).max()) if self.crom.size else 1
        ctx = self.context
        acc = tot = 2
        dw = 2**34  # max - s of two 32-bit scores needs 34 bits
        b_operands = [x16, mmax, cmax, 2**15]
        for _, op, f in program:
            if op == "EMB":
                dw = max(dw, qmax * mmax * 2 ** max(-f[6], 0))
                b_operands.append(f[1])
                b_operands.append(f[5])
            elif op == "MATVEC":
                inp, shift = f[2], f[8]
                acc = max(acc, x16 * qmax * group)
                tot = max(tot, x16 * qmax * inp * mmax)
                dw = max(dw, x16 * qmax * group * mmax, x16 * qmax * inp * mmax * 2 ** max(-shift, 0))
                b_operands.append(f[10])
            elif op == "RMS":
                n = f[2]
                eps = (f[5] << 32) | (f[4] & 0xFFFFFFFF)
                dw = max(dw, n * x16 * x16 + eps, x16 * cmax * cmax * 2 ** max(-f[6], 0))
            elif op == "ROPE":
                dw = max(dw, 2 * x16 * cmax)
                b_operands += [f[6], f[2] // 2]
            elif op == "ATTN":
                hs, scale = f[6], f[7]
                dw = max(dw, hs * x16 * x16 * scale * 2 ** max(-f[8], 0), ctx * cmax, cmax * 2**15, x16 * x16)
                b_operands.append(scale)
                if ctx * cmax >= 2**31:
                    self._limit("the attention exp sum does not fit the 32-bit divisor")
            elif op == "SILU":
                dw = max(dw, x16 * cmax, (x16 * cmax // 2**15 + 1) * x16 * 2 ** max(-f[6], 0))
            elif op == "TRACE":
                b_operands.append(f[5])
        # Shifts of the shift-and-saturate unit (RSH_POSITIVE when all are right shifts),
        # SILU fraction bits (SILU_FRAC_GE4), and the RMS epsilon (added in 32 bits).
        shifts = [14, 15]
        silu_frac = []
        for _, op, f in program:
            if op in ("EMB", "SILU"):
                shifts.append(f[6])
            elif op in ("MATVEC", "ATTN"):
                shifts.append(f[8])
            elif op == "RMS":
                shifts.append(f[6])
                if f[5] != 0 or not 0 <= f[4] < 2**30:
                    raise ValueError("the RMS epsilon must be below 2^30")
            if op == "SILU":
                silu_frac.append(f[4])
                if not 0 <= f[4] <= 15:
                    raise ValueError("SILU fraction bits must be in 0..15")
            if op in ("EMB", "MATVEC", "RMS", "ATTN", "SILU") and not min(shifts) >= -128 and max(shifts) < 64:
                raise ValueError("a shift is outside the 8-bit shift field")
        if max(b_operands) >= 2**17:
            self._limit(f"a multiplier operand needs more than 18 bits: {max(b_operands)}")
        if sbits(cmax) > 18:
            self._limit("constants need more than 18 bits")
        widths = {
            "DW": sbits(dw) + 1,
            "ACCW": sbits(acc),
            "TOTW": sbits(tot),
            "CW": sbits(cmax),
            "AW": max(self.dmem_words - 1, 1).bit_length(),
            "PCW": max(len(self.program) - 1, 1).bit_length(),
            "WAW": max(len(self.wrom) - 1, 1).bit_length(),
            "SAW": max(len(self.srom) - 1, 1).bit_length(),
            "CAW": max(len(self.crom) - 1, 1).bit_length(),
        }
        widths["RSH_POSITIVE"] = int(min(shifts) >= 1)
        widths["SILU_FRAC_GE4"] = int(min(silu_frac, default=4) >= 4)
        # Numeric modes: rounding of every result shift, overflow of every narrowing, and a
        # MATVEC total narrower than its proven bound (narrowed after each group addition).
        widths["ROUND_MODE"] = ("half-up", "half-even", "truncate").index(self.rounding)
        widths["ROUND_HALF_EVEN"] = int(self.rounding == "half-even")
        widths["ROUND_TRUNCATE"] = int(self.rounding == "truncate")
        widths["OVERFLOW_WRAP"] = int(self.overflow == "wrap")
        narrow = self.acc_bits is not None and self.acc_bits < widths["TOTW"]
        widths["ACC_NARROW"] = int(narrow)
        widths["ACCB"] = self.acc_bits if narrow else widths["TOTW"]
        if narrow:
            widths["TOTW"] = self.acc_bits
        dims = [self.cfg[k] for k in ("dim", "hidden_dim", "vocab_size")] + [self.context, 2 * self.lanes]
        widths["IW"] = max(dims).bit_length() + 1
        if widths["AW"] > 22:
            self._limit("the data memory exceeds the 24-bit byte address of the external SRAM")
        widths["SRAM_BYTES"] = max(1024, 1 << (4 * self.dmem_words - 1).bit_length())
        # Microarchitecture options: the vector buffer holds the longest vector an operation
        # reads through it (MATVEC inputs, RMS vectors, a head of q); one accumulator per head dim.
        parts = self.buffer_parts
        widths["OPBUF_VEC"] = int("vector" in parts)
        widths["OPBUF_Q"] = int("q" in parts)
        widths["OPBUF_ACC"] = int("acc" in parts)
        widths["MEM_SQI"] = int(self.memory_interface == "sqi")
        heads = [f[6] for _, op, f in program if op == "ATTN"]
        if "vector" in parts:
            widths["VBN"] = max(f[2] for _, op, f in program if op in ("MATVEC", "RMS"))
        if "q" in parts or "acc" in parts:
            # The rotating registers return to their start after exactly hs uses.
            if len(set(heads)) != 1:
                raise ValueError("the attention buffers need one head size")
            widths["HSN"] = heads[0]
        if "acc" in parts:
            # Attention output accumulators: sum over the context of p (at most 2^15) times v (int16).
            widths["OACCW"] = sbits(self.context * 2**15 * 2**15)
        return widths

    def unmap(self) -> dict[str, np.ndarray]:
        """The logical arrays, restored from the ROM images (inverse mapping)."""
        out: dict[str, np.ndarray] = {}
        for name, mtx in self.matrices.items():
            q, m = unmap_matrix(
                self.wrom[mtx["wbase"] :], self.srom[mtx["sbase"] :], mtx["out"], mtx["in"], mtx["ngroups"], self.lanes
            )
            out[f"{name}.q"], out[f"{name}.m"] = q, m
        names = sorted(self.consts, key=self.consts.get)
        for i, name in enumerate(names):
            end = self.consts[names[i + 1]] if i + 1 < len(names) else len(self.crom)
            out[name] = self.crom[self.consts[name] : end]
        for name in ("lut.rope_cos", "lut.rope_sin"):
            out[name] = out[name].reshape(self.context, -1)
        return out

    def storage_map(self, weights_hash: str) -> dict:
        return {
            "format": "llmchip-storage-map/1",
            "rule": MAPPING_RULE,
            "sourceWeights": weights_hash,
            "lanes": self.lanes,
            "weightBits": self.wbits,
            "roms": {
                "weights": {"words": len(self.wrom), "wordBits": self.lanes * self.wbits, "hash": rom_hash(self.wrom)},
                "scales": {"words": len(self.srom), "wordBits": self.lanes * 15, "hash": rom_hash(self.srom)},
                "constants": {"words": len(self.crom), "wordBits": self.widths["CW"], "hash": rom_hash(self.crom)},
                "program": {"words": len(self.program)},
            },
            "matrices": self.matrices,
            "constants": self.consts,
            "dataMemory": {"words": self.dmem_words, "layout": self.layout, "traceBuffers": self.trace_map},
        }
