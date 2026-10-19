"""Weight file formats: header-only readers and a lazy tensor loader.

Headers are read without touching tensor data, so a census of a 15 GB file
takes milliseconds. Supported:

- safetensors, single file or sharded (`*.safetensors` + `model.safetensors.index.json`);
- llama2.c legacy (v0) checkpoints, whose layout follows from a 7-int header;
- GGUF (header and tensor table only: quantized data is not decoded).

Every tensor is described as a `TensorInfo` with its byte range in its file, so
the census can check that the header accounts for every byte.
"""

from __future__ import annotations

import json
import struct
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

# safetensors dtype -> bits per element
ST_BITS = {
    "F64": 64, "F32": 32, "F16": 16, "BF16": 16, "I64": 64, "I32": 32, "I16": 16, "I8": 8,
    "U64": 64, "U32": 32, "U16": 16, "U8": 8, "BOOL": 8, "F8_E4M3": 8, "F8_E5M2": 8,
}  # fmt: skip

# ggml type -> (name, average bits per weight including block scales)
GGML_TYPES = {
    0: ("F32", 32.0), 1: ("F16", 16.0), 2: ("Q4_0", 4.5), 3: ("Q4_1", 5.0), 6: ("Q5_0", 5.5), 7: ("Q5_1", 6.0),
    8: ("Q8_0", 8.5), 9: ("Q8_1", 9.0), 10: ("Q2_K", 2.5625), 11: ("Q3_K", 3.4375), 12: ("Q4_K", 4.5),
    13: ("Q5_K", 5.5), 14: ("Q6_K", 6.5625), 15: ("Q8_K", 8.5), 16: ("IQ2_XXS", 2.0625), 17: ("IQ2_XS", 2.3125),
    18: ("IQ3_XXS", 3.0625), 19: ("IQ1_S", 1.5625), 20: ("IQ4_NL", 4.5), 21: ("IQ3_S", 3.4375), 22: ("IQ2_S", 2.5),
    23: ("IQ4_XS", 4.25), 24: ("I8", 8.0), 25: ("I16", 16.0), 26: ("I32", 32.0), 27: ("I64", 64.0),
    28: ("F64", 64.0), 29: ("IQ1_M", 1.75), 30: ("BF16", 16.0),
}  # fmt: skip


@dataclass
class TensorInfo:
    name: str
    dtype: str
    shape: list[int]
    file: str
    #: absolute byte offset of the tensor data in its file
    offset: int
    nbytes: int
    bits: float

    @property
    def params(self) -> int:
        return int(np.prod(self.shape)) if self.shape else 1

    def to_json(self) -> dict:
        return asdict(self) | {"params": self.params}


@dataclass
class FileLayout:
    """What a weight file's header says about its bytes."""

    name: str
    format: str
    size: int
    #: first byte of tensor data (after the header)
    data_start: int
    tensors: list[TensorInfo]
    metadata: dict


def read_safetensors(path: Path) -> FileLayout:
    with path.open("rb") as f:
        raw = f.read(8)
        if len(raw) < 8:
            raise ValueError(f"{path.name}: too small for safetensors")
        (n,) = struct.unpack("<Q", raw)
        if n <= 0 or n > 200 << 20:
            raise ValueError(f"{path.name}: implausible safetensors header length {n}")
        header = json.loads(f.read(n))
    meta = header.pop("__metadata__", None) or {}
    start = 8 + n
    tensors = []
    for name, info in header.items():
        begin, end = info["data_offsets"]
        dtype = info["dtype"]
        tensors.append(
            TensorInfo(
                name, dtype, list(info["shape"]), path.name, start + begin, end - begin, float(ST_BITS.get(dtype, 0))
            )
        )
    return FileLayout(path.name, "safetensors", path.stat().st_size, start, tensors, meta)


LLAMA2C_LAYER = (
    ("attention_norm", lambda c: [c["dim"]]),
    ("attention.wq", lambda c: [c["dim"], c["dim"]]),
    ("attention.wk", lambda c: [c["kv_dim"], c["dim"]]),
    ("attention.wv", lambda c: [c["kv_dim"], c["dim"]]),
    ("attention.wo", lambda c: [c["dim"], c["dim"]]),
    ("ffn_norm", lambda c: [c["dim"]]),
    ("feed_forward.w1", lambda c: [c["hidden_dim"], c["dim"]]),
    ("feed_forward.w2", lambda c: [c["dim"], c["hidden_dim"]]),
    ("feed_forward.w3", lambda c: [c["hidden_dim"], c["dim"]]),
)


def llama2c_config(data: bytes) -> dict:
    dim, hidden, layers, heads, kv_heads, vocab, seq_len = struct.unpack_from("<7i", data, 0)
    return {
        "dim": dim, "hidden_dim": hidden, "n_layers": layers, "n_heads": heads, "n_kv_heads": kv_heads,
        "vocab_size": abs(vocab), "seq_len": seq_len, "shared_classifier": vocab > 0,
        "kv_dim": kv_heads * (dim // heads), "head_size": dim // heads,
    }  # fmt: skip


def read_llama2c(path: Path) -> FileLayout:
    """The tensor layout of a llama2.c v0 checkpoint, named after Meta's Llama checkpoints.

    llama2.c stores each kind of layer tensor for all layers contiguously, in the
    order of `run.c`'s `memory_map_weights`; each layer's slice is listed separately.
    """
    with path.open("rb") as f:
        c = llama2c_config(f.read(28))
    if min(c["dim"], c["hidden_dim"], c["n_layers"], c["n_heads"], c["n_kv_heads"], c["vocab_size"], c["seq_len"]) <= 0:
        raise ValueError(f"{path.name}: not a llama2.c checkpoint")
    pos = 28
    tensors: list[TensorInfo] = []

    def add(name: str, shape: list[int]) -> None:
        nonlocal pos
        n = int(np.prod(shape)) * 4
        tensors.append(TensorInfo(name, "F32", shape, path.name, pos, n, 32.0))
        pos += n

    add("tok_embeddings.weight", [c["vocab_size"], c["dim"]])
    for suffix, shape in LLAMA2C_LAYER:
        for i in range(c["n_layers"]):
            add(f"layers.{i}.{suffix}.weight", shape(c))
    add("norm.weight", [c["dim"]])
    half = c["seq_len"] * c["head_size"] // 2
    add("freq_cis_real", [c["seq_len"], c["head_size"] // 2])
    add("freq_cis_imag", [c["seq_len"], c["head_size"] // 2])
    assert half * 4 == tensors[-1].nbytes
    if not c["shared_classifier"]:
        add("output.weight", [c["vocab_size"], c["dim"]])
    return FileLayout(path.name, "llama2c", path.stat().st_size, 28, tensors, {"llama2c": c})


class _Reader:
    def __init__(self, f) -> None:
        self.f = f

    def u32(self) -> int:
        return struct.unpack("<I", self.f.read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.f.read(8))[0]

    def string(self) -> str:
        n = self.u64()
        if n > 64 << 20:
            raise ValueError("GGUF string too long")
        return self.f.read(n).decode("utf-8", "replace")

    def value(self, vtype: int):
        simple = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
        if vtype == 8:
            return self.string()
        if vtype == 9:
            itype, count = self.u32(), self.u64()
            if count > 1 << 26:
                raise ValueError("GGUF array too long")
            items = [self.value(itype) for _ in range(count)]
            # Token lists are huge and say nothing about the structure.
            return items if count <= 64 else {"length": count}
        fmt = simple[vtype]
        return struct.unpack(fmt, self.f.read(struct.calcsize(fmt)))[0]


def read_gguf(path: Path) -> FileLayout:
    with path.open("rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError(f"{path.name}: not GGUF")
        r = _Reader(f)
        version, n_tensors, n_kv = r.u32(), r.u64(), r.u64()
        meta: dict = {"gguf.version": version}
        for _ in range(n_kv):
            key = r.string()
            meta[key] = r.value(r.u32())
        infos = []
        for _ in range(n_tensors):
            name = r.string()
            dims = [r.u64() for _ in range(r.u32())]
            infos.append((name, dims, r.u32(), r.u64()))
        align = int(meta.get("general.alignment", 32))
        start = (f.tell() + align - 1) // align * align
    tensors = []
    for name, dims, gtype, off in infos:
        tname, bits = GGML_TYPES.get(gtype, (f"ggml:{gtype}", 0.0))
        # GGUF lists dimensions fastest-first; report them in the usual (out, in) order.
        shape = list(reversed(dims))
        nbytes = round(int(np.prod(dims)) * bits / 8) if bits else 0
        tensors.append(TensorInfo(name, tname, shape, path.name, start + off, nbytes, bits))
    return FileLayout(path.name, "gguf", path.stat().st_size, start, tensors, meta)


def is_llama2c_checkpoint(path: Path) -> bool:
    """A .bin file whose size is exactly what its 7-int llama2.c header implies."""
    try:
        layout = read_llama2c(path)
    except (ValueError, struct.error, OverflowError):
        return False
    return sum(t.nbytes for t in layout.tensors) + 28 == layout.size


def read_layout(path: Path) -> FileLayout:
    if path.suffix == ".safetensors":
        return read_safetensors(path)
    if path.suffix == ".gguf":
        return read_gguf(path)
    if path.suffix == ".bin":
        return read_llama2c(path)
    raise ValueError(f"{path.name}: unsupported weight format")


def bf16_to_f32(raw: np.ndarray) -> np.ndarray:
    return (raw.astype(np.uint32) << 16).view(np.float32)


class WeightLoader:
    """Loads tensors as float32 on demand from safetensors or llama2.c files (memory-mapped)."""

    def __init__(self, layouts: list[FileLayout], paths: dict[str, Path]) -> None:
        self.index = {t.name: t for layout in layouts for t in layout.tensors}
        self.paths = paths
        self._maps: dict[str, np.memmap] = {}
        self._cache: dict[str, np.ndarray] = {}

    def __contains__(self, name: str) -> bool:
        return name in self.index

    def get(self, name: str) -> np.ndarray:
        if name in self._cache:
            return self._cache[name]
        arr = self.peek(name)
        self._cache[name] = arr
        return arr

    def peek(self, name: str) -> np.ndarray:
        """The tensor as float32, without keeping it."""
        if name in self._cache:
            return self._cache[name]
        t = self.index[name]
        m = self._maps.get(t.file)
        if m is None:
            m = self._maps[t.file] = np.memmap(self.paths[t.file], dtype=np.uint8, mode="r")
        if t.offset + t.nbytes > m.size:
            raise ValueError(
                f"{t.file} is truncated: tensor {name} needs bytes up to {t.offset + t.nbytes}, the file has {m.size}"
            )
        raw = m[t.offset : t.offset + t.nbytes]
        if t.dtype == "F32":
            arr = raw.view("<f4").astype(np.float32)
        elif t.dtype == "F16":
            arr = raw.view("<f2").astype(np.float32)
        elif t.dtype == "BF16":
            arr = bf16_to_f32(raw.view("<u2"))
        else:
            raise ValueError(f"tensor {name}: dtype {t.dtype} cannot be loaded as float32")
        return arr.reshape(t.shape)

    def release(self) -> None:
        self._cache.clear()
        self._maps.clear()
