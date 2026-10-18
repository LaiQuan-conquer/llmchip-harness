"""Compiles a numeric spec into the vector engine's ROM images and microcode (requirements FR-RTL-01/02).

Storage mapping rule `lanes-interleaved/1` (reversible, FR-RTL-02):
- a matrix (out, in) is split into blocks of `lanes` output rows; word
  `wbase + b * in + i` of the weight ROM holds q[b * lanes + l, i] in lane l;
- its scale mantissas follow the same rule in the scale ROM, word
  `sbase + b * ngroups + g`, lane l holding m[b * lanes + l, g];
- missing rows of the last block are zero (weights) and one (mantissas);
- the constant ROM holds norm weights and the rsqrt, exp, sigmoid and RoPE
  tables as 32-bit words.
`unmap` restores the logical arrays from the ROM images; `generate` checks
that the round trip reproduces every array of the numeric weights file.
"""

from __future__ import annotations

import hashlib

import numpy as np

from .isa import FLAG_POS, FLAG_SAT32, POINTS, encode

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

    def __init__(self, spec: dict, arrays: dict[str, np.ndarray], lanes: int) -> None:
        if lanes < 1 or lanes & (lanes - 1):
            raise ValueError("lanes must be a power of two")
        self.spec, self.lanes = spec, lanes
        c = spec["model"]["config"]
        self.cfg = c
        dim, hidden, layers, vocab = c["dim"], c["hidden_dim"], c["n_layers"], c["vocab_size"]
        hs = dim // c["n_heads"]
        kv_dim = c["n_kv_heads"] * hs
        ctx = spec["context"]["maxTokens"]
        group = spec["weights"]["groupSize"]
        f = spec["activations"]["frac"]
        fn, fl = spec["normWeights"]["frac"], spec["logits"]["frac"]
        exps = spec["weights"]["exponents"]
        self.wbits = spec["weights"]["storageBits"]
        self.context = ctx

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
        for name in names:
            q, m = arrays[f"{name}.q"], arrays[f"{name}.m"]
            words, scales = map_matrix(q, m, lanes)
            self.matrices[name] = {
                "wbase": wptr,
                "sbase": sptr,
                "out": q.shape[0],
                "in": q.shape[1],
                "ngroups": m.shape[1],
                "words": len(words),
            }
            wrom.append(words)
            srom.append(scales)
            wptr += len(words)
            sptr += len(scales)
        self.wrom = np.concatenate(wrom)
        self.srom = np.concatenate(srom)

        # Constant ROM.
        crom: list[int] = []
        self.consts: dict[str, int] = {}

        def put(name: str, values: np.ndarray) -> int:
            self.consts[name] = len(crom)
            crom.extend(int(v) for v in values.ravel())
            return self.consts[name]

        for layer in range(layers):
            put(f"{layer}.rms_att", arrays[f"{layer}.rms_att"])
            put(f"{layer}.rms_ffn", arrays[f"{layer}.rms_ffn"])
        put("rms_final", arrays["rms_final"])
        for lut in ("rsqrt", "exp", "sigmoid", "rope_cos", "rope_sin"):
            put(f"lut.{lut}", arrays[f"lut.{lut}"])
        self.crom = np.array(crom, np.int64)

        # Data memory layout (words of 32 bits).
        layout: dict[str, int] = {}
        top = 0
        for name, size in (
            ("x", dim),
            ("xn", dim),
            ("q", dim),
            ("att", dim),
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
                    f_in + exps[name] - f_out,
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
                    "EMB", [layout["x"], dim, emb["wbase"], emb["sbase"], group, emb["ngroups"], exps["emb"] - f["x"]]
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
                        "ROPE", [layout["q"], dim, hs, self.consts["lut.rope_cos"], self.consts["lut.rope_sin"], 0, 0]
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
        self.program = [w for _, w in program]
        self.descriptions = [d for d, _ in program]
        # Debug program: after every computing op, trace its destination (point `op`, layer = op index).
        dests = self._destinations(program, layout, dim, hidden, vocab, kv_dim)
        debug: list[int] = []
        self.debug_map: list[int] = []  # debug trace number -> index of the op in the normal program
        for index, (_desc, word) in enumerate(program):
            debug.append(word)
            if index in dests:
                addr, n, stride = dests[index]
                debug.append(encode("TRACE", [addr, n, POINTS["op"], index, FLAG_POS if stride else 0, stride]))
                self.debug_map.append(index)
        self.debug_program = debug

    @staticmethod
    def _destinations(program, layout, dim, hidden, vocab, kv_dim) -> dict[int, tuple[int, int, int]]:
        out: dict[int, tuple[int, int, int]] = {}
        from .isa import FLAG_POS as POS
        from .isa import decode

        for index, (_, word) in enumerate(program):
            op, f = decode(word)
            if op == "EMB":
                out[index] = (f[0], f[1], 0)
            elif op == "MATVEC":
                out[index] = (f[1], f[3], f[10] if f[9] & POS else 0)
            elif op == "RMS":
                out[index] = (f[1], f[2], 0)
            elif op == "ROPE":
                out[index] = (f[0], f[1], f[6] if f[5] & POS else 0)
            elif op == "ATTN":
                out[index] = (f[3], f[4] * f[6], 0)
            elif op == "SILU":
                out[index] = (f[2], f[3], 0)
            elif op == "ADD":
                out[index] = (f[0], f[2], 0)
        return out

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
                "constants": {"words": len(self.crom), "wordBits": 32, "hash": rom_hash(self.crom)},
                "program": {"words": len(self.program), "wordBits": 8 + 32 * 16},
            },
            "matrices": self.matrices,
            "constants": self.consts,
            "dataMemory": {"words": self.dmem_words, "layout": self.layout},
        }
