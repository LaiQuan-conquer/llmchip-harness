"""The spatial template (taalas-spatial/1): speed from the fixed schedule of the circuits, area
from their structure and the measured cell areas, die from the profile's size rules."""

import io
import json
from pathlib import Path

import numpy as np
import pytest
from llmchip_arch import design, verify
from llmchip_model import Config, Tokenizer, load_llama2c
from llmchip_model.evalset import load_evalset
from llmchip_model.proxy import char_tokenizer, random_checkpoint
from llmchip_numeric import build, storage_bits
from llmchip_rtl.spatial.generator import Spatial

ROOT = Path(__file__).parents[3]
DATA = ROOT / "plugins" / "process-library" / "data"
PROXY = Config(dim=16, hidden_dim=32, n_layers=2, n_heads=2, n_kv_heads=1, vocab_size=35, seq_len=64)


@pytest.fixture(scope="module")
def proxy():
    cfg, w = load_llama2c(random_checkpoint(PROXY, 1))
    tok = Tokenizer(char_tokenizer(), cfg.vocab_size)
    prompts = load_evalset(ROOT / "plugins/model-llama/data/evalset-proxy.json")["calibration"]
    spec, npz = build(cfg, w, tok, prompts, weight_format="ternary", group_size=16)
    with np.load(io.BytesIO(npz)) as z:
        arrays = {k: z[k].astype(np.int64) for k in z.files if k != "meta"}
    return spec, arrays


def run(spec, arrays, **params):
    profile = json.loads((DATA / "gf180mcu.json").read_text())
    library = json.loads((DATA / "library-180nm.json").read_text())
    p = {"weightStorage": "gf180mcu.stdcell-rom", "architecture": "taalas-spatial/1", "clockMHz": 8, **params}
    return design(spec, "a" * 64, "b" * 64, storage_bits(spec, arrays), profile, library, p, arrays)


def test_speed_is_the_schedule_of_the_circuits_and_exceeds_one_thousand_tokens_per_second(proxy):
    chip, ppa = run(*proxy)
    cycles = Spatial(*proxy).cycles_per_token()
    assert chip["architecture"] == "taalas-spatial/1"
    assert chip["performance"]["cyclesPerToken"] == cycles <= 8000
    assert ppa["performance"]["cyclesWithTracesPerPosition"] == [cycles] * 64
    assert chip["performance"]["tokensPerSecond"] == pytest.approx(8e6 / cycles)
    assert chip["performance"]["tokensPerSecond"] > 1000
    assert sum(s["cycles"] for s in ppa["performance"]["stages"]) + 1 == cycles


def test_no_external_memory_and_every_pin_is_timed(proxy):
    chip, _ = run(*proxy)
    names = {p["name"] for p in chip["pins"]}
    assert not any(n.startswith("kv_") for n in names)
    timed = set(chip["timing"]["inputs"]) | set(chip["timing"]["outputs"]) | {chip["timing"]["clock"]["port"]}
    assert timed == names
    assert chip["storage"]["kvCache"]["location"] == "onchip"
    assert "0x30" in chip["interfaces"]["host"]["commands"]
    assert {r["point"] for r in chip["observability"]["points"]} == {p["id"] for p in proxy[0]["observationPoints"]}


def test_area_follows_the_measured_cells_and_picks_a_die_within_the_size_rules(proxy):
    _, ppa = run(*proxy)
    area = ppa["area"]
    blocks = {b["item"]: b["areaMm2"]["typ"] for b in area["blocks"]}
    assert sum(blocks.values()) == pytest.approx(area["cellsMm2"]["typ"])
    # Synthesis of this design measured 5.96 mm2 of cells (2026-10-21).
    assert area["cellsMm2"]["typ"] == pytest.approx(5.96, abs=0.15)
    assert blocks["KV cache rings"] == pytest.approx(32768 * 63.66e-6)
    die = area["suggestedDie"]
    assert die["yMm"] in (2, 3, 5) and die["xMm"] >= 1
    assert die["xMm"] * die["yMm"] >= area["dieMm2"]["typ"] - 1e-9
    assert area["coreMm2"]["low"] < area["coreMm2"]["typ"] < area["coreMm2"]["high"]


def test_verify_recomputes_the_cycles_and_judges_the_area_against_the_chosen_die(proxy):
    spec, arrays = proxy
    # Without a die the area limit is the profile's prerun area (2.4 mm2): it does not fit.
    _, unset = run(spec, arrays)
    assert unset["reverse"]["areaLimitMm2"] == 2.4 and unset["conclusion"]["feasibility"] == "infeasible"
    die = unset["area"]["suggestedDie"]
    chip, ppa = run(spec, arrays, cyclesPerToken=8000, die={"xMm": die["xMm"], "yMm": die["yMm"]})
    assert ppa["reverse"]["areaLimitMm2"] == pytest.approx(die["areaMm2"])
    m = verify(chip, spec, "a" * 64, ppa, arrays)
    assert m["numeric_spec_matches"] and m["observation_points_covered"] and m["fits_area"]
    assert m["cycles_match_model"] and m["cycles_within_target"]
    tampered = json.loads(json.dumps(chip))
    tampered["performance"]["cyclesPerToken"] -= 1
    assert verify(tampered, spec, "a" * 64, ppa, arrays)["cycles_match_model"] is False
    # The prerun die of the vector engine (1.2 x 2 mm) cannot hold the spatial engine.
    small, small_ppa = run(spec, arrays, die={"xMm": 1.2, "yMm": 2})
    assert small_ppa["conclusion"]["feasibility"] == "infeasible"
    assert verify(small, spec, "a" * 64, small_ppa, arrays)["fits_area"] is False


def test_the_old_template_is_unchanged_by_default(proxy):
    spec, arrays = proxy
    profile = json.loads((DATA / "gf180mcu.json").read_text())
    library = json.loads((DATA / "library-180nm.json").read_text())
    p = {"weightStorage": "gf180mcu.stdcell-rom", "parallelMacs": 2, "clockMHz": 8, "memoryInterface": "sqi"}
    chip, _ = design(spec, "a" * 64, "b" * 64, storage_bits(spec, arrays), profile, library, p, arrays)
    assert chip["architecture"] == "taalas-hardwired/0.2"
