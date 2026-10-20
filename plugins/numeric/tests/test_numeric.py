import hashlib
import json
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from llmchip_model import Model, Tokenizer, load_llama2c
from llmchip_model.evalset import load_evalset
from llmchip_numeric import L2Model, SpecMismatch, arith, build, storage_bits
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


def l2_for(model, tmp_path, fmt, **modes):
    cfg, w, tok = model
    spec, npz = build(cfg, w, tok, load_evalset(EVALSET)["calibration"], weight_format=fmt, **modes)
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


def test_rounding_modes_match_exact_arithmetic():
    """Every mode on every value and shift of a small range, against exact rationals."""
    v = np.arange(-4096, 4097)
    for shift in range(1, 9):
        exact = [Fraction(int(x), 2**shift) for x in v]
        floor = [q.numerator // q.denominator for q in exact]
        half_up = [(q + Fraction(1, 2)).numerator // (q + Fraction(1, 2)).denominator for q in exact]
        half_even = [round(q) for q in exact]  # Python rounds Fractions half to even
        assert arith.rshift(v, shift, "truncate").tolist() == floor
        assert arith.rshift(v, shift, "half-up").tolist() == half_up
        assert arith.rshift(v, shift, "half-even").tolist() == half_even
    assert arith.rshift(np.array([3, -3]), -2, "half-even").tolist() == [12, -12]


def test_overflow_saturates_or_wraps():
    v = np.array([32766, 32767, 32768, 40000, -32768, -32769, -40000])
    assert arith.overflow(v, 16, "saturate").tolist() == [32766, 32767, 32767, 32767, -32768, -32768, -32768]
    wrapped = [32766, 32767, -32768, 40000 - 65536, -32768, 32767, -40000 + 65536]
    assert arith.overflow(v, 16, "wrap").tolist() == wrapped
    assert arith.out_of_range(v, 16) == 4


def test_narrow_accumulator_adds_groups_in_order():
    terms = np.array([[100, 100, -150]])
    assert arith.accumulate(terms, None)[0].tolist() == [50]
    # 8 bits: 100 + 100 = 200 does not fit; saturated to 127, then 127 - 150 = -23.
    total, events = arith.accumulate(terms, 8, "saturate")
    assert total.tolist() == [-23] and events == 1
    # Wrapping is exact modulo 2^8: 200 wraps to -56, -206 wraps back, and the true sum 50 remains.
    total, events = arith.accumulate(terms, 8, "wrap")
    assert total.tolist() == [50] and events == 2


def test_default_scheme_is_unchanged(model, tmp_path):
    """The defaults (half-up, saturate, no-overflow accumulator) give the L2 of before the modes existed."""
    *_, tok = model
    spec, l2 = l2_for(model, tmp_path, "int4")
    assert spec["rounding"]["mode"] == "half-up" and spec["overflow"]["mode"] == "saturate"
    acc = spec["accumulator"]
    assert acc["canOverflow"] is False and acc["bits"] == acc["autoBits"]
    traces: list = []
    l2.generate(tok.encode("Once upon a time"), 24, traces)
    h = hashlib.sha256()
    for t in traces:
        h.update(np.asarray(t["logits"], "<i8").tobytes())
        h.update(np.asarray(t["ffn_residual"], "<i8").tobytes())
    assert h.hexdigest() == "f4e10b0f44fad4db79534f0dca47fbde02155dc4d73d849d71e557ebd141b589"


def test_every_scheme_runs_and_is_recorded(model, tmp_path):
    cfg, w, tok = model
    ids = tok.encode("Once upon a time")
    outputs = {}
    for rounding in arith.ROUNDING:
        for overflow in arith.OVERFLOW:
            spec, l2 = l2_for(model, tmp_path, "int8", rounding=rounding, overflow=overflow)
            assert (spec["rounding"]["mode"], spec["overflow"]["mode"]) == (rounding, overflow)
            assert (l2.rounding, l2.overflow) == (rounding, overflow)
            outputs[rounding, overflow] = tok.decode(l2.generate(ids, 24))
    # At int8 every scheme still tells the same story as L0 for this prompt.
    reference = tok.decode(Model(cfg, w).generate(ids, 24))
    assert outputs["half-up", "saturate"] == reference
    assert outputs["half-even", "saturate"] == reference


def test_narrow_accumulator_is_recorded_and_counts_overflows(model, tmp_path):
    *_, tok = model
    spec, l2 = l2_for(model, tmp_path, "int8", accumulator_bits=24)
    acc = spec["accumulator"]
    assert acc["bits"] == 24 and acc["canOverflow"] is True and acc["autoBits"] > 24
    l2.generate(tok.encode("Once upon a time"), 16)
    assert l2.saturations.get("accumulator", 0) > 0
    with pytest.raises(ValueError):
        l2_for(model, tmp_path, "int8", accumulator_bits=8)
