"""Design-space exploration of the template (requirements FR-ARC-04).

Every point (lanes x memory interface x operand buffers, at a clock) gets its
exact cycles per token from the microcode it would run (`performance.py`) and
its area from the process library (the estimator), and whether it fits the area
limit. The Pareto front holds the points no other fitting point beats on both
speed and die area.

A graph numeric spec (S2's analysis of an imported model) is explored the same
way, from the model's shape: the engine implements the llama2.c structure, so
the cycles are those of the same operations at the model's dimensions, and the
bounds the engine does not meet are reported as findings.
"""

from __future__ import annotations

import itertools
import zipfile

import numpy as np
from llmchip_estimator.estimate import estimate as estimate_area
from llmchip_rtl.compiler import Compiled

from .performance import buffer_bits, options_of, performance

GRAPH_SPEC_FORMAT = "llmchip-numeric-graph-spec/1"


def graph_shape(spec: dict) -> tuple[dict, dict]:
    """(model shape, numeric choices) of a graph numeric spec, for `Compiled.for_shape`."""
    h = spec["hyper"]
    shape = {
        "dim": h["hidden"],
        "hidden_dim": h["intermediate"],
        "n_layers": h["layers"],
        "n_heads": h["heads"],
        "n_kv_heads": h["kvHeads"],
        "head_size": h["headDim"],
        "vocab_size": h["vocab"],
        "seq_len": spec["context"]["maxTokens"],
        "shared_classifier": spec["weights"]["classifier"].startswith("shared"),
    }
    w = spec["weights"]
    numeric = {
        "context": spec["context"]["maxTokens"],
        "groupSize": w["groupSize"],
        "storageBits": w["storageBits"],
        "levels": w["levels"],
    }
    return shape, numeric


def npz_storage_bits(spec: dict, path: str) -> dict[str, int]:
    """Bits a chip would store for a graph numeric spec's weights file, from the array headers
    only (the arrays of a large model need not be loaded)."""
    wbits = spec["weights"]["storageBits"]
    out = {"weights": 0, "scales": 0, "biases": 0, "norms": 0, "luts": 0}
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name = info.filename.removesuffix(".npy")
            if name == "meta":
                continue
            with z.open(info) as f:
                version = np.lib.format.read_magic(f)
                read = np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0
                shape, _, _ = read(f)
            size = int(np.prod(shape))
            if name.endswith(".q"):
                out["weights"] += size * wbits
            elif name.endswith(".m"):
                out["scales"] += size * 15
            elif name.endswith(".b"):
                out["biases"] += size * 32
            elif name.startswith("lut."):
                out["luts"] += size * 16
            else:
                out["norms"] += size * 16
    return out


def compile_point(spec: dict, arrays, lanes: int, options: dict) -> Compiled:
    if spec.get("format") == GRAPH_SPEC_FORMAT:
        shape, numeric = graph_shape(spec)
        return Compiled.for_shape(shape, numeric, lanes, options)
    return Compiled(spec, arrays, lanes, options)


def point(spec: dict, arrays, storage: dict, profile: dict, library: dict, params: dict) -> dict:
    """One design point: exact cycles, area estimate, fit."""
    lanes = int(params.get("parallelMacs", 8))
    clock = float(params.get("clockMHz", 50))
    options = options_of(params)
    compiled = compile_point(spec, arrays, lanes, options)
    perf = performance(compiled, clock, traces=False)
    shape = compiled.cfg
    wbits = spec["weights"]["storageBits"]
    total_bits = sum(storage.values())
    request = {
        "model": {
            "params": -(-total_bits // wbits),
            "dim": shape["dim"],
            "layers": shape["n_layers"],
            "heads": shape["n_heads"],
            "kvHeads": shape["n_kv_heads"],
            "vocab": shape["vocab_size"],
            "hiddenDim": shape["hidden_dim"],
            "title": f"{total_bits:,} stored bits",
        },
        "weightBits": wbits,
        "activationBits": 16,
        "kvBits": 16,
        "contextLength": compiled.context,
        "kvLocation": "offchip",
        "activationLocation": "offchip",
        "weightStorage": params["weightStorage"],
        "parallelMacs": lanes,
        "clockMHz": clock,
        "onchipBufferBits": buffer_bits(compiled),
        **({"targetAreaMm2": params["targetAreaMm2"]} if params.get("targetAreaMm2") else {}),
    }
    ppa = estimate_area(request, profile, library)
    die = (ppa.get("area") or {}).get("dieMm2") or {}
    core = (ppa.get("area") or {}).get("coreMm2") or {}
    limit = (ppa.get("reverse") or {}).get("areaLimitMm2")
    return {
        "id": f"L{lanes}-{options['memoryInterface']}-{options['operandBuffers']}-{clock:g}MHz",
        "lanes": lanes,
        "clockMHz": clock,
        **options,
        "cyclesPerToken": perf["cyclesPerToken"],
        "cyclesFirst": perf["cyclesFirst"],
        "cyclesLast": perf["cyclesLast"],
        "tokensPerSecond": perf["tokensPerSecond"],
        "breakdown": perf["breakdown"],
        "bottleneck": perf["bottleneck"],
        "onchipBufferBits": buffer_bits(compiled),
        "dieMm2": die,
        "coreMm2": core,
        "areaLimitMm2": limit,
        "fits": bool(die) and limit is not None and die.get("typ", float("inf")) <= limit,
        "feasibility": ppa["conclusion"]["feasibility"],
        "items": [{"item": i["item"], "areaMm2": (i["areaMm2"] or {}).get("typ")} for i in ppa["items"]],
        "limits": compiled.limits,
        "externalMemoryBytes": 4 * compiled.dmem_words,
    }


def explore(spec: dict, arrays, storage: dict, profile: dict, library: dict, params: dict) -> dict:
    """All points of the grid, the Pareto front, and the point of the given parameters."""
    lanes = params.get("lanesOptions") or [1, 2, 4, 8]
    interfaces = params.get("memoryInterfaces") or ["spi", "sqi"]
    buffers = params.get("operandBuffersOptions") or ["none", "attention", "all"]
    clocks = params.get("clocksMHz") or [float(params.get("clockMHz", 8))]
    points = []
    for n, mem, buf, clock in itertools.product(lanes, interfaces, buffers, clocks):
        points.append(
            point(
                spec,
                arrays,
                storage,
                profile,
                library,
                params | {"parallelMacs": n, "memoryInterface": mem, "operandBuffers": buf, "clockMHz": clock},
            )
        )
    for p in points:
        area = (p["dieMm2"] or {}).get("typ", float("inf"))
        p["pareto"] = p["fits"] and not any(
            q["fits"]
            and q["tokensPerSecond"] >= p["tokensPerSecond"]
            and (q["dieMm2"] or {}).get("typ", float("inf")) <= area
            and (q["tokensPerSecond"] > p["tokensPerSecond"] or (q["dieMm2"] or {}).get("typ", float("inf")) < area)
            for q in points
        )
    chosen = point(spec, arrays, storage, profile, library, params)
    fitting = [p for p in points if p["fits"]]
    best = max(fitting, key=lambda p: p["tokensPerSecond"]) if fitting else None
    return {
        "format": "llmchip-arch-exploration/1",
        "analysis": spec.get("format") == GRAPH_SPEC_FORMAT,
        "model": spec["model"],
        "grid": {"lanes": lanes, "memoryInterfaces": interfaces, "operandBuffers": buffers, "clocksMHz": clocks},
        "points": points,
        "chosen": chosen,
        "fastestFitting": best["id"] if best else None,
        "storageBits": storage,
    }
