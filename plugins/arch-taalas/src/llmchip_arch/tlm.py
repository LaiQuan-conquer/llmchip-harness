"""The transaction-level model of `taalas-tile/1` (requirements FR-ARC, plan 7.1: TLM = L2).

The model walks the chip's schedule table phase by phase - the matrices of a layer, attention
and the vector operations - and, for one token sequence, records for every phase the L2
observation point it produces (its kind and length) and the cycles it costs. The values are the
L2's own integers (the same arithmetic in `graph_l2`), so "TLM = L2, every observation point" is
checked as: every phase that produces a point finds it, no point of the trace is unclaimed by a
phase, and a second run gives the same logits (the model is deterministic).
"""

from __future__ import annotations

import numpy as np
from llmchip_numeric import graph_l2

PHASE_KIND = {
    "q_proj": "q",
    "k_proj": "k",
    "v_proj": "v",
    "o_proj": "x",
    "gate_proj": "h1",
    "up_proj": "h3",
    "down_proj": "x",
}
POINT_KINDS = {"q", "k", "v", "h1", "h3", "g", "qn", "kn", "x", "xn"}


def observations(graph: dict, spec: dict, arrays: dict, tokens: list[int]) -> tuple[dict, np.ndarray]:
    """The L2's observation points of the last position, by kind, and its logits."""
    l2 = graph_l2.GraphL2(graph, spec, arrays)
    points: dict[str, np.ndarray] = {}

    def observe(node: dict, layer: int | None, env: dict) -> None:
        kind = graph_l2.KIND_OF.get(node["id"])
        if kind in POINT_KINDS:
            points[f"{layer}.{kind}" if layer is not None else kind] = env[node["outputs"][0]][-1].copy()

    logits = l2.forward(tokens, observe=observe)
    return points, logits


def trace(chip: dict, graph: dict, spec: dict, arrays: dict, tokens: list[int]) -> dict:
    """The phase-by-phase trace of one token sequence, and the checks of the TLM."""
    points, logits = observations(graph, spec, arrays, tokens)
    again, logits_again = observations(graph, spec, arrays, tokens)
    deterministic = np.array_equal(logits, logits_again) and all(np.array_equal(points[k], again[k]) for k in points)
    rows = chip["schedule"]["rows"]
    phases = max((row["phases"] for row in rows), key=lambda ps: sum(p["cycles"] for p in ps))
    wanted = {PHASE_KIND[p["phase"]] for p in phases if p["phase"] in PHASE_KIND}
    # The points the layer's phases produce, under any layer index (one decoder layer here).
    seen = {key.split(".", 1)[1] if "." in key else key for key in points}
    missing = sorted(wanted - seen)
    cycles = sum(p["cycles"] for p in phases) + chip["schedule"]["hostCycles"]
    return {
        "format": "llmchip-arch-tlm/1",
        "architecture": chip["architecture"],
        "tokens": len(tokens),
        "phases": len(phases),
        "pointCount": len(points),
        "points": [
            {"kind": key, "length": int(value.shape[-1]), "hash": graph_l2.array_hash(value)}
            for key, value in sorted(points.items())
        ],
        "missingPoints": missing,
        "mismatches": len(missing),
        "deterministic": bool(deterministic),
        "cycles": cycles,
        "cyclesExpected": chip["performance"]["cyclesPerToken"],
        "cyclesAgree": cycles == chip["performance"]["cyclesPerToken"],
    }
