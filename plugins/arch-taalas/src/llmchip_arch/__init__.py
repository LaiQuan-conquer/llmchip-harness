"""Taalas-style hardwired architecture template (requirements FR-ARC-01..07, S3).

Weights live in on-chip read-only storage; one MAC lane per storage bank
streams one weight per cycle while activations sit in register buffers. The
template derives everything numeric from the frozen numeric spec (FR-ARC-06):
it never redefines formats, it references the spec by hash.
"""

from __future__ import annotations

import math

from llmchip_estimator.estimate import estimate as estimate_area

ARCHITECTURE = "taalas-hardwired/0.1"


def design(
    spec: dict, spec_hash: str, weights_hash: str, storage: dict, profile: dict, library: dict, params: dict
) -> tuple[dict, dict]:
    cfg = spec["model"]["config"]
    lanes = int(params.get("parallelMacs", 8))
    clock = float(params.get("clockMHz", 50))
    kv_location = params.get("kvLocation", "offchip")
    impl = params["weightStorage"]
    context = spec["context"]["maxTokens"]
    wbits = spec["weights"]["storageBits"]
    dim, hidden, layers, vocab = cfg["dim"], cfg["hidden_dim"], cfg["n_layers"], cfg["vocab_size"]
    kv_dim = cfg["n_kv_heads"] * dim // cfg["n_heads"]

    matrix_macs = layers * (2 * dim * dim + 2 * dim * kv_dim + 3 * dim * hidden) + vocab * dim
    attn_macs = 2 * layers * dim * context
    cycles = math.ceil((matrix_macs + attn_macs) / lanes * 1.1)  # 10% for norms, nonlinear tables and control
    compute_ms = cycles / (clock * 1e3)
    kv_bytes_token = layers * 2 * kv_dim * 2  # int16 K and V
    bandwidth = float(params.get("offchipBandwidthMBps", 25))
    io_ms = kv_bytes_token * (context + 1) / (bandwidth * 1e3) if kv_location == "offchip" else 0.0
    token_ms = max(compute_ms, io_ms)

    total_bits = sum(storage.values())
    request = {
        # Everything stored on chip, expressed in weight-sized units for the area model.
        "model": {
            "params": math.ceil(total_bits / wbits),
            "dim": dim,
            "layers": layers,
            "heads": cfg["n_heads"],
            "kvHeads": cfg["n_kv_heads"],
            "vocab": vocab,
            "hiddenDim": hidden,
            "title": f"{spec['model']['family']} ({total_bits:,} stored bits)",
        },
        "weightBits": wbits,
        "activationBits": 16,
        "kvBits": 16,
        "contextLength": context,
        "kvLocation": kv_location,
        "weightStorage": impl,
        "parallelMacs": lanes,
        "clockMHz": clock,
        "offchipBandwidthMBps": bandwidth,
        **({"targetAreaMm2": params["targetAreaMm2"]} if params.get("targetAreaMm2") else {}),
        **({"kvStorage": params["kvStorage"]} if params.get("kvStorage") else {}),
    }
    ppa = estimate_area(request, profile, library)
    ppa["performance"] = {
        "macsPerToken": matrix_macs + attn_macs,
        "cyclesPerToken": cycles,
        "computeMsPerToken": compute_ms,
        "offchipBytesPerToken": kv_bytes_token * (context + 1) if kv_location == "offchip" else 0,
        "msPerToken": token_ms,
        "tokensPerSecond": 1000 / token_ms,
        "bottleneck": "off-chip interface" if io_ms > compute_ms else "compute",
    }
    area_limit = ppa.get("reverse", {}).get("areaLimitMm2")

    pins = [
        {"name": "clk", "dir": "in"},
        {"name": "rst_n", "dir": "in"},
        {"name": "host_sclk", "dir": "in"},
        {"name": "host_cs_n", "dir": "in"},
        {"name": "host_mosi", "dir": "in"},
        {"name": "host_miso", "dir": "out"},
        {"name": "busy", "dir": "out"},
    ]
    if kv_location == "offchip":
        pins += [
            {"name": f"kv_{n}", "dir": d}
            for n, d in (("sclk", "out"), ("cs_n", "out"), ("mosi", "out"), ("miso", "in"))
        ]
    points = spec["observationPoints"]
    registers, address = [], 0x100
    for point in points:
        registers.append(
            {
                "point": point["id"],
                "address": address,
                "format": point["format"],
                "shape": point["shape"],
                "readback": "host SPI command 0x5x",
            }
        )
        address += 0x100
    chip = {
        "format": "llmchip-chip-spec/1",
        "architecture": ARCHITECTURE,
        "numericSpec": {"hash": spec_hash, "version": spec["version"], "format": spec["format"]},
        "weights": {"hash": weights_hash},
        "process": {"id": profile["id"], "title": profile.get("title")},
        "targets": {"areaLimitMm2": area_limit, "clockMHz": clock, "contextTokens": context},
        "function": "autoregressive token generation of the hardwired model: the host sends prompt tokens over SPI, "
        "the chip returns one greedy-decoded token per step (decoding rule of the numeric spec)",
        "dataflow": {
            "kind": "weight-stationary, sequential layers, one weight per lane per cycle",
            "lanes": lanes,
            "activations": "int16 registers, formats from the numeric spec",
            "accumulatorBits": spec["accumulator"]["bits"],
            "nonlinear": [
                "rsqrt table (rmsnorm)",
                "exp table (softmax)",
                "sigmoid table (SiLU)",
                "RoPE cos/sin tables",
            ],
            "schedule": [
                "embedding lookup",
                *[
                    f"layer {i}: rmsnorm, q/k/v, rope, attention, wo, residual, rmsnorm, w1/w3, silu-gate, w2, residual"
                    for i in range(layers)
                ],
                "final rmsnorm",
                "classifier (shares the embedding storage)" if cfg["shared_classifier"] else "classifier",
                "argmax",
            ],
        },
        "storage": {
            "weights": {
                "implementation": impl,
                "bits": storage["weights"],
                "banks": lanes,
                "wordWidth": wbits,
                "wordsPerBank": math.ceil(storage["weights"] / wbits / lanes),
            },
            "scales": {"implementation": impl, "bits": storage["scales"]},
            "norms": {"implementation": impl, "bits": storage["norms"]},
            "tables": {"implementation": impl, "bits": storage["luts"]},
            "kvCache": {
                "location": kv_location,
                "bytes": kv_bytes_token * context,
                "format": "int16",
                "interface": "SPI SRAM" if kv_location == "offchip" else params.get("kvStorage"),
            },
        },
        "interfaces": {
            "host": {
                "kind": "SPI slave, mode 0",
                "commands": {
                    "0x01": "reset sequence",
                    "0x02": "push token",
                    "0x03": "read next token",
                    "0x5x": "debug readback",
                    "0x60": "single step",
                },
            },
            **({"kvMemory": {"kind": "SPI master to an external SRAM"}} if kv_location == "offchip" else {}),
        },
        "pins": pins,
        "observability": {"registers": registers, "singleStep": True, "covers": [p["id"] for p in points]},
        "performance": ppa["performance"],
        "processCapabilities": sorted(
            {f"storage:{impl}", "stdcell-logic", "io-pads"}
            | ({f"storage:{params['kvStorage']}"} if params.get("kvStorage") else set())
        ),
    }
    return chip, ppa


def verify(chip: dict, spec: dict, spec_hash: str, ppa: dict) -> dict:
    """Consistency of a chip spec with its numeric spec and area limit (G2 evidence)."""
    covered = set(chip.get("observability", {}).get("covers", []))
    needed = {p["id"] for p in spec["observationPoints"]}
    limit = chip["targets"].get("areaLimitMm2")
    die = ppa.get("area", {}).get("dieMm2", {}).get("typ")
    return {
        "numeric_spec_matches": chip.get("numericSpec", {}).get("hash") == spec_hash,
        "observation_points_covered": needed <= covered,
        "feasibility": ppa["conclusion"]["feasibility"],
        "fits_area": die is not None and limit is not None and die <= limit,
        **({"die_area_mm2.typ": die} if die is not None else {}),
        **({"area_limit_mm2": limit} if limit is not None else {}),
        "tokens_per_second": ppa["performance"]["tokensPerSecond"],
    }
