"""Simulation library of the sim-verilator plugin (runs in the EDA container, Python 3.10).

Builds an RTL bundle with Verilator, plays stimulus files, parses the
testbench's binary log, and compares observation points with the L2 traces
or with the instruction-level simulator shipped in the bundle (isa.py).
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import re
import struct
import subprocess
import tarfile
from pathlib import Path

import numpy as np

TOOLS = "/foss/tools/bin"
POINT_NAMES = {1: "attn_residual", 2: "ffn_residual", 3: "logits", 15: "op"}
ENV = {
    **os.environ,
    "PATH": f"{TOOLS}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    # Icarus Verilog's runtime library is not on the image's default library path.
    "LD_LIBRARY_PATH": ":".join(p for p in ("/foss/tools/iverilog/lib", os.environ.get("LD_LIBRARY_PATH", "")) if p),
}


def run(cmd: list[str], cwd: Path, timeout: int = 3600) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=ENV)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}):\n{(proc.stdout + proc.stderr)[-4000:]}")
    return proc.stdout + proc.stderr


class Bundle:
    def __init__(self, tar_path: str, work: Path) -> None:
        with tarfile.open(tar_path) as tar:
            tar.extractall(work)
        self.root = work / "bundle"
        self.rtl = self.root / "rtl"
        self.design = json.loads((self.root / "design.json").read_text())
        spec = importlib.util.spec_from_file_location("bundle_isa", self.root / "isa.py")
        self.isa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.isa)
        self.binaries: dict[str, Path] = {}

    @property
    def params(self) -> dict:
        return self.design["params"]

    def lint(self) -> dict:
        proc = subprocess.run(
            [f"{TOOLS}/verilator", "--lint-only", "llmchip_core.v"],
            cwd=self.rtl,
            capture_output=True,
            text=True,
            env=ENV,
        )
        text = proc.stdout + proc.stderr
        warnings = sum(1 for line in text.splitlines() if line.startswith("%Warning"))
        errors = sum(1 for line in text.splitlines() if line.startswith("%Error") and "Exiting due to" not in line)
        return {"lint_warnings": warnings, "lint_errors": errors, "lint_exit": proc.returncode, "log": text}

    def build(self, coverage: bool = False) -> Path:
        key = "cov" if coverage else "fast"
        if key in self.binaries:
            return self.binaries[key]
        mdir = f"obj_{key}"
        cmd = [
            f"{TOOLS}/verilator",
            "--cc",
            "--exe",
            "--build",
            "-O3",
            "--x-assign",
            "fast",
            "--x-initial",
            "fast",
            "-j",
            str(os.cpu_count() or 4),
            "--Mdir",
            mdir,
            "-o",
            "Vsim",
            "llmchip_core.v",
            "tb.cpp",
            "-CFLAGS",
            "-O2",
        ]
        if coverage:
            cmd.insert(1, "--coverage-line")
        run(cmd, self.rtl, timeout=3600)
        self.binaries[key] = self.rtl / mdir / "Vsim"
        return self.binaries[key]

    def simulate(
        self,
        stimulus: list[str],
        *,
        debug: bool = False,
        coverage: bool = False,
        name: str = "sim",
        prog: str | None = None,
    ) -> list[dict]:
        """Plays a stimulus; `prog` replaces the program ROM image; a coverage run writes `<name>.dat`."""
        binary = self.build(coverage)
        stim = self.rtl / f"{name}.stim"
        out = self.rtl / f"{name}.bin"
        stim.write_text("\n".join(stimulus) + "\n")
        prog = prog or ("prog_debug.hex" if debug else None)
        args = (
            [str(binary)]
            + ([f"+prog={prog}"] if prog else [])
            + ([f"+cov={name}.dat"] if coverage else [])
            + [stim.name, out.name]
        )
        run(args, self.rtl, timeout=7200)
        return parse(out.read_bytes())

    def machine(self, debug: bool = False):
        img = np.load(self.root / "isa-image.npz")
        program = [int(w) for w in img["debug" if debug else "program"]]
        p = self.params
        return self.isa.Machine(program, img["wrom"], img["srom"], img["crom"], p["LANES"], p["NDMEM"], p["CTX"])


def parse(data: bytes) -> list[dict]:
    a = np.frombuffer(data, dtype="<i4")
    records, i = [], 0
    while i < len(a):
        if a[i] != 0x53544550:
            raise ValueError(f"corrupt log at word {i}")
        rid, pos, token_in, token_out, nxt, cycles, error, timeout, ngroups = (int(v) for v in a[i + 1 : i + 10])
        i += 10
        groups = []
        for _ in range(ngroups):
            point, layer, count, start = (int(v) for v in a[i : i + 4])
            i += 4
            groups.append({"point": point, "layer": layer, "start": start, "values": a[i : i + count].astype(np.int64)})
            i += count
        records.append(
            {
                "run": rid,
                "pos": pos,
                "token_in": token_in,
                "token_out": token_out,
                "next": nxt,
                "cycles": cycles,
                "error": error,
                "timeout": timeout,
                "groups": groups,
            }
        )
    return records


def run_line(rid: int, steps: int, tokens: list[int], reset_cycles: int = 0) -> str:
    return f"RUN {rid} {steps} {reset_cycles} {len(tokens)} " + " ".join(str(t) for t in tokens)


def step_points(record: dict, layers: int) -> dict:
    out = {"attn_residual": [None] * layers, "ffn_residual": [None] * layers, "logits": None}
    for g in record["groups"]:
        name = POINT_NAMES.get(g["point"])
        if name in ("attn_residual", "ffn_residual"):
            out[name][g["layer"]] = g["values"]
        elif name == "logits":
            out["logits"] = g["values"]
    return out


def first_difference(a, b):
    if a is None or b is None:
        return 0
    a, b = np.asarray(a), np.asarray(b)
    if a.shape != b.shape:
        return 0
    diff = np.nonzero(a != b)[0]
    return int(diff[0]) if len(diff) else None


def compare_with_l2(records: list[dict], trace: dict, run_id: int, key: str, layers: int) -> dict:
    """Compares one run with the L2 trace of a sequence (key `p3`: prompt 3, `r0`: random
    sequence 0). Returns counts and the first divergence."""
    expected_tokens = trace[f"{key}.tokens"]
    steps = [r for r in records if r["run"] == run_id]
    result = {
        "steps": len(steps),
        "expected_steps": len(expected_tokens),
        "values": 0,
        "mismatches": 0,
        "first": None,
        "cycles": sum(r["cycles"] for r in steps),
    }
    for r in steps:
        pos = r["pos"]
        if pos >= len(expected_tokens):
            result["mismatches"] += 1
            result["first"] = result["first"] or {"pos": pos, "point": "extra step"}
            continue
        got = step_points(r, layers)
        checks = [
            ("attn_residual", n, got["attn_residual"][n], trace[f"{key}.attn_residual"][pos][n]) for n in range(layers)
        ]
        checks += [
            ("ffn_residual", n, got["ffn_residual"][n], trace[f"{key}.ffn_residual"][pos][n]) for n in range(layers)
        ]
        checks += [
            ("logits", 0, got["logits"], trace[f"{key}.logits"][pos]),
            ("token", 0, [r["next"]], [int(expected_tokens[pos])]),
        ]
        for point, layer, a, b in checks:
            result["values"] += len(b)
            index = first_difference(a, b)
            if index is not None:
                result["mismatches"] += 1
                if result["first"] is None:
                    result["first"] = {"pos": pos, "point": point, "layer": layer, "index": index}
    if len(steps) != len(expected_tokens) and result["first"] is None:
        result["mismatches"] += 1
        result["first"] = {"pos": len(steps), "point": "missing steps"}
    return result


def tokens_in(trace: dict, key: str) -> list[int]:
    """Input token of every position of an L2 sequence: the first prompt token, then each step's next token."""
    return [int(trace[f"{key}.prompt"][0])] + [int(t) for t in trace[f"{key}.tokens"][:-1]]


def compare_with_isa(records: list[dict], machine, run_id: int, tokens_in: list[int], layers: int) -> dict:
    """Compares a run whose tokens are all forced with the instruction-level simulator."""
    steps = [r for r in records if r["run"] == run_id]
    result = {"steps": len(steps), "values": 0, "mismatches": 0, "first": None}
    for r in steps:
        traces: list = []
        token = machine.step(tokens_in[r["pos"]], r["pos"], traces)
        expected = {
            ("attn_residual", t["layer"])
            if t["point"] == 1
            else ("ffn_residual", t["layer"])
            if t["point"] == 2
            else ("logits", 0): t["values"]
            for t in traces
        }
        got = step_points(r, layers)
        checks = [(k, got[k[0]][k[1]] if k[0] != "logits" else got["logits"], v) for k, v in expected.items()]
        checks.append((("token", 0), [r["token_out"]], [token]))
        for (point, layer), a, b in checks:
            result["values"] += len(b)
            index = first_difference(a, b)
            if index is not None:
                result["mismatches"] += 1
                if result["first"] is None:
                    result["first"] = {"pos": r["pos"], "point": point, "layer": layer, "index": index}
    return result


def compare_debug(bundle: Bundle, tokens: list[int], upto: int, name: str = "debug") -> dict:
    """Runs positions 0..upto with the debug program, which traces the output of every
    operation, and compares every trace with the instruction-level simulator."""
    records = bundle.simulate([run_line(0, upto + 1, tokens[: upto + 1])], debug=True, name=name)
    machine = bundle.machine(debug=True)
    descriptions = bundle.design["program"]
    result: dict = {"steps": len(records), "operations": 0, "values": 0, "mismatches": 0, "first": None}
    for r in records:
        expected: list = []
        machine.step(tokens[r["pos"]], r["pos"], expected)
        got = [g for g in r["groups"] if g["point"] == 15]
        want = [t for t in expected if t["point"] == 15]
        if len(got) != len(want):
            result["mismatches"] += 1
            result["first"] = result["first"] or {
                "pos": r["pos"],
                "op": None,
                "operation": "operation count differs",
                "element": None,
                "cycle": None,
            }
        prev_end = 0
        for g, w in zip(got, want, strict=False):
            op_cycles = [prev_end, g["start"] - 1]
            prev_end = g["start"] + len(g["values"])
            result["operations"] += 1
            result["values"] += len(w["values"])
            index = first_difference(g["values"], w["values"])
            if index is not None:
                result["mismatches"] += 1
                if result["first"] is None:
                    op = w["layer"]
                    result["first"] = {
                        "pos": r["pos"],
                        "op": op,
                        "operation": descriptions[op],
                        "element": index,
                        # Cycles from the step start, debug program: the operation runs in
                        # `opCycles`; its trace streams one element per cycle right after it.
                        "opCycles": op_cycles,
                        "cycle": g["start"] + index,
                        "rtl": int(g["values"][index]),
                        "expected": int(w["values"][index]),
                    }
    if len(records) != upto + 1 and result["first"] is None:
        result["mismatches"] += 1
        result["first"] = {
            "pos": len(records),
            "op": None,
            "operation": "missing steps",
            "element": None,
            "cycle": None,
        }
    return result


def locate(bundle: Bundle, tokens: list[int], upto: int) -> dict | None:
    """The first operation whose output differs from the instruction-level simulator, with its cycle."""
    return compare_debug(bundle, tokens, upto, name="locate")["first"]


def waveform(bundle: Bundle, tokens: list[int], pos: int, start: int, end: int, path: Path) -> Path:
    """VCD of cycles start..end of the step at `pos` (debug program, normal run up to that step)."""
    if "trace" not in bundle.binaries:
        cmd = [
            f"{TOOLS}/verilator",
            "--cc",
            "--exe",
            "--build",
            "-O1",
            "--trace",
            "--x-assign",
            "fast",
            "--x-initial",
            "fast",
            "-j",
            str(os.cpu_count() or 4),
            "--Mdir",
            "obj_trace",
            "-o",
            "Vsim",
            "llmchip_core.v",
            "tb.cpp",
        ]
        run(cmd, bundle.rtl, timeout=3600)
        bundle.binaries["trace"] = bundle.rtl / "obj_trace" / "Vsim"
    stim = bundle.rtl / "wave.stim"
    stim.write_text(run_line(0, pos + 1, tokens[: pos + 1]) + "\n")
    run(
        [
            str(bundle.binaries["trace"]),
            "+prog=prog_debug.hex",
            f"+vcd={path}",
            f"+vcd_pos={pos}",
            f"+vcd_from={max(start, 0)}",
            f"+vcd_to={end}",
            stim.name,
            "wave.bin",
        ],
        bundle.rtl,
        timeout=7200,
    )
    return path


def coverage_summary(bundle: Bundle, data_files: list[str]) -> dict:
    """Line coverage of the Verilog sources, merged over the coverage runs: a source line
    with coverage points counts as covered when all of its points were hit."""
    annotate = bundle.rtl / "coverage-annotated"
    run([f"{TOOLS}/verilator_coverage", "--annotate", str(annotate), "--annotate-min", "1", *data_files], bundle.rtl)
    files, uncovered = {}, []
    for file in sorted(annotate.glob("*.v*")):
        counted = covered = 0
        for number, line in enumerate(file.read_text().splitlines(), 1):
            m = re.match(r"([ %])(\d{6,})", line)
            if not m:
                continue
            counted += 1
            if m.group(1) == " ":
                covered += 1
            else:
                uncovered.append(f"{file.name}:{number}: {line[m.end() :].strip()}")
        files[file.name] = {"lines": counted, "covered": covered, "pct": round(100.0 * covered / max(counted, 1), 2)}
    total = sum(f["lines"] for f in files.values())
    hit = sum(f["covered"] for f in files.values())
    if not total:
        raise RuntimeError("the coverage annotation has no coverage points")
    return {
        "metric": "line",
        "lines": total,
        "covered": hit,
        "pct": round(100.0 * hit / total, 2),
        "files": files,
        "uncovered": uncovered[:200],
    }


class Tokenizer:
    """Port of llmchip_model.Tokenizer (run.c's BPE), for the host side of the playground in
    the container; tests keep the two identical."""

    def __init__(self, data: bytes, vocab_size: int) -> None:
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

    def encode(self, text: str) -> list[int]:
        ids = [self.index[b" "]] if text else []
        for ch in text:
            piece = ch.encode("utf-8")
            ids.extend([self.index[piece]] if piece in self.index else [b + 3 for b in piece])
        while True:
            best, best_id, best_at = -1e10, -1, -1
            for i in range(len(ids) - 1):
                j = self.index.get(self.vocab[ids[i]] + self.vocab[ids[i + 1]])
                if j is not None and self.scores[j] > best:
                    best, best_id, best_at = self.scores[j], j, i
            if best_at < 0:
                return [1, *ids]
            ids[best_at : best_at + 2] = [best_id]

    def decode(self, tokens: list[int]) -> str:
        out = b""
        for prev, tok in itertools.pairwise(tokens):
            piece = self.vocab[tok]
            if prev == 1 and piece.startswith(b" "):
                piece = piece[1:]
            if len(piece) == 6 and piece.startswith(b"<0x") and piece.endswith(b">"):
                piece = bytes([int(piece[3:5], 16)])
            out += piece
        return out.decode("utf-8", errors="replace")
