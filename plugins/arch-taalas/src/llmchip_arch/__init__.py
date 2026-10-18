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
    # The engine of this template keeps its working data (activations, attention scratch,
    # trace buffers) in the external memory with the KV cache (rtl-taalas).
    act_location = params.get("activationLocation", "offchip")
    impl = params["weightStorage"]
    context = spec["context"]["maxTokens"]
    wbits = spec["weights"]["storageBits"]
    dim, hidden, layers, vocab = cfg["dim"], cfg["hidden_dim"], cfg["n_layers"], cfg["vocab_size"]
    kv_dim = cfg["n_kv_heads"] * dim // cfg["n_heads"]

    matrix_macs = layers * (2 * dim * dim + 2 * dim * kv_dim + 3 * dim * hidden) + vocab * dim
    attn_macs = 2 * layers * dim * context
    if act_location == "offchip":
        # Memory bound: every input word of a matrix block and every attention operand is one
        # 32-bit transfer over the SPI memory port (SCK = clk / 2: about 70 cycles per word).
        words = matrix_macs / lanes + 2 * attn_macs + 6 * layers * (dim + hidden)
        cycles = math.ceil(words * 70)
    else:
        cycles = math.ceil((matrix_macs + attn_macs) / lanes * 1.1)  # 10% for norms, tables and control
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
        "activationLocation": act_location,
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
    if kv_location == "offchip" or act_location == "offchip":
        # The memory bus is driven only while busy; when idle the host may use it.
        pins += [
            {"name": "kv_sclk", "dir": "out", "enable": "busy"},
            {"name": "kv_cs_n", "dir": "out", "enable": "busy"},
            {"name": "kv_mosi", "dir": "out", "enable": "busy"},
            {"name": "kv_miso", "dir": "in"},
        ]
    period = 1000.0 / clock
    # Interface timing (FR-SGN-02a: the source of every input and output delay constraint).
    asynchronous = "asynchronous to clk; two-flop synchronizer on chip"
    timing = {
        "clock": {"port": "clk", "periodNs": round(period, 3)},
        "inputs": {
            "rst_n": {"maxNs": round(0.5 * period, 3), "minNs": 0.0, "kind": asynchronous},
            "host_sclk": {"maxNs": round(0.5 * period, 3), "minNs": 0.0, "kind": asynchronous},
            "host_cs_n": {"maxNs": round(0.5 * period, 3), "minNs": 0.0, "kind": asynchronous},
            "host_mosi": {"maxNs": round(0.5 * period, 3), "minNs": 0.0, "kind": asynchronous},
        },
        "outputs": {
            "host_miso": {"maxNs": round(0.25 * period, 3), "minNs": 0.0},
            "busy": {"maxNs": round(0.25 * period, 3), "minNs": 0.0},
        },
        # Board environment of the interface: load on outputs, edge rate of inputs, and the
        # slowest edge the board accepts at a pad (the pad's off-chip side).
        "board": {"outputLoadPf": 5.0, "inputTransitionNs": 2.0, "maxPadTransitionNs": round(0.05 * period, 3)},
        "notes": "Host SPI SCK at most clk / 8. Memory SPI SCK = clk / 2: kv_miso is sampled one clock after "
        "the falling SCK edge, so external SRAM access time plus board and pad delays must stay below "
        "the kv_miso input delay.",
    }
    if any(p["name"] == "kv_miso" for p in pins):
        timing["inputs"]["kv_miso"] = {"maxNs": round(0.6 * period, 3), "minNs": 0.0, "kind": "synchronous (SRAM data)"}
        for name in ("kv_sclk", "kv_cs_n", "kv_mosi"):
            timing["outputs"][name] = {"maxNs": round(0.25 * period, 3), "minNs": 0.0}
    points = spec["observationPoints"]
    registers = [
        {
            "point": point["id"],
            "format": point["format"],
            "shape": point["shape"],
            "readback": "trace buffer in the external memory, read by the host while the chip is idle",
        }
        for point in points
    ]
    die = params.get("die")
    chip = {
        "format": "llmchip-chip-spec/1",
        "architecture": ARCHITECTURE,
        "numericSpec": {"hash": spec_hash, "version": spec["version"], "format": spec["format"]},
        "weights": {"hash": weights_hash},
        "process": {"id": profile["id"], "title": profile.get("title")},
        "targets": {
            "areaLimitMm2": area_limit,
            "clockMHz": clock,
            "contextTokens": context,
            **(
                {"die": {"xMm": die["xMm"], "yMm": die["yMm"], "areaMm2": round(die["xMm"] * die["yMm"], 4)}}
                if die
                else {}
            ),
        },
        "function": "autoregressive token generation of the hardwired model: the host sends prompt tokens over SPI, "
        "the chip returns one greedy-decoded token per step (decoding rule of the numeric spec)",
        "dataflow": {
            "kind": "weight-stationary, sequential layers, one weight per lane per cycle",
            "lanes": lanes,
            "activations": "int16, formats from the numeric spec; "
            + ("in the external memory with the KV cache" if act_location == "offchip" else "in on-chip registers"),
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
                "kind": "SPI slave, mode 0, SCK at most clk / 8",
                "commands": {
                    "0x10": "START token[15:0] pos[15:0] mode[7:0]: run one step (mode bit 0: copy the observation "
                    "points, bit 1: also the debug traces); ignored while busy",
                    "0x20": "STATUS: returns busy, error, done, 13 zero bits, token[15:0]",
                },
            },
            **(
                {
                    "memory": {
                        "kind": "SPI master, mode 0, SCK = clk / 2, 23LC1024-style serial SRAM (READ 0x03, "
                        "WRITE 0x02, 24-bit byte address, sequential mode), 32-bit words most significant byte first",
                        "holds": ["KV cache"]
                        + (["activations", "attention scratch", "trace buffers"] if act_location == "offchip" else []),
                    }
                }
                if any(p["name"] == "kv_miso" for p in pins)
                else {}
            ),
        },
        "timing": timing,
        "pins": pins,
        "observability": {"points": registers, "covers": [p["id"] for p in points]},
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
