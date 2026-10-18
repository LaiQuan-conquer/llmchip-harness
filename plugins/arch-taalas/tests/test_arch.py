import json
from pathlib import Path

import pytest
from llmchip_arch import design, verify
from llmchip_model import Tokenizer, load_llama2c
from llmchip_model.evalset import load_evalset
from llmchip_numeric import L2Model, build, storage_bits

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "models" / "stories260K"
DATA = ROOT / "plugins" / "process-library" / "data"


@pytest.fixture(scope="module")
def l2(tmp_path_factory):
    cfg, w = load_llama2c((FIXTURE / "stories260K.bin").read_bytes())
    tok = Tokenizer((FIXTURE / "tok512.bin").read_bytes(), cfg.vocab_size)
    spec, npz = build(cfg, w, tok, load_evalset(ROOT / "plugins/numeric/data/evalset-tinystories.json")["calibration"])
    d = tmp_path_factory.mktemp("l2")
    (d / "s.json").write_text(json.dumps(spec))
    (d / "w.npz").write_bytes(npz)
    return L2Model.load(d / "s.json", d / "w.npz")


def run(l2, **params):
    profile = json.loads((DATA / "gf180mcu.json").read_text())
    library = json.loads((DATA / "library-180nm.json").read_text())
    return design(
        l2.spec,
        "a" * 64,
        "b" * 64,
        storage_bits(l2.spec, l2.a),
        profile,
        library,
        {"weightStorage": "gf180mcu.stdcell-rom", **params},
    )


def test_chip_spec_references_the_numeric_spec_and_covers_observation_points(l2):
    chip, ppa = run(l2)
    assert chip["numericSpec"]["hash"] == "a" * 64
    assert chip["dataflow"]["accumulatorBits"] == l2.spec["accumulator"]["bits"]
    assert {r["point"] for r in chip["observability"]["registers"]} == {p["id"] for p in l2.spec["observationPoints"]}
    assert "storage:gf180mcu.stdcell-rom" in chip["processCapabilities"]
    assert chip["storage"]["weights"]["bits"] == storage_bits(l2.spec, l2.a)["weights"]
    assert ppa["performance"]["tokensPerSecond"] > 0


def test_verify_detects_a_different_numeric_spec_and_the_area_limit(l2):
    chip, ppa = run(l2)
    result = verify(chip, l2.spec, "a" * 64, ppa)
    assert result["numeric_spec_matches"] and result["observation_points_covered"]
    assert result["fits_area"] is False  # stories260K does not fit in 2 mm2 with the standard-cell ROM
    assert verify(chip, l2.spec, "c" * 64, ppa)["numeric_spec_matches"] is False
    bigger, ppa2 = run(l2, targetAreaMm2=8)
    assert verify(bigger, l2.spec, "a" * 64, ppa2)["fits_area"] is True
