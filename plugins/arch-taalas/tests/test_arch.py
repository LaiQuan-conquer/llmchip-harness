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
    assert {r["point"] for r in chip["observability"]["points"]} == {p["id"] for p in l2.spec["observationPoints"]}
    # Every port has an interface timing entry: the source of the IO delay constraints.
    timed = set(chip["timing"]["inputs"]) | set(chip["timing"]["outputs"]) | {chip["timing"]["clock"]["port"]}
    assert timed == {p["name"] for p in chip["pins"]}
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


def test_speed_is_the_cycle_count_of_the_microcode(l2):
    from llmchip_rtl import perf
    from llmchip_rtl.compiler import Compiled

    chip, ppa = run(l2, parallelMacs=2, clockMHz=8, memoryInterface="sqi", operandBuffers="attention")
    c = Compiled(l2.spec, l2.a, 2, {"memoryInterface": "sqi", "operandBuffers": "attention"})
    per_pos = [perf.step_cycles(c.program, c.params, p, 0).total for p in range(c.context)]
    p = ppa["performance"]
    assert p["cyclesPerPosition"] == per_pos
    assert p["cyclesPerToken"] == sum(per_pos) / len(per_pos)
    assert p["tokensPerSecond"] == 8e6 / p["cyclesPerToken"]
    assert chip["dataflow"]["memoryInterface"] == "sqi" and chip["dataflow"]["operandBuffers"] == "attention"
    assert {f"kv_sio{k}" for k in range(4)} <= {pin["name"] for pin in chip["pins"]}
    assert ppa["microarchitecture"]["onchipBufferBits"] > 0
    # verify recomputes the cycles from the microcode; a chip spec claiming another speed fails.
    assert verify(chip, l2.spec, "a" * 64, ppa, l2.a)["cycles_match_model"] is True
    forged = json.loads(json.dumps(chip))
    forged["performance"]["cyclesPerToken"] /= 2
    assert verify(forged, l2.spec, "a" * 64, ppa, l2.a)["cycles_match_model"] is False


def test_exploration_marks_the_pareto_front_and_the_fit(l2):
    from llmchip_arch import explore

    profile = json.loads((DATA / "gf180mcu.json").read_text())
    library = json.loads((DATA / "library-180nm.json").read_text())
    params = {"weightStorage": "gf180mcu.stdcell-rom", "clockMHz": 8, "targetAreaMm2": 20, "lanesOptions": [1, 4]}
    result = explore.explore(l2.spec, l2.a, storage_bits(l2.spec, l2.a), profile, library, params)
    points = result["points"]
    assert len(points) == 2 * 2 * 3
    front = [p for p in points if p["pareto"]]
    assert front and all(p["fits"] for p in front)
    for p in points:  # no point of the front is beaten in both speed and area
        if p["pareto"]:
            assert not any(
                q["fits"] and q["tokensPerSecond"] > p["tokensPerSecond"] and q["dieMm2"]["typ"] < p["dieMm2"]["typ"]
                for q in points
            )
    assert result["fastestFitting"] == max((p for p in points if p["fits"]), key=lambda p: p["tokensPerSecond"])["id"]


def test_a_graph_spec_is_explored_from_its_shape(tmp_path):
    from llmchip_arch import explore

    spec = {
        "format": explore.GRAPH_SPEC_FORMAT,
        "model": {"family": "qwen3"},
        "hyper": {
            "layers": 2,
            "hidden": 64,
            "heads": 4,
            "kvHeads": 2,
            "headDim": 32,
            "intermediate": 128,
            "vocab": 1000,
        },
        "context": {"maxTokens": 64},
        "weights": {
            "storageBits": 8,
            "groupSize": 32,
            "levels": [-127, 127],
            "classifier": "shared with the embedding",
        },
    }
    profile = json.loads((DATA / "gf180mcu.json").read_text())
    library = json.loads((DATA / "library-180nm.json").read_text())
    storage = {"weights": 10_000_000 * 8, "scales": 0, "biases": 0, "norms": 0, "luts": 0}
    result = explore.explore(
        spec,
        None,
        storage,
        profile,
        library,
        {"weightStorage": "gf180mcu.stdcell-rom", "clockMHz": 8, "targetAreaMm2": 2.4},
    )
    assert result["analysis"] is True
    assert not any(p["fits"] for p in result["points"])  # ten million weights do not fit 2.4 mm2
    assert result["chosen"]["tokensPerSecond"] > 0
