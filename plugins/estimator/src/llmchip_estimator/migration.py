"""Process migration impact assessment (requirements FR-MIG-01/02/04).

Compares the current and the target process (profile and implementation
library) and judges each difference against what the design actually depends
on: the chip spec's process capabilities (FR-ARC-01). Every difference gets an
impact level and the earliest stage that has to run again; the report's
restart stage is the earliest of them. Differences the design does not depend
on are listed as not affecting it, so they never invalidate anything.
"""

from __future__ import annotations

from typing import Any

STAGES = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]


def earliest(stages: list[str]) -> str | None:
    return min(stages, key=STAGES.index) if stages else None


def assess(chip: dict, current: dict, target: dict, library: dict) -> dict[str, Any]:
    entries = {e["id"]: e for e in library["entries"]}
    tech_c, tech_t = current.get("technology") or {}, target.get("technology") or {}
    deps = set(chip.get("processCapabilities", []))
    diffs: list[dict[str, Any]] = []

    def add(area: str, what: str, impact: str, restart: str | None, affects: bool, detail: str) -> None:
        diffs.append(
            {
                "area": area,
                "difference": what,
                "impact": impact,
                "restartStage": restart if affects else None,
                "affectsDesign": affects,
                "detail": detail,
            }
        )

    # Storage implementations the architecture chose (FR-ARC-01 dependencies).
    for dep in sorted(d for d in deps if d.startswith("storage:")):
        impl = entries.get(dep.split(":", 1)[1])
        if impl is None:
            add("storage", dep, "high", "S3", True, "the implementation is not in the library")
            continue
        candidates = [
            e
            for e in library["entries"]
            if e.get("process") == target["id"]
            and e.get("kind") == impl.get("kind")
            and e.get("form") == impl.get("form")
            and e.get("availability") != "unavailable"
        ]
        if impl.get("form") == "macro":
            if not candidates:
                add(
                    "storage",
                    f"{impl['kind']} macro",
                    "high",
                    "S3",
                    True,
                    f"the design keeps its weights in {impl['title']}; the target offers no {impl['kind']} macro, "
                    "so the storage architecture must change",
                )
            else:
                pending = all(c.get("availability") == "pending" for c in candidates)
                add(
                    "storage",
                    f"{impl['kind']} macro",
                    "medium" if pending else "low",
                    "S4",
                    True,
                    f"target macro {candidates[0]['id']} ({candidates[0].get('availability')}): "
                    "regenerate the storage mapping and views",
                )
        else:
            # Standard-cell logic ROM: RTL is process independent; only the implementation reruns.
            add(
                "storage",
                "standard-cell ROM",
                "low",
                "S6",
                True,
                "weights are standard-cell logic: synthesis and layout with the target library",
            )
    # Macros of the target the design does not use: reported, never restarting anything.
    used_forms = {
        (entries[d.split(":", 1)[1]].get("kind"), entries[d.split(":", 1)[1]].get("form"))
        for d in deps
        if d.startswith("storage:") and d.split(":", 1)[1] in entries
    }
    for kind in ("rom", "sram"):
        cur = [
            e
            for e in library["entries"]
            if e.get("process") == current["id"]
            and e.get("kind") == kind
            and e.get("form") == "macro"
            and e.get("availability") == "available"
        ]
        tgt = [
            e
            for e in library["entries"]
            if e.get("process") == target["id"]
            and e.get("kind") == kind
            and e.get("form") == "macro"
            and e.get("availability") != "unavailable"
        ]
        if bool(cur) != bool(tgt) and (kind, "macro") not in used_forms:
            add(
                "storage",
                f"{kind} macro availability",
                "none",
                None,
                False,
                f"{kind} macros: current {'yes' if cur else 'no'}, target {'yes' if tgt else 'no'}; "
                "the design does not use one",
            )

    if "stdcell-logic" in deps:
        sc, st = tech_c.get("stdcell") or {}, tech_t.get("stdcell") or {}
        for key, label in (
            ("library", "standard-cell library"),
            ("voltageV", "core voltage"),
            ("tracks", "track height"),
            ("corners", "timing corners"),
        ):
            if sc.get(key) != st.get(key):
                add(
                    "stdcell",
                    label,
                    "medium",
                    "S6",
                    True,
                    f"{sc.get(key)} -> {st.get(key) if st.get(key) is not None else 'pending'}",
                )
    if "io-pads" in deps:
        ic, it = tech_c.get("io") or {}, tech_t.get("io") or {}
        for key, label in (("library", "IO library"), ("padUm", "pad size"), ("voltageV", "IO voltage")):
            if ic.get(key) != it.get(key):
                add(
                    "io",
                    label,
                    "medium",
                    "S6",
                    True,
                    f"{ic.get(key)} -> {it.get(key) if it.get(key) is not None else 'pending'}",
                )
    for key, label in (
        ("metalLayers", "metal layers"),
        ("voltageDomains", "voltage domains"),
        ("parasitics", "parasitic corners"),
    ):
        if tech_c.get(key) != tech_t.get(key):
            add(
                "process",
                label,
                "medium",
                "S6",
                True,
                f"{tech_c.get(key)} -> {tech_t.get(key) if tech_t.get(key) is not None else 'pending'}",
            )
    so_c, so_t = tech_c.get("signoff") or {}, tech_t.get("signoff") or {}
    if so_c.get("openTools") and not so_t.get("openTools"):
        add(
            "signoff",
            "rule file formats and tools",
            "medium",
            "S7",
            True,
            "the target's rule decks may not run in open tools: check which signoff checks still run",
        )
    # The die must follow the target's size rules (architecture decides the die).
    die = (chip.get("targets") or {}).get("die")
    foundry = target.get("foundry") or {}
    if die and foundry:
        ok = (not foundry.get("dieYOptionsMm") or die["yMm"] in foundry["dieYOptionsMm"]) and die["xMm"] >= foundry.get(
            "dieXMinMm", 0
        )
        area_ok = die["xMm"] * die["yMm"] >= foundry.get("minAreaMm2", 0)
        if not (ok and area_ok):
            add(
                "die",
                "die size rules",
                "high",
                "S3",
                True,
                f"die {die['xMm']} x {die['yMm']} mm does not follow the target's size rules",
            )
    affecting = [d for d in diffs if d["affectsDesign"]]
    restart = earliest([d["restartStage"] for d in affecting if d["restartStage"]])
    return {
        "format": "llmchip-migration-report/1",
        "from": current["id"],
        "to": target["id"],
        "dependencies": sorted(deps),
        "differences": diffs,
        "restartStage": restart,
        "needsOwnerApproval": restart is not None and STAGES.index(restart) <= STAGES.index("S3"),
    }
