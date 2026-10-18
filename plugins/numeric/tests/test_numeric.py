import json
from pathlib import Path

import numpy as np
import pytest
from llmchip_model import Model, Tokenizer, load_llama2c
from llmchip_model.evalset import load_evalset
from llmchip_numeric import L2Model, SpecMismatch, build, storage_bits
from llmchip_numeric.evaluate import evaluate
from llmchip_numeric.l2 import rshift
from llmchip_numeric.spec import quantize_matrix

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "models" / "stories260K"
EVALSET = Path(__file__).parents[1] / "data" / "evalset-tinystories.json"


@pytest.fixture(scope="module")
def model():
    cfg, w = load_llama2c((FIXTURE / "stories260K.bin").read_bytes())
    return cfg, w, Tokenizer((FIXTURE / "tok512.bin").read_bytes(), cfg.vocab_size)


def l2_for(model, tmp_path, fmt):
    cfg, w, tok = model
    spec, npz = build(cfg, w, tok, load_evalset(EVALSET)["calibration"], weight_format=fmt)
    (tmp_path / "s.json").write_text(json.dumps(spec))
    (tmp_path / "w.npz").write_bytes(npz)
    return spec, L2Model.load(tmp_path / "s.json", tmp_path / "w.npz")


def test_rounding_is_half_up_with_arithmetic_shift():
    v = np.array([5, 6, 7, -5, -6, -7])
    assert rshift(v, 2).tolist() == [1, 2, 2, -1, -1, -2]
    assert rshift(np.array([3]), -2).tolist() == [12]


def test_group_quantization_reconstructs_within_half_a_step():
    rng = np.random.default_rng(0)
    w = rng.normal(size=(8, 70))
    q, m, e = quantize_matrix(w, "int4", 32)
    assert q.shape == (8, 70) and m.shape == (8, 3)
    assert np.abs(q).max() <= 7
    rec = q * (m[:, np.arange(70) // 32] * 2.0**-e)
    step = m[:, np.arange(70) // 32] * 2.0**-e
    assert np.all(np.abs(rec - w) <= step / 2 + 1e-12)


def test_spec_records_everything_l2_needs(model, tmp_path):
    spec, l2 = l2_for(model, tmp_path, "int4")
    for section in (
        "weights",
        "activations",
        "accumulator",
        "rounding",
        "saturation",
        "operators",
        "luts",
        "decode",
        "observationPoints",
        "context",
    ):
        assert section in spec
    assert spec["accumulator"]["bits"] <= 48
    bits = storage_bits(spec, l2.a)
    norms = 5 * 2 * 64 + 64
    assert bits["weights"] == (260_032 - norms) * 4


def test_l2_is_deterministic_and_close_to_l0_at_int8(model, tmp_path):
    cfg, w, tok = model
    _, l2 = l2_for(model, tmp_path, "int8")
    ids = tok.encode("Once upon a time")
    t1, t2 = [], []
    assert l2.generate(ids, 32, t1) == l2.generate(ids, 32, t2)
    for a, b in zip(t1, t2, strict=True):
        np.testing.assert_array_equal(a["logits"], b["logits"])
    assert tok.decode(l2.generate(ids, 40)) == tok.decode(Model(cfg, w).generate(ids, 40))


def test_weights_must_match_the_spec(model, tmp_path):
    spec, l2 = l2_for(model, tmp_path, "int4")
    arrays = dict(l2.a)
    arrays["0.wq.q"] = arrays["0.wq.q"].copy()
    arrays["0.wq.q"][0, 0] ^= 1
    with pytest.raises(SpecMismatch):
        L2Model(spec, arrays)


def test_evaluation_reports_accuracy_against_l0(model, tmp_path):
    cfg, w, tok = model
    _, l2 = l2_for(model, tmp_path, "int4")
    evalset = load_evalset(EVALSET) | {"prompts": ["Once upon a time", "Tom had a big red ball"], "steps": 24}
    report = evaluate(Model(cfg, w), l2, tok, evalset)
    m = report["metrics"]
    assert m["prompts_evaluated"] == 2 and m["tokens_per_prompt"] == 24
    assert m["l0_perplexity"] > 1 and m["l2_perplexity"] > 1
    assert 0 <= m["token_agreement"] <= 1
    assert len(report["samples"]) == 2
