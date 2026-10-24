"""Taalas-style hardwired architecture template (requirements FR-ARC-01..07, S3).

Weights live in on-chip read-only storage; one MAC lane per storage bank
streams one weight per cycle. Working data and the KV cache live in an external
serial SRAM; operand buffers keep the vectors that are read repeatedly on chip.
The template derives everything numeric from the frozen numeric spec (FR-ARC-06):
it never redefines formats, it references the spec by hash. Its speed is the
cycle count of the microcode it will run (`performance.py`), not an estimate.
"""

from __future__ import annotations

import math

import numpy as np
from llmchip_estimator.estimate import estimate as estimate_area
from llmchip_rtl.compiler import Compiled

from . import spatial
from .performance import buffer_bits, options_of, performance

ARCHITECTURE = "taalas-hardwired/0.2"
TEMPLATES = (ARCHITECTURE, spatial.ARCHITECTURE)


def design(
    spec: dict,
    spec_hash: str,
    weights_hash: str,
    storage: dict,
    profile: dict,
    library: dict,
    params: dict,
    arrays: dict[str, np.ndarray] | None = None,
) -> tuple[dict, dict]:
    """Chip spec and PPA. `arrays` are the numeric weights: the program is compiled from them
    (the cycles do not depend on their values; without them the program is built from the shape).
    params["architecture"] selects the template: the microcoded vector engine (default) or the
    spatial engine (`spatial.py`, the model as circuits)."""
    if params.get("architecture", ARCHITECTURE) == spatial.ARCHITECTURE:
        return spatial.design(spec, spec_hash, weights_hash, storage, profile, params, arrays)
    cfg = spec["model"]["config"]
    lanes = int(params.get("parallelMacs", 8))
    clock = float(params.get("clockMHz", 50))
    options = options_of(params)
    sqi = options["memoryInterface"] == "sqi"
    compiled = Compiled(spec, arrays, lanes, options)
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
    kv_bytes_token = layers * 2 * kv_dim * 4  # K and V, one 32-bit word per value
    bandwidth = float(params.get("offchipBandwidthMBps", 25))

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
        "onchipBufferBits": buffer_bits(compiled),
        **({"targetAreaMm2": params["targetAreaMm2"]} if params.get("targetAreaMm2") else {}),
        **({"kvStorage": params["kvStorage"]} if params.get("kvStorage") else {}),
    }
    ppa = estimate_area(request, profile, library)
    ppa["performance"] = performance(compiled, clock) | {"macsPerToken": matrix_macs + attn_macs}
    ppa["microarchitecture"] = {
        **options,
        "lanes": lanes,
        "vectorBufferWords": compiled.widths.get("VBN", 0),
        "attentionAccumulators": compiled.widths.get("HSN", 0),
        "onchipBufferBits": buffer_bits(compiled),
        "datapathBits": compiled.widths["DW"],
        "programInstructions": len(compiled.program),
        "externalMemoryWords": compiled.dmem_words,
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
    if (kv_location == "offchip" or act_location == "offchip") and sqi:
        # Quad SPI: SCK and CS driven only while busy; four data lines driven only while the
        # port sends (command, address, write data); when idle the host may use the bus.
        pins += [
            {"name": "kv_sclk", "dir": "out", "enable": "busy"},
            {"name": "kv_cs_n", "dir": "out", "enable": "busy"},
            *[{"name": f"kv_sio{k}", "dir": "inout"} for k in range(4)],
        ]
    elif kv_location == "offchip" or act_location == "offchip":
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
    if sqi:
        # The data lines are inputs (sampled one clock after the falling SCK edge, as kv_miso)
        # and outputs (changed with the falling SCK edge, as kv_mosi).
        for k in range(4):
            timing["inputs"][f"kv_sio{k}"] = {
                "maxNs": round(0.6 * period, 3),
                "minNs": 0.0,
                "kind": "synchronous (SRAM data)",
            }
            timing["outputs"][f"kv_sio{k}"] = {"maxNs": round(0.25 * period, 3), "minNs": 0.0}
        for name in ("kv_sclk", "kv_cs_n"):
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
            **({"cyclesPerToken": int(params["cyclesPerToken"])} if params.get("cyclesPerToken") else {}),
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
            "operandBuffers": options["operandBuffers"],
            "memoryInterface": options["memoryInterface"],
            "activations": "int16, formats from the numeric spec; "
            + ("in the external memory with the KV cache" if act_location == "offchip" else "in on-chip registers"),
            "accumulatorBits": spec["accumulator"]["bits"],
            "accumulatorCanOverflow": spec["accumulator"].get("canOverflow", False),
            "rounding": str(spec.get("rounding", {}).get("mode", "half-up")).removeprefix("round-"),
            "overflow": spec.get("overflow", {}).get("mode", "saturate"),
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
                "interface": ("SQI SRAM" if sqi else "SPI SRAM")
                if kv_location == "offchip"
                else params.get("kvStorage"),
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
                        "kind": (
                            "quad SPI (SQI) master on 4 bidirectional lines, SCK = clk / 2, 23LC1024-style serial "
                            "SRAM put into SQI mode after reset (RSTIO 0xFF, EQIO 0x38; READ 0x03 with 2 dummy SCK, "
                            "WRITE 0x02, 24-bit byte address, sequential mode); a board reading the SRAM while the "
                            "chip is idle uses SQI or sends RSTIO first"
                            if sqi
                            else "SPI master, mode 0, SCK = clk / 2, 23LC1024-style serial SRAM (READ 0x03, "
                            "WRITE 0x02, 24-bit byte address, sequential mode)"
                        )
                        + ", 32-bit words most significant byte first",
                        "holds": ["KV cache"]
                        + (["activations", "attention scratch", "trace buffers"] if act_location == "offchip" else []),
                    }
                }
                if any(p["name"] == "kv_sclk" for p in pins)
                else {}
            ),
        },
        "timing": timing,
        "pins": pins,
        "observability": {"points": registers, "covers": [p["id"] for p in points]},
        "performance": {k: v for k, v in ppa["performance"].items() if not k.endswith("PerPosition")},
        "processCapabilities": sorted(
            {f"storage:{impl}", "stdcell-logic", "io-pads"}
            | ({f"storage:{params['kvStorage']}"} if params.get("kvStorage") else set())
        ),
    }
    return chip, ppa


def verify(chip: dict, spec: dict, spec_hash: str, ppa: dict, arrays: dict | None = None) -> dict:
    """Consistency of a chip spec with its numeric spec and area limit (S3 evidence, check arch.spec). With the
    numeric weights, the stated speed is recomputed from the microcode the chip spec implies
    (or, for the spatial template, from the circuits the weights give)."""
    if chip.get("architecture") == spatial.ARCHITECTURE:
        return spatial.verify(chip, spec, spec_hash, ppa, arrays)
    covered = set(chip.get("observability", {}).get("covers", []))
    speed: dict = {}
    if arrays is not None:
        flow = chip["dataflow"]
        compiled = Compiled(spec, arrays, flow["lanes"], options_of(flow))
        exact = performance(compiled, chip["targets"]["clockMHz"])
        stated = chip.get("performance", {}).get("cyclesPerToken")
        speed = {"cycles_match_model": stated is not None and abs(stated - exact["cyclesPerToken"]) < 0.5}
        speed["cycles_per_token.model"] = exact["cyclesPerToken"]
        # The speed target is cycles per token at the last position (the slowest).
        target = chip["targets"].get("cyclesPerToken")
        speed["cycles_target_set"] = bool(target)
        if target:
            speed["cycles_within_target"] = exact["cyclesLast"] <= target
            speed["cycles_target"] = target
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
        **speed,
    }
