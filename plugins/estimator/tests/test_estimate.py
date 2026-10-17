import json
from pathlib import Path

import pytest
from llmchip_estimator.estimate import MODEL_PRESETS, estimate, resolve_model

DATA = Path(__file__).parents[2] / "process-library" / "data"
PROFILE = json.loads((DATA / "sephonchitech-180bcd.json").read_text())
GF180 = json.loads((DATA / "gf180mcu.json").read_text())
LIBRARY = json.loads((DATA / "library-180nm.json").read_text())


def run(model, profile=PROFILE, **params):
    storage = params.pop(
        "weightStorage", f"{profile['id']}.foundry-rom" if profile is PROFILE else "gf180mcu.stdcell-rom"
    )
    return estimate({"model": model, "weightStorage": storage, **params}, profile, LIBRARY)


def test_structure_reproduces_the_stories260k_parameter_count():
    preset = MODEL_PRESETS["stories260K"]
    derived = resolve_model({k: preset[k] for k in ("dim", "layers", "heads", "kvHeads", "vocab", "hiddenDim")})
    assert derived["params"] == preset["params"]


def test_stories260k_breakdown_cost_and_performance():
    report = run({"preset": "stories260K"})
    items = {i["item"] for i in report["items"]}
    assert {
        "weight storage",
        "KV cache (off chip)",
        "activation buffers",
        "compute units",
        "control and interfaces",
        "logic placement and routing (utilization)",
        "power network",
        "IO ring, seal ring and unused die area",
    } <= items
    weights = next(i for i in report["items"] if i["item"] == "weight storage")
    assert weights["areaMm2"]["typ"] == pytest.approx(260_032 * 4 * 0.5 * 1.2 / 1e6)
    assert report["cost"]["cny"]["low"] >= 10_000
    assert report["performance"]["bottleneck"] in ("compute", "off-chip interface")
    assert report["conclusion"]["feasibility"] in ("feasible", "conditionally_feasible", "infeasible")
    # The foundry ROM is not confirmed, so the result cannot be unconditionally feasible.
    assert report["conclusion"]["feasibility"] != "feasible"


@pytest.mark.parametrize("preset", ["qwen2.5-0.5b", "llama3.1-8b"])
def test_large_models_do_not_fit_one_180nm_die(preset):
    report = run({"preset": preset})
    assert report["conclusion"]["feasibility"] == "infeasible"
    assert report["multiChip"]["chipsAtReticle"]["typ"] >= 1
    assert any("smaller model" in s for s in report["suggestions"])


def test_missing_values_give_insufficient_data_never_zero():
    profile = json.loads(json.dumps(PROFILE))
    profile["ring"]["ioRingWidthMm"] = None
    report = run({"preset": "stories260K"}, profile=profile, weightStorage="sephonchitech-180bcd.foundry-rom")
    assert report["conclusion"]["feasibility"] == "insufficient_data"
    assert report["missing"] == [{"parameter": "ring.ioRingWidthMm", "howToObtain": "IO library from the foundry"}]


def test_reverse_estimate_grows_with_fewer_bits():
    reverse = run({"preset": "stories260K"})["reverse"]["maxParams"]
    assert reverse["2"]["typ"] > reverse["4"]["typ"] > reverse["8"]["typ"]


def test_prerun_process_has_no_foundry_cost():
    report = run({"preset": "stories260K"}, profile=GF180)
    assert report["cost"]["cny"] is None
    assert report["area"]["dieMm2"]["typ"] > 0


def test_onchip_kv_cache_uses_its_storage_entry():
    report = run(
        {"preset": "stories260K"}, kvLocation="onchip", kvStorage="sephonchitech-180bcd.foundry-sram", contextLength=64
    )
    kv = next(i for i in report["items"] if i["item"] == "KV cache (on chip)")
    assert kv["areaMm2"]["typ"] == pytest.approx(5 * 2 * 32 * 64 * 8 * 5 / 1e6)
