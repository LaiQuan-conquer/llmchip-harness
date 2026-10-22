"""`chip.json`: what a virtual chip declares about itself, and its validation.

The harness assumes nothing about the model or the chip beyond this manifest: the model's
context and vocabulary, the tokenizer, the backend that executes it, the clock and the
host interface come from here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .interface import CAPABILITIES, ChipMeta

FORMAT = "llmchip-vchip/1"
TOKENIZER_KINDS = ("llama2c", "hf")
HOST_KINDS = ("direct", "spi")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# The Taalas-like host frames (SPI mode 0, MSB first): START = cmd + token[16] + pos[16] +
# mode[8]; STATUS = cmd + busy, error, done, 13'b0, token[16]; READ = cmd + addr[16], then
# 32 bits per observation word.
SPI_DEFAULTS = {
    "sck_div": 8,
    "start_bits": 48,
    "status_bits": 40,
    "read_header_bits": 24,
    "read_word_bits": 32,
    "token_bits": 16,
    "pos_bits": 16,
}


class ManifestError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Manifest:
    dir: Path
    data: dict
    problems: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.data.get("id", self.dir.name))

    def path(self, rel: str) -> Path:
        p = Path(rel).expanduser()
        return p if p.is_absolute() else self.dir / p

    def artifact(self, name: str) -> Path:
        rel = self.data.get("artifacts", {}).get(name)
        if rel is None:
            raise KeyError(f"chip {self.id} declares no artifact `{name}`")
        return self.path(rel)

    def meta(self) -> ChipMeta:
        d = self.data
        return ChipMeta(
            id=self.id,
            backend=d["backend"],
            model=dict(d["model"]),
            tokenizer=dict(d["tokenizer"]),
            clock_mhz=d.get("clock_mhz"),
            capabilities=list(d.get("capabilities", ["greedy"])),
            host=dict(d["host"]),
            artifacts=dict(d.get("artifacts", {})),
        )

    def policy(self) -> dict:
        """The chip's acceptance policy: `policy` in chip.json, else policy.json beside it, else none."""
        if "policy" in self.data:
            return dict(self.data["policy"])
        p = self.dir / "policy.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def hashes(self) -> dict[str, str]:
        """SHA-256 of the manifest, the tokenizer and every artifact (directories hashed file by file)."""
        out = {
            "manifest": file_hash(self.dir / "chip.json"),
            "tokenizer": file_hash(self.path(self.data["tokenizer"]["path"])),
        }
        for name, rel in self.data.get("artifacts", {}).items():
            out[f"artifact.{name}"] = path_hash(self.path(rel))
        policy = self.dir / "policy.json"
        if "policy" not in self.data and policy.exists():
            out["policy"] = file_hash(policy)
        return out


_hash_cache: dict[tuple[str, int, int], str] = {}


def file_hash(path: Path) -> str:
    st = path.stat()
    key = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    if key not in _hash_cache:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        _hash_cache[key] = h.hexdigest()
    return _hash_cache[key]


def path_hash(path: Path) -> str:
    if path.is_file():
        return file_hash(path)
    h = hashlib.sha256()
    for p in sorted(q for q in path.rglob("*") if q.is_file()):
        h.update(f"{p.relative_to(path).as_posix()}\0{file_hash(p)}\n".encode())
    return h.hexdigest()


def _normalize(d: dict) -> None:
    """Accepted shorthands: `tokenizer` as a path, `artifact` as the single artifact `main`."""
    tok = d.get("tokenizer")
    if isinstance(tok, str):
        d["tokenizer"] = {"path": tok, "kind": "hf" if tok.endswith(".json") else "llama2c"}
    if "artifact" in d and "artifacts" not in d:
        d["artifacts"] = {"main": d.pop("artifact")}
    host = d.get("host", "direct")
    if isinstance(host, str):
        host = {"kind": host}
    if isinstance(host, dict) and host.get("kind") == "spi":
        host = {**SPI_DEFAULTS, "read_words": 0, **host}
    d["host"] = host


def _positive_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def validate(m: Manifest, backends: dict[str, str] | None = None) -> list[str]:
    """Problems with the manifest; empty when it is valid. `backends` are the known backend names."""
    d, problems = m.data, []
    if d.get("format") != FORMAT:
        problems.append(f"format must be {FORMAT}")
    if not isinstance(d.get("id"), str) or not ID_PATTERN.match(d["id"]):
        problems.append("id must be lowercase letters, digits, '.', '_' or '-'")
    backend = d.get("backend")
    if not isinstance(backend, str):
        problems.append("backend is missing")
    elif backends is not None and backend not in backends and "entrypoint" not in d:
        problems.append(f"unknown backend {backend} (known: {', '.join(sorted(backends))})")
    model = d.get("model")
    if not isinstance(model, dict):
        problems.append("model is missing")
    else:
        for key in ("context", "vocab"):
            if not _positive_int(model.get(key)):
                problems.append(f"model.{key} must be a positive integer")
        if not isinstance(model.get("family"), str):
            problems.append("model.family is missing")
    tok = d.get("tokenizer")
    if not isinstance(tok, dict) or not isinstance(tok.get("path"), str):
        problems.append("tokenizer.path is missing")
    else:
        if tok.get("kind") not in TOKENIZER_KINDS:
            problems.append(f"tokenizer.kind must be one of {', '.join(TOKENIZER_KINDS)}")
        if not m.path(tok["path"]).is_file():
            problems.append(f"tokenizer file {tok['path']} does not exist")
        stop = tok.get("stop", [])
        if not isinstance(stop, list) or not all(isinstance(t, int) and t >= 0 for t in stop):
            problems.append("tokenizer.stop must be a list of token ids")
    for name, rel in d.get("artifacts", {}).items():
        if not isinstance(rel, str) or not m.path(rel).exists():
            problems.append(f"artifact {name} ({rel}) does not exist")
    clock = d.get("clock_mhz")
    if clock is not None and (not isinstance(clock, int | float) or isinstance(clock, bool) or clock <= 0):
        problems.append("clock_mhz must be a positive number or null")
    caps = d.get("capabilities", ["greedy"])
    if not isinstance(caps, list) or any(c not in CAPABILITIES for c in caps):
        problems.append(f"capabilities must be among {', '.join(CAPABILITIES)}")
    elif "greedy" not in caps:
        problems.append("capabilities must include greedy (the only decoding rule of the harness)")
    host = d.get("host")
    if not isinstance(host, dict) or host.get("kind") not in HOST_KINDS:
        problems.append(f"host.kind must be one of {', '.join(HOST_KINDS)}")
    elif host["kind"] == "spi":
        for key in (*SPI_DEFAULTS, "read_words"):
            v = host.get(key)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                problems.append(f"host.{key} must be a non-negative integer")
        if isinstance(model, dict) and not problems:
            if _positive_int(model.get("vocab")) and model["vocab"] > 2 ** host["token_bits"]:
                problems.append(
                    f"model.vocab {model['vocab']} does not fit the host's {host['token_bits']}-bit token field"
                )
            if _positive_int(model.get("context")) and model["context"] > 2 ** host["pos_bits"]:
                problems.append(
                    f"model.context {model['context']} does not fit the host's {host['pos_bits']}-bit position"
                )
    if backend == "process":
        cmd = d.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
            problems.append("a process chip needs command: a non-empty list of strings")
    timeout = d.get("timeout_s")
    if timeout is not None and (not isinstance(timeout, int | float) or timeout <= 0):
        problems.append("timeout_s must be a positive number")
    if "policy" in d and not isinstance(d["policy"], dict):
        problems.append("policy must be an object")
    prompts = d.get("prompts")
    if prompts is not None and (not isinstance(prompts, str) or not m.path(prompts).is_file()):
        problems.append(f"prompts file {prompts} does not exist")
    return problems


def load(chip_dir: Path, backends: dict[str, str] | None = None) -> Manifest:
    """The manifest of `chip_dir`, with `problems` filled in (never raises for an invalid manifest)."""
    path = chip_dir / "chip.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return Manifest(chip_dir, {}, [f"chip.json cannot be read: {e}"])
    if not isinstance(data, dict):
        return Manifest(chip_dir, {}, ["chip.json must be an object"])
    _normalize(data)
    m = Manifest(chip_dir, data)
    m.problems = validate(m, backends)
    return m
