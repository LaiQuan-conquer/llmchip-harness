"""Llama-family model intake and the L0 FP32 reference (requirements FR-MDL, §3.4.3).

- `load_llama2c` reads llama2.c checkpoints (legacy v0 format, as `run.c` does).
- `Tokenizer` is a port of `run.c`'s BPE tokenizer.
- `Model.forward` is a NumPy float32 port of `run.c`'s `forward`; the L0 reference is
  validated against the vendored, compiled `run.c` (tests/test_model.py).

The Model IR is an `.npz` file: a JSON `config` entry plus one float32 array per tensor,
matrices stored as (out, in), exactly as llama2.c lays them out.
"""

from __future__ import annotations

import io
import itertools
import json
import struct
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

IR_FORMAT = "llmchip-model-ir/1"
BOS, EOS = 1, 2


@dataclass(frozen=True)
class Config:
    dim: int
    hidden_dim: int
    n_layers: int
    n_heads: int
    n_kv_heads: int
    vocab_size: int
    seq_len: int
    shared_classifier: bool = True
    norm_eps: float = 1e-5
    rope_theta: float = 10000.0

    @property
    def head_size(self) -> int:
        return self.dim // self.n_heads

    @property
    def kv_dim(self) -> int:
        return self.n_kv_heads * self.head_size

    @property
    def kv_mul(self) -> int:
        return self.n_heads // self.n_kv_heads


LAYER_TENSORS = ("rms_att", "wq", "wk", "wv", "wo", "rms_ffn", "w1", "w2", "w3")


def load_llama2c(data: bytes) -> tuple[Config, dict[str, np.ndarray]]:
    """Parses a llama2.c legacy (v0) checkpoint: 7 int32 header fields, then float32 tensors."""
    dim, hidden, layers, heads, kv_heads, vocab, seq_len = struct.unpack_from("<7i", data, 0)
    shared = vocab > 0
    cfg = Config(dim, hidden, layers, heads, kv_heads, abs(vocab), seq_len, shared)
    floats = np.frombuffer(data, dtype="<f4", offset=28)
    pos = 0

    def take(*shape: int) -> np.ndarray:
        nonlocal pos
        n = int(np.prod(shape))
        if pos + n > floats.size:
            raise ValueError("checkpoint is truncated")
        out = floats[pos : pos + n].reshape(shape).astype(np.float32)
        pos += n
        return out

    L, d, h, kv = layers, dim, hidden, cfg.kv_dim
    w = {"token_embedding": take(cfg.vocab_size, d), "rms_att": take(L, d), "wq": take(L, d, d), "wk": take(L, kv, d)}
    w |= {"wv": take(L, kv, d), "wo": take(L, d, d), "rms_ffn": take(L, d), "w1": take(L, h, d), "w2": take(L, d, h)}
    w |= {"w3": take(L, h, d), "rms_final": take(d)}
    take(seq_len * cfg.head_size // 2)  # freq_cis_real, unused (as in run.c)
    take(seq_len * cfg.head_size // 2)  # freq_cis_imag
    w["wcls"] = w["token_embedding"] if shared else take(cfg.vocab_size, d)
    if pos != floats.size:
        raise ValueError(f"checkpoint has {floats.size - pos} unexpected trailing floats")
    return cfg, w


def save_ir(cfg: Config, weights: dict[str, np.ndarray]) -> bytes:
    buf = io.BytesIO()
    arrays = {k: v for k, v in weights.items() if not (k == "wcls" and cfg.shared_classifier)}
    np.savez(buf, config=np.array(json.dumps({"format": IR_FORMAT, **asdict(cfg)})), **arrays)
    return buf.getvalue()


def load_ir(path: str | Path) -> tuple[Config, dict[str, np.ndarray]]:
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["config"]))
        if meta.pop("format") != IR_FORMAT:
            raise ValueError("not a Model IR file")
        cfg = Config(**meta)
        w = {k: z[k] for k in z.files if k != "config"}
    if cfg.shared_classifier:
        w["wcls"] = w["token_embedding"]
    return cfg, w


def structure_report(cfg: Config, weights: dict[str, np.ndarray], license_: str | None) -> dict:
    counts = {k: int(v.size) for k, v in weights.items() if not (k == "wcls" and cfg.shared_classifier)}
    total = sum(counts.values())
    per_token = 2 * (total - counts["token_embedding"] + (counts["token_embedding"] if cfg.shared_classifier else 0))
    return {
        "family": "llama",
        "config": asdict(cfg) | {"head_size": cfg.head_size, "kv_dim": cfg.kv_dim},
        "operators": {
            "rmsnorm": 2 * cfg.n_layers + 1,
            "matmul": 7 * cfg.n_layers + 1,
            "rope": cfg.n_layers,
            "attention": cfg.n_layers,
            "swiglu": cfg.n_layers,
            "embedding_lookup": 1,
        },
        "parameters": {"total": total, "by_tensor": counts},
        "flopsPerToken": per_token,
        "license": license_ or "unknown",
    }


class Tokenizer:
    """Port of run.c's tokenizer (BPE by highest merge score, byte fallback, dummy prefix)."""

    def __init__(self, data: bytes, vocab_size: int) -> None:
        (self.max_token_length,) = struct.unpack_from("<i", data, 0)
        pos = 4
        self.vocab: list[bytes] = []
        self.scores: list[float] = []
        for _ in range(vocab_size):
            score, length = struct.unpack_from("<fi", data, pos)
            pos += 8
            self.vocab.append(data[pos : pos + length])
            self.scores.append(score)
            pos += length
        self.index = {piece: i for i, piece in enumerate(self.vocab)}

    def encode(self, text: str, bos: bool = True, eos: bool = False) -> list[int]:
        tokens: list[int] = [BOS] if bos else []
        # The dummy prefix takes part in the merges, as in run.c.
        ids: list[int] = [self.index[b" "]] if text else []
        for ch in text:
            piece = ch.encode("utf-8")
            if piece in self.index:
                ids.append(self.index[piece])
            else:
                ids.extend(b + 3 for b in piece)
        while True:
            best, best_id, best_at = -1e10, -1, -1
            for i in range(len(ids) - 1):
                merged = self.vocab[ids[i]] + self.vocab[ids[i + 1]]
                j = self.index.get(merged)
                if j is not None and self.scores[j] > best:
                    best, best_id, best_at = self.scores[j], j, i
            if best_at < 0:
                break
            ids[best_at : best_at + 2] = [best_id]
        tokens.extend(ids)
        if eos:
            tokens.append(EOS)
        return tokens

    def decode_piece(self, prev: int, token: int) -> bytes:
        piece = self.vocab[token]
        if prev == BOS and piece.startswith(b" "):
            piece = piece[1:]
        if len(piece) == 6 and piece.startswith(b"<0x") and piece.endswith(b">"):
            return bytes([int(piece[3:5], 16)])
        return piece

    def decode(self, tokens: list[int]) -> str:
        out = b""
        for prev, tok in itertools.pairwise(tokens):
            out += self.decode_piece(prev, tok)
        return out.decode("utf-8", errors="replace")


class Model:
    """NumPy float32 port of run.c's transformer forward pass."""

    def __init__(self, cfg: Config, weights: dict[str, np.ndarray]) -> None:
        self.cfg = cfg
        self.w = weights
        self.reset()

    def reset(self) -> None:
        c = self.cfg
        self.key_cache = np.zeros((c.n_layers, c.seq_len, c.kv_dim), np.float32)
        self.value_cache = np.zeros((c.n_layers, c.seq_len, c.kv_dim), np.float32)

    @staticmethod
    def rmsnorm(x: np.ndarray, weight: np.ndarray, eps: float) -> np.ndarray:
        ss = np.float32(np.dot(x, x) / np.float32(x.size) + np.float32(eps))
        return (weight * (np.float32(1.0) / np.sqrt(ss) * x)).astype(np.float32)

    def forward(self, token: int, pos: int, trace: dict | None = None, stats: dict | None = None) -> np.ndarray:
        """Logits for `token` at `pos`. `stats` collects the largest magnitude of each tensor kind (calibration)."""
        c, w = self.cfg, self.w

        def seen(kind: str, value: np.ndarray) -> None:
            if stats is not None:
                stats[kind] = max(stats.get(kind, 0.0), float(np.abs(value).max()))

        hs = c.head_size
        x = w["token_embedding"][token].copy()
        seen("x", x)
        head_dim = (np.arange(0, c.dim, 2) % hs).astype(np.float32)
        freq = np.float32(1.0) / np.power(np.float32(c.rope_theta), head_dim / np.float32(hs))
        val = np.float32(pos) * freq
        fcr, fci = np.cos(val).astype(np.float32), np.sin(val).astype(np.float32)
        for layer in range(c.n_layers):
            xb = self.rmsnorm(x, w["rms_att"][layer], c.norm_eps)
            seen("xn", xb)
            q = w["wq"][layer] @ xb
            k = w["wk"][layer] @ xb
            v = w["wv"][layer] @ xb
            seen("v", v)
            q0, q1 = q[0::2].copy(), q[1::2].copy()
            q[0::2], q[1::2] = q0 * fcr - q1 * fci, q0 * fci + q1 * fcr
            n = c.kv_dim // 2
            k0, k1 = k[0::2].copy(), k[1::2].copy()
            k[0::2], k[1::2] = k0 * fcr[:n] - k1 * fci[:n], k0 * fci[:n] + k1 * fcr[:n]
            seen("q", q)
            seen("k", k)
            self.key_cache[layer, pos] = k
            self.value_cache[layer, pos] = v
            out = np.zeros(c.dim, np.float32)
            for h in range(c.n_heads):
                kvh = (h // c.kv_mul) * hs
                qh = q[h * hs : (h + 1) * hs]
                keys = self.key_cache[layer, : pos + 1, kvh : kvh + hs]
                att = (keys @ qh) / np.sqrt(np.float32(hs))
                seen("score", att)
                att = np.exp(att - att.max())
                att = (att / att.sum()).astype(np.float32)
                out[h * hs : (h + 1) * hs] = att @ self.value_cache[layer, : pos + 1, kvh : kvh + hs]
            seen("att", out)
            x = (x + w["wo"][layer] @ out).astype(np.float32)
            seen("x", x)
            if trace is not None:
                trace.setdefault("attn_residual", []).append(x.copy())
            xb = self.rmsnorm(x, w["rms_ffn"][layer], c.norm_eps)
            seen("xn", xb)
            h1 = w["w1"][layer] @ xb
            h3 = w["w3"][layer] @ xb
            g = (h1 * (np.float32(1.0) / (np.float32(1.0) + np.exp(-h1))) * h3).astype(np.float32)
            seen("h1", h1)
            seen("h3", h3)
            seen("g", g)
            x = (x + w["w2"][layer] @ g).astype(np.float32)
            seen("x", x)
            if trace is not None:
                trace.setdefault("ffn_residual", []).append(x.copy())
        x = self.rmsnorm(x, w["rms_final"], c.norm_eps)
        seen("xn", x)
        logits = (w["wcls"] @ x).astype(np.float32)
        seen("logits", logits)
        return logits

    def generate(self, prompt_tokens: list[int], steps: int) -> list[int]:
        """Greedy generation as run.c with temperature 0: argmax, lowest id on ties, stops at BOS."""
        self.reset()
        tokens = list(prompt_tokens)
        token = tokens[0]
        for pos in range(min(steps, self.cfg.seq_len)):
            logits = self.forward(token, pos)
            if pos < len(prompt_tokens) - 1:
                nxt = prompt_tokens[pos + 1]
            else:
                nxt = int(np.argmax(logits))
                if nxt == BOS:
                    break
                tokens.append(nxt)
            token = nxt
        return tokens

    def nll(self, tokens: list[int]) -> tuple[float, int]:
        """Summed negative log-likelihood of tokens[1:] given the prefix (teacher forcing)."""
        self.reset()
        total, count = 0.0, 0
        for pos in range(min(len(tokens), self.cfg.seq_len) - 1):
            logits = self.forward(tokens[pos], pos).astype(np.float64)
            m = logits.max()
            lse = m + np.log(np.exp(logits - m).sum())
            total += lse - logits[tokens[pos + 1]]
            count += 1
        return total, count
