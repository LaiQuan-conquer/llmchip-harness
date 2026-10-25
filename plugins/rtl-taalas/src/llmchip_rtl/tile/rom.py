"""The weight-to-ROM mapping of `taalas-tile/1` (requirements FR-RTL-02, FR-SGN-07).

A ROM word holds `macs` consecutive weights of one output row - one row's weights for a chunk of
`macs` input columns - so a matrix of (rows, columns) takes `rows * ceil(columns/macs)` words, and
a phase of the schedule walks them row by row. The products of one word are the row's partial dot
product over that column chunk; the engine sums them with a tree of eight groups of `macs/8`
(MACS/8) and accumulates the chunk sums per row (drafts/tile-180nm-pipeline.md, row-major layout).
A word can be wider than 64 bits (128 MACs of W4 are 512 bits), so words are Python integers, not
NumPy ones. The mapping is versioned and reversible: `to_words` packs the quantized levels of a
matrix into words, `from_words` reads them back, and `roundtrip` proves that forward then inverse
equals the original levels. The levels are two's-complement integers of the spec's weight format
(4 bits for W4); the scale mantissas stay in the engine's registers, as in the numeric spec's
arithmetic.
"""

from __future__ import annotations

import hashlib
import math

import numpy as np

MAPPING_VERSION = "taalas-tile-rom/2"


def chunks_per_row(columns: int, macs: int) -> int:
    return math.ceil(columns / macs)


def words_per_matrix(rows: int, columns: int, macs: int) -> int:
    return rows * chunks_per_row(columns, macs)


def to_words(q: np.ndarray, macs: int, bits: int) -> list[int]:
    """The ROM words of a quantized matrix (rows, columns), `macs * bits` bits each: row major."""
    rows, columns = q.shape
    chunks = chunks_per_row(columns, macs)
    mask = (1 << bits) - 1
    words: list[int] = []
    for row in range(rows):
        for chunk in range(chunks):
            word = 0
            for k in range(macs):
                column = chunk * macs + k
                level = int(q[row, column]) & mask if column < columns else 0
                word |= level << (k * bits)
            words.append(word)
    return words


def from_words(words: list[int], rows: int, columns: int, macs: int, bits: int) -> np.ndarray:
    """The quantized matrix restored from `words` (the inverse of `to_words`)."""
    chunks = chunks_per_row(columns, macs)
    mask = (1 << bits) - 1
    sign = 1 << (bits - 1)
    out = np.zeros((rows, chunks * macs), dtype=np.int64)
    for row in range(rows):
        for chunk in range(chunks):
            word = int(words[row * chunks + chunk])
            for k in range(macs):
                value = (word >> (k * bits)) & mask
                out[row, chunk * macs + k] = value - (1 << bits) if value >= sign else value
    return out[:, :columns]


def roundtrip(q: np.ndarray, macs: int, bits: int) -> bool:
    return np.array_equal(from_words(to_words(q, macs, bits), *q.shape, macs, bits), q)


def hexfile(words: list[int], macs: int, bits: int) -> list[str]:
    """The `$readmemh` lines of the words, `macs * bits` bits each (two's complement)."""
    width = -(-macs * bits // 4)  # nibbles per word, a whole number
    return [f"{word:0{width}x}" for word in words]


def digest(words: list[int], macs: int, bits: int) -> str:
    size = -(-macs * bits // 8)
    h = hashlib.sha256()
    for word in words:
        h.update(int(word).to_bytes(size, "little"))
    return h.hexdigest()
