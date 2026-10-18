"""Feasibility, area, cost and performance estimator (requirements FR-EST, S0).

Every quantity is a range ``{low, typ, high}`` where ``low`` is the optimistic
end (smallest area, lowest cost) and ``high`` the pessimistic end. Every input
value carries its source and confidence (empirical, datasheet, calibrated,
measured); missing values are never treated as zero but reported, which turns
the conclusion into ``insufficient_data`` (FR-EST-02, FR-EST-05, FR-EST-09).
"""

from __future__ import annotations

import math
from typing import Any

POINTS = ("low", "typ", "high")

# Llama-family model presets. `params` counts every weight, including embeddings.
MODEL_PRESETS: dict[str, dict[str, Any]] = {
    "stories260K": {
        "title": "llama2.c stories260K",
        "params": 260_032,
        "dim": 64,
        "layers": 5,
        "heads": 8,
        "kvHeads": 4,
        "vocab": 512,
        "hiddenDim": 172,
        "maxContext": 512,
        "source": "karpathy/llama2.c model table; hidden_dim derived from llama2.c's formula (multiple_of 4)",
    },
    "qwen2.5-0.5b": {
        "title": "Qwen2.5 0.5B",
        "params": 494_032_768,
        "dim": 896,
        "layers": 24,
        "heads": 14,
        "kvHeads": 2,
        "vocab": 151_936,
        "hiddenDim": 4864,
        "maxContext": 32_768,
        "source": "Qwen2.5-0.5B config.json",
    },
    "llama3.1-8b": {
        "title": "Llama 3.1 8B",
        "params": 8_030_261_248,
        "dim": 4096,
        "layers": 32,
        "heads": 32,
        "kvHeads": 8,
        "vocab": 128_256,
        "hiddenDim": 14_336,
        "maxContext": 131_072,
        "source": "Llama-3.1-8B config.json",
    },
}


class Missing(Exception):
    """A required value is unknown."""

    def __init__(self, what: str, how: str) -> None:
        super().__init__(what)
        self.what = what
        self.how = how


def rng(value: Any, what: str, how: str) -> dict[str, float]:
    """A {low, typ, high} range from a profile value, or Missing."""
    if not isinstance(value, dict) or any(not isinstance(value.get(p), (int, float)) for p in POINTS):
        raise Missing(what, how)
    return {p: float(value[p]) for p in POINTS}


def scale(r: dict[str, float], factor: float) -> dict[str, float]:
    return {p: r[p] * factor for p in POINTS}


def add(*ranges: dict[str, float]) -> dict[str, float]:
    return {p: sum(r[p] for r in ranges) for p in POINTS}


def mul(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    return {p: a[p] * b[p] for p in POINTS}


def const(x: float) -> dict[str, float]:
    return dict.fromkeys(POINTS, float(x))


def resolve_model(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("preset"):
        if spec["preset"] not in MODEL_PRESETS:
            raise ValueError(f"unknown model preset {spec['preset']}; known: {', '.join(MODEL_PRESETS)}")
        return {**MODEL_PRESETS[spec["preset"]], **{k: v for k, v in spec.items() if k != "preset"}}
    model = dict(spec)
    structural = all(k in model for k in ("dim", "layers", "heads", "kvHeads", "vocab", "hiddenDim"))
    if "params" not in model and structural:
        dim, kv_dim = model["dim"], model["dim"] * model["kvHeads"] // model["heads"]
        per_layer = 2 * dim * dim + 2 * dim * kv_dim + 3 * dim * model["hiddenDim"] + 2 * dim
        model["params"] = model["vocab"] * dim + model["layers"] * per_layer + dim
    if "params" not in model:
        raise ValueError("model needs a preset, a params count, or the full structure")
    model.setdefault("title", "custom model")
    model.setdefault("source", "user input")
    return model


def implementation(library: dict[str, Any], entry_id: str | None) -> dict[str, Any]:
    for entry in library.get("entries", []):
        if entry["id"] == entry_id:
            return entry
    raise ValueError(f"implementation {entry_id} is not in library {library.get('id')}")


def die_for_core(core_mm2: float, ring_mm: float, rules: dict[str, Any] | None) -> tuple[float, float, float]:
    """Smallest die (x, y, area) that fits the core under the size rules; square die without rules."""
    if not rules or not rules.get("dieYOptionsMm"):
        side = math.sqrt(core_mm2) + 2 * ring_mm
        return side, side, side * side
    best: tuple[float, float, float] | None = None
    for y in rules["dieYOptionsMm"]:
        core_h = y - 2 * ring_mm
        if core_h <= 0:
            continue
        x = max(core_mm2 / core_h + 2 * ring_mm, float(rules.get("dieXMinMm") or 0))
        if best is None or x * y < best[2]:
            best = (x, y, x * y)
    if best is None:
        raise ValueError("no die size option can hold the IO ring")
    return best


def max_core_for_area(area_mm2: float, ring_mm: float, rules: dict[str, Any] | None) -> float:
    """Largest core area whose die fits within area_mm2 under the size rules."""
    if not rules or not rules.get("dieYOptionsMm"):
        side = math.sqrt(area_mm2) - 2 * ring_mm
        return max(side, 0.0) ** 2
    best = 0.0
    for y in rules["dieYOptionsMm"]:
        x = area_mm2 / y
        if x < float(rules.get("dieXMinMm") or 0):
            continue
        best = max(best, max(x - 2 * ring_mm, 0.0) * max(y - 2 * ring_mm, 0.0))
    return best


def apply_calibrations(library: dict[str, Any], calibrations: list[dict[str, Any]]) -> dict[str, Any]:
    """A copy of the library with calibrated values replacing empirical ones (FR-EST-13)."""
    by_id = {c["implementation"]: c for c in calibrations}
    entries = []
    for entry in library.get("entries", []):
        calibration = by_id.get(entry["id"])
        if calibration:
            entry = {**entry, "availability": calibration["availability"]}
            if calibration["availability"] != "available":
                missing = [f"{stage}: {', '.join(v)}" for stage, v in calibration.get("missingViews", {}).items() if v]
                reason = calibration.get("failure") or f"missing views ({'; '.join(missing)})"
                entry["availabilityNote"] = f"calibration found it unavailable: {reason}"
            if calibration.get("densityUm2PerBit"):
                entry["densityUm2PerBit"] = calibration["densityUm2PerBit"]
                entry["peripheryFactor"] = calibration.get("peripheryFactor", entry["peripheryFactor"])
        entries.append(entry)
    return {**library, "entries": entries}


def estimate(params: dict[str, Any], profile: dict[str, Any], library: dict[str, Any]) -> dict[str, Any]:
    model = resolve_model(params["model"])
    weight_bits = int(params.get("weightBits", 4))
    act_bits = int(params.get("activationBits", 8))
    kv_bits = int(params.get("kvBits", 8))
    context = int(params.get("contextLength", 64))
    kv_location = params.get("kvLocation", "offchip")
    parallel = int(params.get("parallelMacs", 8))
    clock_mhz = float(params.get("clockMHz", 50))
    bandwidth = float(params.get("offchipBandwidthMBps", 25))

    missing: list[dict[str, str]] = []
    conditions: list[str] = []
    items: list[dict[str, Any]] = []

    def item(name: str, compute) -> dict[str, float] | None:
        try:
            area, source, confidence = compute()
        except Missing as m:
            missing.append({"parameter": m.what, "howToObtain": m.how})
            items.append({"item": name, "areaMm2": None, "source": "missing", "confidence": None})
            return None
        items.append({"item": name, "areaMm2": area, "source": source, "confidence": confidence})
        return area

    used_impls: list[dict[str, Any]] = []

    def storage(bits: float, entry_id: str | None, what: str):
        entry = implementation(library, entry_id)
        if entry["process"] != profile["id"]:
            raise ValueError(f"implementation {entry_id} belongs to process {entry['process']}, not {profile['id']}")
        used_impls.append(entry)
        density = rng(
            entry.get("densityUm2PerBit"), f"{entry_id}.densityUm2PerBit", "run storage calibration (FR-EST-13)"
        )
        periphery = rng(
            entry.get("peripheryFactor"), f"{entry_id}.peripheryFactor", "run storage calibration (FR-EST-13)"
        )
        area = scale(mul(density, add(const(1), periphery)), bits / 1e6)
        return area, f"{entry['title']} ({what})", entry["densityUm2PerBit"].get("confidence", "empirical")

    logic = profile.get("logic") or {}
    weight_total_bits = model["params"] * weight_bits
    weights = item(
        "weight storage", lambda: storage(weight_total_bits, params.get("weightStorage"), f"{weight_total_bits:,} bit")
    )

    kv_dim = model["dim"] * model["kvHeads"] // model["heads"] if "dim" in model else None
    kv_bytes_per_token = model["layers"] * 2 * kv_dim * kv_bits / 8 if kv_dim else None
    if kv_location == "onchip":
        if kv_bytes_per_token is None:
            missing.append(
                {
                    "parameter": "model structure (layers, dim, heads, kvHeads)",
                    "howToObtain": "provide the model structure",
                }
            )
            kv = None
        else:
            kv = item(
                "KV cache (on chip)",
                lambda: storage(kv_bytes_per_token * 8 * context, params.get("kvStorage"), f"{context} tokens"),
            )
    else:
        kv = const(0)
        items.append(
            {
                "item": "KV cache (off chip)",
                "areaMm2": kv,
                "source": "external SPI SRAM on the test board",
                "confidence": "n/a",
            }
        )

    def activations():
        flop = rng(
            logic.get("flopBitAreaUm2"), "logic.flopBitAreaUm2", "standard-cell library datasheet or calibration"
        )
        if "dim" not in model:
            raise Missing("model structure (dim, hiddenDim, heads)", "provide the model structure")
        bits = (6 * model["dim"] + 2 * model.get("hiddenDim", 4 * model["dim"]) + model["heads"] * context) * act_bits
        return (
            scale(flop, bits / 1e6),
            f"activation and attention-score buffers ({bits:,} bit in flip-flops)",
            logic["flopBitAreaUm2"].get("confidence"),
        )

    def compute_units():
        mac = rng(logic.get("macAreaUm2"), "logic.macAreaUm2", "synthesize one MAC unit (FR-EST-13)")
        return scale(mac, parallel / 1e6), f"{parallel} MAC units", logic["macAreaUm2"].get("confidence")

    def control():
        r = rng(logic.get("controlAreaMm2"), "logic.controlAreaMm2", "synthesize the control template")
        return r, "sequencer, interfaces, debug readback", logic["controlAreaMm2"].get("confidence")

    act = item("activation buffers", activations)
    mac = item("compute units", compute_units)
    ctl = item("control and interfaces", control)

    try:
        util = rng(logic.get("utilization"), "logic.utilization", "placement experiments")
        power = rng(logic.get("powerOverhead"), "logic.powerOverhead", "power grid design")
        ring = add(
            rng((profile.get("ring") or {}).get("ioRingWidthMm"), "ring.ioRingWidthMm", "IO library from the foundry"),
            rng((profile.get("ring") or {}).get("sealRingWidthMm"), "ring.sealRingWidthMm", "foundry design rules"),
        )
    except Missing as m:
        missing.append({"parameter": m.what, "howToObtain": m.how})
        util = power = ring = None

    result: dict[str, Any] = {
        "model": {k: model[k] for k in model if k != "preset"},
        "assumptions": {
            "weightBits": weight_bits,
            "activationBits": act_bits,
            "kvBits": kv_bits,
            "contextLength": context,
            "kvLocation": kv_location,
            "parallelMacs": parallel,
            "clockMHz": clock_mhz,
            "offchipBandwidthMBps": bandwidth,
            "weightStorage": params.get("weightStorage"),
            "kvStorage": params.get("kvStorage"),
        },
        "process": {"id": profile["id"], "title": profile.get("title"), "source": profile.get("source")},
        "items": items,
        "missing": missing,
    }

    # Performance (FR-EST-07): one MAC per weight plus attention over the context.
    attn_macs = 2 * model["layers"] * model["dim"] * context if "dim" in model else 0
    macs = model["params"] + attn_macs
    cycles = math.ceil(macs / parallel * 1.1)
    compute_ms = cycles / (clock_mhz * 1e3)
    perf: dict[str, Any] = {"macsPerToken": macs, "cyclesPerToken": cycles, "computeMsPerToken": compute_ms}
    if kv_location == "offchip" and kv_bytes_per_token is not None:
        traffic = kv_bytes_per_token * (context + 1)
        io_ms = traffic / (bandwidth * 1e3)
        perf |= {"offchipBytesPerToken": traffic, "offchipMsPerToken": io_ms}
        token_ms = max(compute_ms, io_ms)
        perf["bottleneck"] = "off-chip interface" if io_ms > compute_ms else "compute"
    else:
        token_ms = compute_ms
        perf["bottleneck"] = "compute"
    perf |= {"msPerToken": token_ms, "tokensPerSecond": 1000 / token_ms}
    result["performance"] = perf

    if missing or util is None:
        result["conclusion"] = {
            "feasibility": "insufficient_data",
            "conditions": [],
            "reasons": ["required values are missing"],
        }
        result["suggestions"] = []
        return result

    logic_area = {p: (act[p] + mac[p] + ctl[p]) / util[p] for p in POINTS}
    core = {p: (weights[p] + kv[p] + logic_area[p]) * (1 + power[p]) for p in POINTS}
    items.append(
        {
            "item": "logic placement and routing (utilization)",
            "areaMm2": {p: logic_area[p] - act[p] - mac[p] - ctl[p] for p in POINTS},
            "source": "utilization factor",
            "confidence": logic["utilization"].get("confidence"),
        }
    )
    items.append(
        {
            "item": "power network",
            "areaMm2": {p: core[p] - core[p] / (1 + power[p]) for p in POINTS},
            "source": "power overhead factor",
            "confidence": logic["powerOverhead"].get("confidence"),
        }
    )

    foundry = profile.get("foundry")
    rules = foundry or profile.get("prerunConstraint")
    dies = {p: die_for_core(core[p], ring[p], rules) for p in POINTS}
    die = {p: dies[p][2] for p in POINTS}
    items.append(
        {
            "item": "IO ring, seal ring and unused die area",
            "areaMm2": {p: die[p] - core[p] for p in POINTS},
            "source": "ring widths",
            "confidence": profile["ring"]["ioRingWidthMm"].get("confidence"),
        }
    )
    result["area"] = {
        "coreMm2": core,
        "dieMm2": die,
        "die": {p: {"xMm": round(dies[p][0], 3), "yMm": dies[p][1]} for p in POINTS},
    }

    reticle = float(profile.get("reticleLimitMm2") or 850)
    limit = params.get("targetAreaMm2")
    if limit is None and params.get("budgetCny") and foundry:
        limit = float(params["budgetCny"]) / foundry["priceCnyPerMm2"]
    if limit is None and rules:
        limit = rules.get("minAreaMm2") or rules.get("areaMm2")
    limit = float(limit) if limit else reticle

    if foundry:
        billed = {p: max(die[p], foundry["minAreaMm2"]) for p in POINTS}
        result["cost"] = {
            "billedAreaMm2": billed,
            "cny": {p: billed[p] * foundry["priceCnyPerMm2"] for p in POINTS},
            "rule": f"{foundry['priceCnyPerMm2']} CNY/mm2, minimum {foundry['minAreaMm2']} mm2",
            "notCovered": [
                "packaging and bonding",
                "test board",
                "shipping",
                "tax (pending)",
                "NDA, PDK or IP fees (pending)",
                *foundry.get("pending", []),
            ],
        }
    else:
        result["cost"] = {
            "billedAreaMm2": None,
            "cny": None,
            "rule": "no foundry pricing (prerun process)",
            "notCovered": [],
        }

    # Multi-chip estimate when one die cannot hold the design (FR-EST-06, informative).
    if die["low"] > reticle:
        reticle_core = max_core_for_area(reticle, ring["typ"], None)
        result["multiChip"] = {
            "chipsAtReticle": {p: math.ceil(core[p] / reticle_core) for p in POINTS},
            "reticleMm2": reticle,
        }

    # Reverse estimate (FR-EST-04): parameters that fit the area limit.
    weight_entry = implementation(library, params.get("weightStorage"))
    density = rng(weight_entry["densityUm2PerBit"], "density", "")
    periphery = rng(weight_entry["peripheryFactor"], "periphery", "")
    reverse: dict[str, Any] = {"areaLimitMm2": limit, "maxParams": {}}
    for bits in (2, 3, 4, 8):
        fits = {}
        for p in POINTS:
            available = max_core_for_area(limit, ring[p], rules) / (1 + power[p]) - kv[p] - logic_area[p]
            fits[p] = max(int(available * 1e6 / (density[p] * (1 + periphery[p]) * bits)), 0)
        reverse["maxParams"][str(bits)] = fits
    result["reverse"] = reverse

    # Conclusion (FR-EST-05).
    reasons: list[str] = []
    if die["low"] > reticle:
        feasibility = "infeasible"
        reasons.append(f"even the optimistic die ({die['low']:.0f} mm2) exceeds the reticle limit ({reticle:.0f} mm2)")
    elif die["low"] > limit:
        feasibility = "infeasible"
        reasons.append(f"even the optimistic die ({die['low']:.2f} mm2) exceeds the area limit ({limit:.2f} mm2)")
    else:
        feasibility = "feasible" if die["high"] <= limit else "conditionally_feasible"
        if die["high"] > limit:
            conditions.append(
                "actual densities must stay near the typical/optimistic end: "
                f"typical die {die['typ']:.2f} mm2, pessimistic {die['high']:.2f} mm2, limit {limit:.2f} mm2"
            )
        for entry in used_impls:
            if entry["availability"] == "unavailable":
                feasibility = "infeasible"
                reasons.append(f"{entry['title']} is unavailable: {entry.get('availabilityNote', '')}")
                continue
            if entry["availability"] != "available":
                conditions.append(
                    f"{entry['title']} must be available: {entry.get('availabilityNote', entry['availability'])}"
                )
                feasibility = "conditionally_feasible"
            if entry["densityUm2PerBit"].get("confidence") == "empirical":
                conditions.append(
                    f"{entry['title']} density is empirical; confirm with storage calibration (FR-EST-13)"
                )
                feasibility = "conditionally_feasible"
    if feasibility == "infeasible":
        conditions = []  # Conditions only qualify a feasible result.
    result["conclusion"] = {"feasibility": feasibility, "conditions": conditions, "reasons": reasons}

    suggestions = []
    if feasibility in ("infeasible", "conditionally_feasible"):
        if weight_bits > 2:
            suggestions.append("lower the weight bit width (e.g. 2-bit or ternary)")
        if kv_location == "onchip":
            suggestions.append("move the KV cache off chip, or shorten the context")
        if feasibility == "infeasible":
            suggestions.append(
                f"use a smaller model: about {reverse['maxParams'][str(weight_bits)]['typ']:,} parameters "
                f"fit at {weight_bits} bit"
            )
            suggestions.append("increase the die area or budget, or use a more advanced process node")
    result["suggestions"] = suggestions
    return result


def summary_markdown(report: dict[str, Any]) -> str:
    def fmt(r: Any, digits: int = 3) -> str:
        if r is None:
            return "missing"
        return " / ".join(f"{r[p]:.{digits}f}" for p in POINTS)

    c = report["conclusion"]
    lines = [
        f"# Feasibility estimate: {report['model']['title']} on {report['process']['title']}",
        "",
        f"**Conclusion: {c['feasibility']}**",
        "",
        *[f"- reason: {r}" for r in c["reasons"]],
        *[f"- condition: {x}" for x in c["conditions"]],
        "",
        "## Area (mm2, optimistic / typical / pessimistic)",
        "",
        "| Item | Area | Source | Confidence |",
        "|---|---|---|---|",
        *[f"| {i['item']} | {fmt(i['areaMm2'])} | {i['source']} | {i['confidence']} |" for i in report["items"]],
    ]
    if "area" in report:
        lines += ["", f"Core: {fmt(report['area']['coreMm2'])} mm2; die: {fmt(report['area']['dieMm2'])} mm2"]
    if report.get("cost", {}).get("cny"):
        lines += [
            "",
            "## Cost",
            "",
            f"{fmt(report['cost']['cny'], 0)} CNY ({report['cost']['rule']})",
            "",
            "Not covered: " + "; ".join(report["cost"]["notCovered"]),
        ]
    perf = report["performance"]
    lines += [
        "",
        "## Performance",
        "",
        f"{perf['cyclesPerToken']:,} cycles/token, {perf['msPerToken']:.2f} ms/token, "
        f"{perf['tokensPerSecond']:.1f} tokens/s; bottleneck: {perf['bottleneck']}",
    ]
    if report["missing"]:
        lines += ["", "## Missing data", "", *[f"- {m['parameter']}: {m['howToObtain']}" for m in report["missing"]]]
    if report.get("suggestions"):
        lines += ["", "## Suggestions", "", *[f"- {s}" for s in report["suggestions"]]]
    return "\n".join(lines) + "\n"
