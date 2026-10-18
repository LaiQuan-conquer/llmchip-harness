"""Proxy models: llama2.c checkpoints with fixed-seed, untrained weights and a
character tokenizer.

A proxy has the structure of a planned model (Llama, the same tensor layout)
but no training, so its outputs carry no meaning. It lets the flow run
end to end on the real size constraints before the trained weights exist; the
trained model then replaces it from S1 on. Results on a proxy must be labelled
as such (it is not evidence of accuracy).
"""

from __future__ import annotations

import struct

import numpy as np

from . import Config

# <unk>, BOS and EOS as in llama2.c, then one token per character.
SPECIALS = (b"<unk>", b"\n<s>\n", b"\n</s>\n")
DEFAULT_CHARS = " abcdefghijklmnopqrstuvwxyz.,'!?"


def char_tokenizer(chars: str = DEFAULT_CHARS) -> bytes:
    """A llama2.c tokenizer file with one token per character and no merges."""
    if " " not in chars or len(set(chars)) != len(chars):
        raise ValueError("the character set must contain the space (dummy prefix) and no duplicates")
    pieces = [*SPECIALS, *(c.encode("utf-8") for c in chars)]
    out = bytearray(struct.pack("<i", max(len(p) for p in pieces)))
    for piece in pieces:
        out += struct.pack("<fi", 0.0, len(piece)) + piece
    return bytes(out)


def random_checkpoint(cfg: Config, seed: int) -> bytes:
    """A llama2.c (v0) checkpoint with weights drawn from N(0, 1/fan_in) and unit norms."""
    rng = np.random.default_rng(seed)
    d, h, n, kv = cfg.dim, cfg.hidden_dim, cfg.n_layers, cfg.kv_dim

    def matrix(*shape: int) -> np.ndarray:
        return rng.normal(0.0, 1.0 / np.sqrt(shape[-1]), size=shape).astype("<f4")

    embedding = matrix(cfg.vocab_size, d)
    # Special tokens get small embedding rows: with the shared classifier their logits stay
    # small, so an untrained proxy does not end every generation at once by emitting BOS.
    embedding[: len(SPECIALS)] *= 0.05
    tensors = [
        embedding,
        np.ones((n, d), "<f4"),
        matrix(n, d, d),
        matrix(n, kv, d),
        matrix(n, kv, d),
        matrix(n, d, d),
        np.ones((n, d), "<f4"),
        matrix(n, h, d),
        matrix(n, d, h),
        matrix(n, h, d),
        np.ones(d, "<f4"),
        np.zeros(cfg.seq_len * cfg.head_size // 2, "<f4"),  # freq_cis_real, unused by run.c
        np.zeros(cfg.seq_len * cfg.head_size // 2, "<f4"),  # freq_cis_imag
    ]
    header = struct.pack("<7i", d, h, n, cfg.n_heads, cfg.n_kv_heads, cfg.vocab_size, cfg.seq_len)
    return header + b"".join(t.tobytes() for t in tensors)
