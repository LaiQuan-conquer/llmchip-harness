"""The `taalas-tile/1` architecture: ROM arrays, parallel MACs and a fixed schedule (FR-ARC).

One engine owns a set of ROM banks (the quantized weights) and `macs` multipliers. In one cycle
it reads one ROM word - `macs` weights for one input column across `macs` output rows - and
multiplies it by one activation; the products accumulate into `macs` accumulators. A matrix of
(rows, columns) therefore takes `columns * ceil(rows / macs)` cycles, and the schedule table of
the design is the cycle model: the RTL generator emits one state per phase, and `verify_spec`
recomputes the cycles from the table.

Q-full gives every decoder layer an engine of its own (the layers run as a pipeline, so a token
costs the slowest layer plus the host frames); a slice has one engine. The model is deliberately
explicit: every phase is a row of the schedule with its cycle count.
"""

from __future__ import annotations

import math

ARCHITECTURE = "taalas-tile/1"
DEFAULT_MACS = 448  # divides 896 (Qwen's hidden) and 4864 (its intermediate size)
HOST_CYCLES = 8  # START and STATUS frames, as the vector engine's host interface


def _matrix_cycles(rows: int, cols: int, macs: int) -> int:
    return cols * math.ceil(rows / macs)


def layer_phases(h: dict, macs: int, context: int) -> list[dict]:
    """The phases of one decoder layer: its matrices, attention, and the vector operations."""
    hd, hidden, inter = h["headDim"], h["hidden"], h["intermediate"]
    kv = h["kvHeads"] * hd
    attn = h["heads"] * hd
    phases = [
        {
            "phase": "q_proj",
            "kind": "matvec",
            "rows": hidden,
            "columns": hidden,
            "cycles": _matrix_cycles(hidden, hidden, macs),
        },
        {
            "phase": "k_proj",
            "kind": "matvec",
            "rows": kv,
            "columns": hidden,
            "cycles": _matrix_cycles(kv, hidden, macs),
        },
        {
            "phase": "v_proj",
            "kind": "matvec",
            "rows": kv,
            "columns": hidden,
            "cycles": _matrix_cycles(kv, hidden, macs),
        },
        {"phase": "attention", "kind": "attention", "cycles": 2 * context * math.ceil(attn / macs)},
        {
            "phase": "o_proj",
            "kind": "matvec",
            "rows": hidden,
            "columns": attn,
            "cycles": _matrix_cycles(hidden, attn, macs),
        },
        {
            "phase": "gate_proj",
            "kind": "matvec",
            "rows": inter,
            "columns": hidden,
            "cycles": _matrix_cycles(inter, hidden, macs),
        },
        {
            "phase": "up_proj",
            "kind": "matvec",
            "rows": inter,
            "columns": hidden,
            "cycles": _matrix_cycles(inter, hidden, macs),
        },
        {"phase": "silu_gate", "kind": "vector", "cycles": math.ceil(inter / macs)},
        {
            "phase": "down_proj",
            "kind": "matvec",
            "rows": hidden,
            "columns": inter,
            "cycles": _matrix_cycles(hidden, inter, macs),
        },
        {"phase": "rmsnorm", "kind": "vector", "cycles": 2 * math.ceil(hidden / macs)},
        {"phase": "rope", "kind": "vector", "cycles": 2},
    ]
    return phases


def schedule(spec: dict, macs: int, engines: int) -> dict:
    """The schedule table: the phases of the slowest engine (Q-full: one engine per layer) or of
    the single engine of a slice, and the token's cycle count."""
    h = spec["hyper"]
    context = spec["context"]["maxTokens"]
    phases = layer_phases(h, macs, context)
    per_layer = sum(p["cycles"] for p in phases)
    if engines == 1:
        rows = [{"engine": 0, "phases": phases}]
        cycles = per_layer + HOST_CYCLES
    else:
        # One engine per layer, the layers as a pipeline: the slowest layer sets the pace. The
        # output projection (the tied classifier) is segmented over the engines - the plan's
        # segmented argmax - so one engine does not serialize the whole vocabulary.
        segment = -(-h["vocab"] // engines)
        rows = [{"engine": i, "phases": list(phases)} for i in range(engines)]
        for i, row in enumerate(rows):
            first = i * segment
            rows_of_segment = max(0, min(segment, h["vocab"] - first))
            if rows_of_segment:
                row["phases"].append(
                    {
                        "phase": f"lm_head[{first}:{first + rows_of_segment}]",
                        "kind": "matvec",
                        "rows": rows_of_segment,
                        "columns": h["hidden"],
                        "cycles": _matrix_cycles(rows_of_segment, h["hidden"], macs),
                    }
                )
        cycles = max(sum(p["cycles"] for p in row["phases"]) for row in rows) + HOST_CYCLES
    return {
        "cyclesPerToken": cycles,
        "cyclesPerLayer": per_layer,
        "engines": engines,
        "rows": rows,
        "hostCycles": HOST_CYCLES,
        "rule": "one ROM word (macs weights) per cycle; a matrix takes columns x ceil(rows/macs); "
        "the schedule table is the cycle model",
    }


def verify(chip: dict) -> dict:
    """Checks the chip spec's numbers against its own schedule table (the cycle model)."""
    sched = chip["schedule"]
    if not sched["rows"]:
        raise ValueError("the schedule has no engine row")
    recomputed = max(sum(p["cycles"] for p in row["phases"]) for row in sched["rows"]) + sched["hostCycles"]
    dataflow = chip["dataflow"]
    clocks = chip["targets"]["clockMHz"]
    return {
        "architecture": chip["architecture"],
        "engines": dataflow["engines"],
        "macs_per_engine": dataflow["macsPerEngine"],
        "rom_word_bits": dataflow["romWordBits"],
        "cycles_per_token": chip["performance"]["cyclesPerToken"],
        "cycles_recomputed": recomputed,
        "schedule_agrees": recomputed == chip["performance"]["cyclesPerToken"],
        "tokens_per_second": chip["performance"]["tokensPerSecond"],
        "macs_per_token": chip["performance"]["macsPerToken"],
        "clock_mhz": clocks,
        "rows": len(sched["rows"]),
    }


def design(
    spec: dict,
    params: dict | None = None,
    storage: dict | None = None,
    profile: dict | None = None,
    library: dict | None = None,
) -> dict:
    """The chip spec of a `taalas-tile/1` design for a graph numeric spec."""
    params = params or {}
    if spec.get("format") != "llmchip-numeric-graph-spec/1":
        raise ValueError("taalas-tile/1 designs a recovered graph's numeric spec")
    h = spec["hyper"]
    wbits = spec["weights"]["storageBits"]
    macs = int(params.get("macsPerEngine", DEFAULT_MACS))
    for size in (h["hidden"], h["intermediate"]):
        if size % macs:
            raise ValueError(f"{macs} MACs do not divide {size}")
    clock = float(params.get("clockMHz", 8))
    engines = h["layers"] if params.get("engines", "per-layer") == "per-layer" else 1
    sched = schedule(spec, macs, engines)
    cycles = sched["cyclesPerToken"]
    chip = {
        "architecture": ARCHITECTURE,
        "model": {
            "family": spec["model"]["family"],
            "architecture": spec["model"].get("architecture"),
            "graphHash": spec["model"].get("graphHash"),
        },
        "numeric": {
            "format": spec["format"],
            "weightBits": wbits,
            "groupSize": spec["weights"]["groupSize"],
            "residualBits": spec["activations"]["bits"]["x"],
            "logitsFrac": spec["logits"]["frac"],
        },
        "hyper": h,
        "dataflow": {
            "engines": engines,
            "macsPerEngine": macs,
            "romWordBits": macs * wbits,
            "romBanks": math.ceil(sum(storage.values()) / (macs * wbits)) if storage else None,
            "kvLocation": "onchip",
            "contextLength": spec["context"]["maxTokens"],
            "argmax": "segmented over the output rows of each engine",
        },
        "ports": [
            {"name": "clk", "dir": "in"},
            {"name": "rst_n", "dir": "in"},
            {"name": "host_sclk", "dir": "in"},
            {"name": "host_cs_n", "dir": "in"},
            {"name": "host_mosi", "dir": "in"},
            {"name": "host_miso", "dir": "out"},
            {"name": "busy", "dir": "out"},
        ],
        "targets": {"clockMHz": clock},
        "performance": {
            "cyclesPerToken": cycles,
            "tokensPerSecond": round(clock * 1e6 / cycles, 3),
            "macsPerToken": 2
            * sum(p.get("rows", 0) * p.get("columns", 0) for row in sched["rows"] for p in row["phases"]),
        },
        "schedule": sched,
        "area": None,
    }
    if profile and library and storage:
        from . import estimate_area  # the plugin's area model

        total_bits = sum(storage.values())
        request = {
            "model": {
                "params": math.ceil(total_bits / wbits),
                "dim": h["hidden"],
                "layers": h["layers"],
                "heads": h["heads"],
                "kvHeads": h["kvHeads"],
                "vocab": h["vocab"],
                "hiddenDim": h["intermediate"],
                "title": f"{spec['model']['family']} ({total_bits:,} stored bits)",
            },
            "weightBits": wbits,
            "activationBits": 16,
            "kvBits": 16,
            "contextLength": spec["context"]["maxTokens"],
            "kvLocation": "onchip",
            "kvStorage": params.get("kvStorage", "gf180mcu.sram-512x8"),
            "activationLocation": "onchip",
            "weightStorage": params.get("weightStorage", "gf180mcu.stdcell-rom"),
            "parallelMacs": macs,
            "clockMHz": clock,
            "onchipBufferBits": 0,
            **({"targetAreaMm2": params["targetAreaMm2"]} if params.get("targetAreaMm2") else {}),
        }
        chip["area"] = estimate_area(request, profile, library)
    return chip
