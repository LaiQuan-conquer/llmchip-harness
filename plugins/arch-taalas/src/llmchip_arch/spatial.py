"""The spatial architecture template `taalas-spatial/1` (requirements FR-ARC-01..07, S3).

The model is built as circuits, as Taalas does: every matrix row is a bit-serial adder tree
wired from its weights (llmchip_rtl.spatial), every layer has its own trees, and a token
flows through them in order. Attention and the KV cache are runtime data: a shared
attention unit and on-chip KV rings. There is no program and no external memory.

Speed is the fixed schedule of the generated state machine (the same at every position,
checked against the RTL by simulation). Area is the generated structure (serial adders,
delay bits, output stages, KV bits, multipliers, tables) times cell areas measured by
synthesis (process library `spatialCells`), placed at the profile's utilization.
"""

from __future__ import annotations

import math

from llmchip_estimator.estimate import die_for_core
from llmchip_rtl.spatial.generator import ARCHITECTURE, Spatial

POINTS = ("low", "typ", "high")


def structure(sp: Spatial) -> dict:
    """Counts of the generated design that its area follows."""
    sp.files()  # generates the circuits; counts their adders and delay bits
    mats = sp.matrices.values()
    D, H, L, C, KD, V = sp.dim, sp.hidden, sp.layers, sp.ctx, sp.kv_dim, sp.vocab
    nd = sp.dividers
    a = sp.arrays
    tables = (
        V * D * 16
        + a["lut.rsqrt"].size * 17
        + 2 * a["lut.exp"].size * 17  # one table per head
        + a["lut.sigmoid"].size * 17
        + (a["lut.rope_cos"].size + a["lut.rope_sin"].size) * 16
        + (2 * L + 1) * D * 16
    )
    return {
        "serialAdders": sum(m.adders for m in mats),
        # Scale-digit lines per group, and the input lines of weights beyond +-1.
        "delayBits": sum(m.delays for m in mats) + sum(sp.input_delays.values()),
        "outputRows16": sum(int(m.q.shape[0]) for m in mats if m.width == 16),
        "outputRows32": sum(int(m.q.shape[0]) for m in mats if m.width == 32),
        "kvBits": L * 2 * C * KD * 16,
        "attentionMultipliers": D,
        "tableBits": int(tables),
        # Registers of the core beyond the KV rings: vectors, probabilities, accumulators,
        # dividers, observation registers (2 * layers residual streams), control.
        "coreFlopBits": 2 * L * D * 16
        + 2 * C * 17
        + D * 40
        + 4 * D * 16
        + 2 * KD * 16
        + H * 16
        + nd * (23 + 16)
        + 2 * 32
        + 2 * 23
        + 38
        + 17
        + 7
        + 150,
        "dim": D,
    }


def area(counts: dict, cells: dict, logic: dict) -> dict:
    """Cell area per block (mm2, low/typ/high) and the placed core."""

    def r(key: str) -> dict:
        v = cells[key]
        return {p: float(v[p]) for p in POINTS}

    out_row = {
        p: counts["outputRows16"] * (cells["outputStageBaseUm2"][p] + 16 * cells["outputStageBitUm2"][p])
        + counts["outputRows32"] * (cells["outputStageBaseUm2"][p] + 32 * cells["outputStageBitUm2"][p])
        for p in POINTS
    }
    blocks = {
        "matrix circuits: serial adders": {p: counts["serialAdders"] * r("serialAdderUm2")[p] for p in POINTS},
        "matrix circuits: delay lines (scale digits)": {p: counts["delayBits"] * r("delayBitUm2")[p] for p in POINTS},
        "matrix circuits: row outputs (rounding, narrowing)": out_row,
        "KV cache rings": {p: counts["kvBits"] * r("kvBitUm2")[p] for p in POINTS},
        "attention multipliers": {p: counts["attentionMultipliers"] * r("multiplierUm2")[p] for p in POINTS},
        "tables (embedding, rsqrt, exp, sigmoid, RoPE, norms)": {
            p: counts["tableBits"] * r("tableBitUm2")[p] for p in POINTS
        },
        "core registers": {p: counts["coreFlopBits"] * r("coreFlopBitUm2")[p] for p in POINTS},
        # Measured on the proxy (dim 16); other shapes scale with the dimension (extrapolated).
        "core logic (sequencing, elementwise units, dividers, selection)": {
            p: r("coreLogicUm2PerDim")[p] * counts["dim"] for p in POINTS
        },
        "host interface and chip top": {p: r("hostUm2")[p] for p in POINTS},
    }
    blocks = {k: {p: v[p] / 1e6 for p in POINTS} for k, v in blocks.items()}
    cells_mm2 = {p: sum(b[p] for b in blocks.values()) for p in POINTS}
    util = {p: float(logic["utilization"][p]) for p in POINTS}
    power = {p: float(logic["powerOverhead"][p]) for p in POINTS}
    core = {p: cells_mm2[p] / util[p] * (1 + power[p]) for p in POINTS}
    return {"blocks": blocks, "cellsMm2": cells_mm2, "coreMm2": core, "utilization": util}


def design(spec: dict, spec_hash: str, weights_hash: str, storage: dict, profile: dict, params: dict, arrays) -> tuple:
    if arrays is None:
        raise ValueError("taalas-spatial/1 builds its circuits from the numeric weights")
    sp = Spatial(spec, arrays)
    cfg = spec["model"]["config"]
    clock = float(params.get("clockMHz", 8))
    cycles = sp.cycles_per_token()
    counts = structure(sp)
    cells = profile.get("spatialCells")
    if not cells:
        raise ValueError(f"process {profile['id']} has no measured spatial cell areas (spatialCells)")
    est = area(counts, cells, profile["logic"])
    ring = {
        p: float(profile["ring"]["ioRingWidthMm"][p]) + float(profile["ring"]["sealRingWidthMm"][p]) for p in POINTS
    }
    rules = profile.get("foundry") or profile.get("prerunConstraint")
    dies = {p: die_for_core(est["coreMm2"][p], ring[p], rules) for p in POINTS}
    # Without a die in the parameters, the smallest on the 0.1 mm grid that holds the typical core.
    suggested = {"xMm": math.ceil(dies["typ"][0] * 10 - 1e-9) / 10, "yMm": dies["typ"][1]}
    chosen = params.get("die") or suggested
    # The area limit, by the estimator's rule: a target area, else the die the parameters set,
    # else the budget at the foundry's price, else the process's prerun area.
    if params.get("targetAreaMm2"):
        limit = float(params["targetAreaMm2"])
    elif params.get("die"):
        limit = chosen["xMm"] * chosen["yMm"]
    elif params.get("budgetCny") and profile.get("foundry"):
        limit = float(params["budgetCny"]) / profile["foundry"]["priceCnyPerMm2"]
    elif rules and (rules.get("minAreaMm2") or rules.get("areaMm2")):
        limit = float(rules.get("minAreaMm2") or rules.get("areaMm2"))
    else:
        limit = float(profile.get("reticleLimitMm2") or 850)
    die_area = {p: dies[p][2] for p in POINTS}
    if die_area["low"] > limit:
        feasibility = "infeasible"
        reasons = [f"even the optimistic die ({die_area['low']:.2f} mm2) exceeds the area limit ({limit:.2f} mm2)"]
        conditions: list[str] = []
    else:
        reasons = []
        feasibility = "feasible" if die_area["high"] <= limit else "conditionally_feasible"
        conditions = (
            [
                f"placement must reach the typical utilization: typical die {die_area['typ']:.2f} mm2, "
                f"pessimistic {die_area['high']:.2f} mm2, limit {limit:.2f} mm2"
            ]
            if feasibility == "conditionally_feasible"
            else []
        )
        conditions.append(
            "cell areas are measured by synthesis of one design (the proxy); placement and routing confirm them (S6)"
        )
    ctx = sp.ctx
    # The host's frames per token at the fastest host clock (SCK = clk / 8, one bit per 8
    # cycles): START (48 bits) and STATUS (40 bits); reading the observation points (READ,
    # 24 + 32 bits per word) is optional, for verification.
    host = (48 + 40) * 8
    read = (24 + 32 * (2 * sp.layers * sp.dim + sp.vocab)) * 8
    perf = {
        "cyclesPerToken": cycles,
        # The observation registers are written in every step: no extra busy cycles; reading
        # them is the host's READ after the step (observationReadCycles).
        "cyclesPerTokenWithTraces": cycles,
        "cyclesPerPosition": [cycles] * ctx,
        "cyclesWithTracesPerPosition": [cycles] * ctx,
        "cyclesAtLastPosition": cycles,
        "tokensPerSecond": clock * 1e6 / cycles,
        "hostFrameCyclesPerToken": host,
        "tokensPerSecondWithHost": clock * 1e6 / (cycles + host),
        "observationReadCycles": read,
        "msPerToken": cycles / (clock * 1e3),
        "clockMHz": clock,
        "bottleneck": "attention (rows of the KV rings visited per pass)",
        "model": "fixed schedule of the generated state machine (llmchip_rtl.spatial); equal at every position",
        "stages": [{"stage": n, "cycles": c} for n, c in sp.schedule()],
        "macsPerToken": cfg["n_layers"] * (2 * sp.dim * sp.dim + 2 * sp.dim * sp.kv_dim + 3 * sp.dim * sp.hidden)
        + sp.vocab * sp.dim
        + 2 * cfg["n_layers"] * sp.dim * ctx,
    }
    ppa = {
        "format": "llmchip-ppa/1",
        "architecture": ARCHITECTURE,
        "area": {
            "cellsMm2": est["cellsMm2"],
            "coreMm2": est["coreMm2"],
            "dieMm2": die_area,
            "die": {p: {"xMm": round(dies[p][0], 3), "yMm": dies[p][1]} for p in POINTS},
            "chosenDie": {**chosen, "areaMm2": round(chosen["xMm"] * chosen["yMm"], 4)},
            "suggestedDie": {**suggested, "areaMm2": round(suggested["xMm"] * suggested["yMm"], 4)},
            "blocks": [{"item": k, "areaMm2": v} for k, v in est["blocks"].items()],
            "utilization": est["utilization"],
            "source": cells.get("source"),
            "confidence": cells.get("confidence"),
        },
        "structure": counts,
        "performance": perf,
        "reverse": {"areaLimitMm2": limit},
        "conclusion": {"feasibility": feasibility, "conditions": conditions, "reasons": reasons},
        "suggestions": [],
        "microarchitecture": {
            "template": ARCHITECTURE,
            "memoryInterface": "none",
            "operandBuffers": "none",
            "matrices": "bit-serial adder trees wired from the weights (one per row and layer)",
            "attentionMultipliers": counts["attentionMultipliers"],
            "dividers": sp.dividers,
            "kvCache": f"on chip: {cfg['n_layers'] * 2} rotating rings of {ctx} rows",
        },
    }
    period = 1000.0 / clock
    asynchronous = "asynchronous to clk; two-flop synchronizer on chip"
    pins = [
        {"name": "clk", "dir": "in"},
        {"name": "rst_n", "dir": "in"},
        {"name": "host_sclk", "dir": "in"},
        {"name": "host_cs_n", "dir": "in"},
        {"name": "host_mosi", "dir": "in"},
        {"name": "host_miso", "dir": "out"},
        {"name": "busy", "dir": "out"},
    ]
    timing = {
        "clock": {"port": "clk", "periodNs": round(period, 3)},
        "inputs": {
            name: {"maxNs": round(0.5 * period, 3), "minNs": 0.0, "kind": asynchronous}
            for name in ("rst_n", "host_sclk", "host_cs_n", "host_mosi")
        },
        "outputs": {
            "host_miso": {"maxNs": round(0.25 * period, 3), "minNs": 0.0},
            "busy": {"maxNs": round(0.25 * period, 3), "minNs": 0.0},
        },
        "board": {"outputLoadPf": 5.0, "inputTransitionNs": 2.0, "maxPadTransitionNs": round(0.05 * period, 3)},
        "notes": "Host SPI SCK at most clk / 8. No external memory.",
    }
    points = spec["observationPoints"]
    layers = cfg["n_layers"]
    chip = {
        "format": "llmchip-chip-spec/1",
        "architecture": ARCHITECTURE,
        "numericSpec": {"hash": spec_hash, "version": spec["version"], "format": spec["format"]},
        "weights": {"hash": weights_hash},
        "process": {"id": profile["id"], "title": profile.get("title")},
        "targets": {
            "areaLimitMm2": limit,
            "clockMHz": clock,
            "contextTokens": ctx,
            "die": ppa["area"]["chosenDie"],
            **({"cyclesPerToken": int(params["cyclesPerToken"])} if params.get("cyclesPerToken") else {}),
        },
        "function": "autoregressive token generation of the hardwired model: the host sends prompt tokens over SPI, "
        "the chip returns one greedy-decoded token per step (decoding rule of the numeric spec)",
        "dataflow": {
            "kind": "spatial: every layer's matrices are circuits (bit-serial adder trees wired from the weights); "
            "a token flows through the layers in order",
            "lanes": 0,
            "operandBuffers": "none",
            "memoryInterface": "none",
            "activations": "int16, formats from the numeric spec; in on-chip registers, bit-serial into the circuits",
            "accumulatorBits": spec["accumulator"]["bits"],
            "accumulatorCanOverflow": False,
            "rounding": sp.rounding,
            "overflow": sp.overflow,
            "nonlinear": [
                "rsqrt table (rmsnorm)",
                "exp table (softmax)",
                "sigmoid table (SiLU)",
                "RoPE cos/sin tables",
            ],
            "attention": f"shared unit: {counts['attentionMultipliers']} multipliers (both heads against one K row "
            f"per clock), {sp.dividers} exact serial dividers",
            "schedule": [f"{n}: {c} cycles" for n, c in sp.schedule()],
        },
        "storage": {
            "weights": {"implementation": "circuits (bit-serial adder trees)", "bits": storage["weights"]},
            "scales": {"implementation": "circuits (shift-add digits)", "bits": storage["scales"]},
            "norms": {"implementation": "logic tables", "bits": storage["norms"]},
            "tables": {"implementation": "logic tables", "bits": storage["luts"]},
            "kvCache": {
                "location": "onchip",
                "bytes": counts["kvBits"] // 8,
                "format": "int16",
                "interface": "rotating rings, one row per clock",
            },
        },
        "interfaces": {
            "host": {
                "kind": "SPI slave, mode 0, SCK at most clk / 8",
                "commands": {
                    "0x10": "START token[15:0] pos[15:0] mode[7:0]: run one step; ignored while busy",
                    "0x20": "STATUS: returns busy, error, done, 13 zero bits, token[15:0]",
                    "0x30": "READ addr[15:0]: returns 32-bit observation words from addr on (while idle)",
                },
            }
        },
        "timing": timing,
        "pins": pins,
        "observability": {
            "points": [
                {
                    "point": p["id"],
                    "format": p["format"],
                    "shape": p["shape"],
                    "readback": "observation registers on chip, read by the host with READ while the chip is idle",
                }
                for p in points
            ],
            "covers": [p["id"] for p in points],
            "map": {
                "attn_residual": [2 * n * sp.dim for n in range(layers)],
                "ffn_residual": [(2 * n + 1) * sp.dim for n in range(layers)],
                "logits": 2 * layers * sp.dim,
                "token": 2 * layers * sp.dim + sp.vocab,
            },
        },
        "performance": {k: v for k, v in perf.items() if not k.endswith("PerPosition")},
        "processCapabilities": ["stdcell-logic", "io-pads"],
    }
    return chip, ppa


def verify(chip: dict, spec: dict, spec_hash: str, ppa: dict, arrays) -> dict:
    """G2 evidence of a spatial chip spec: numeric spec, observation points, area, and the
    speed recomputed from the circuits the weights give (and the cycle target, if set)."""
    covered = set(chip.get("observability", {}).get("covers", []))
    needed = {p["id"] for p in spec["observationPoints"]}
    limit = chip["targets"].get("areaLimitMm2")
    die = ppa.get("area", {}).get("dieMm2", {}).get("typ")
    out = {
        "numeric_spec_matches": chip.get("numericSpec", {}).get("hash") == spec_hash,
        "observation_points_covered": needed <= covered,
        "feasibility": ppa["conclusion"]["feasibility"],
        "fits_area": die is not None and limit is not None and die <= limit,
        **({"die_area_mm2.typ": die} if die is not None else {}),
        **({"area_limit_mm2": limit} if limit is not None else {}),
        "tokens_per_second": ppa["performance"]["tokensPerSecond"],
    }
    if arrays is not None:
        exact = Spatial(spec, arrays).cycles_per_token()
        stated = chip.get("performance", {}).get("cyclesPerToken")
        out["cycles_match_model"] = stated == exact
        out["cycles_per_token.model"] = exact
        target = chip["targets"].get("cyclesPerToken")
        if target:
            out["cycles_within_target"] = exact <= target
            out["cycles_target"] = target
    return out
