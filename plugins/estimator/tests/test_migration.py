import copy
import json
from pathlib import Path

from llmchip_estimator.migration import assess

DATA = Path(__file__).parents[2] / "process-library" / "data"
GF180 = json.loads((DATA / "gf180mcu.json").read_text())
LIBRARY = json.loads((DATA / "library-180nm.json").read_text())


def target_without_rom_macro() -> tuple[dict, dict]:
    """A target process like GF180 but with no ROM macro (AC-13/14)."""
    target = copy.deepcopy(GF180) | {"id": "mock-norom"}
    library = copy.deepcopy(LIBRARY)
    for e in [
        e
        for e in LIBRARY["entries"]
        if e["process"] == "gf180mcu" and not (e["kind"] == "rom" and e["form"] == "macro")
    ]:
        library["entries"].append(e | {"id": e["id"].replace("gf180mcu", "mock-norom"), "process": "mock-norom"})
    return target, library


def chip(storage: str) -> dict:
    return {
        "processCapabilities": [f"storage:{storage}", "stdcell-logic", "io-pads"],
        "targets": {"die": {"xMm": 1.2, "yMm": 2}},
    }


def test_a_design_on_a_rom_macro_restarts_at_the_architecture_without_one():
    target, library = target_without_rom_macro()
    report = assess(chip("gf180mcu.openram-rom"), GF180, target, library)
    assert report["restartStage"] == "S3" and report["needsOwnerApproval"]
    assert any(
        d["area"] == "storage" and d["affectsDesign"] and d["restartStage"] == "S3" for d in report["differences"]
    )


def test_a_standard_cell_rom_design_is_not_affected_by_a_missing_rom_macro():
    target, library = target_without_rom_macro()
    report = assess(chip("gf180mcu.stdcell-rom"), GF180, target, library)
    # Only what the design depends on counts: here nothing before S6 (FR-MIG-02, AC-14).
    assert report["restartStage"] in (None, "S6")
    assert all(d["restartStage"] is None for d in report["differences"] if "macro" in d["difference"])
    assert not report["needsOwnerApproval"]


def test_moving_to_the_target_foundry_lists_library_io_and_signoff_differences():
    target = json.loads((DATA / "sephonchitech-180bcd.json").read_text())
    report = assess(chip("gf180mcu.stdcell-rom"), GF180, target, LIBRARY)
    areas = {d["area"] for d in report["differences"] if d["affectsDesign"]}
    assert {"stdcell", "io", "signoff"} <= areas
    assert report["restartStage"] == "S6"
