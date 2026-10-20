import hashlib
import io
import itertools
import json
import tarfile
from pathlib import Path

import numpy as np
import pytest
from llmchip_model import Tokenizer, load_llama2c
from llmchip_model.evalset import load_evalset
from llmchip_numeric import L2Model, arith, build
from llmchip_rtl import isa
from llmchip_rtl.bundle import bundle_files, write_tar
from llmchip_rtl.compiler import Compiled, map_matrix, unmap_matrix

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "models" / "stories260K"
EVALSET = ROOT / "plugins" / "numeric" / "data" / "evalset-tinystories.json"


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    cfg, w = load_llama2c((FIXTURE / "stories260K.bin").read_bytes())
    tok = Tokenizer((FIXTURE / "tok512.bin").read_bytes(), cfg.vocab_size)
    evalset = load_evalset(EVALSET)
    spec, npz = build(cfg, w, tok, evalset["calibration"], weight_format="int4")
    d = tmp_path_factory.mktemp("l2")
    (d / "s.json").write_text(json.dumps(spec))
    (d / "w.npz").write_bytes(npz)
    l2 = L2Model.load(d / "s.json", d / "w.npz")
    return l2, tok, evalset, Compiled(l2.spec, l2.a, 8)


@pytest.mark.parametrize("lanes", [1, 4, 8, 16])
def test_mapping_round_trip_with_padding(lanes):
    rng = np.random.default_rng(lanes)
    q = rng.integers(-8, 8, (37, 24))
    m = rng.integers(1, 2**15, (37, 3))
    words, scales = map_matrix(q, m, lanes)
    assert words.shape[1] == scales.shape[1] == lanes
    back_q, back_m = unmap_matrix(words, scales, 37, 24, 3, lanes)
    assert np.array_equal(back_q, q) and np.array_equal(back_m, m)
    # Padding rows hold the fill values of the mapping rule: q = 0, m = 1.
    blocks = -(-37 // lanes)
    full = words.reshape(blocks, 24, lanes).transpose(0, 2, 1).reshape(blocks * lanes, 24)
    assert not full[37:].any()


def test_compiled_design_unmaps_to_the_quantized_weights(setup):
    l2, _, _, c = setup
    restored = c.unmap()
    assert sorted(restored) == sorted(l2.a)
    for k, v in l2.a.items():
        assert np.array_equal(restored[k], v), k


def test_instruction_encoding_round_trips():
    fields = [0, -1, 2**31 - 1, -(2**31), 12345]
    op, back = isa.decode(isa.encode("MATVEC", fields))
    assert op == "MATVEC" and back[:5] == fields and not any(back[5:])
    with pytest.raises(ValueError):
        isa.encode("ADD", [2**31])


def test_scalar_functions_follow_the_numeric_spec():
    assert [isa.rsh(v, 2) for v in (5, 6, 7, -5, -6, -7)] == [1, 2, 2, -1, -1, -2]
    assert isa.rsh(3, -2) == 12 and isa.rsh(3, 0) == 3
    assert isa.sat(40000, 16) == 32767 and isa.sat(-40000, 16) == -32768
    for v in (1, 2, 63, 64, 255, 256, 2**20 + 7, 2**40):
        e2, t = isa.rsqrt_index(v)
        assert e2 % 2 == 0 and 64 <= t < 256


def test_isa_rules_are_the_numeric_spec_rules():
    """isa.py ships alone in the bundle with its own copy of the rules; it must equal arith.py."""
    v = np.arange(-70000, 70001, 7)
    for mode in arith.ROUNDING:
        for s in (-3, 0, 1, 2, 5, 14, 15):
            assert np.array_equal(isa.rsh(v, s, mode), arith.rshift(v, s, mode)), (mode, s)
    for mode in arith.OVERFLOW:
        for bits in (16, 17, 24, 32):
            assert np.array_equal(isa.sat(v, bits, mode), arith.overflow(v, bits, mode)), (mode, bits)
    assert isa.ROUND_MODES == arith.ROUNDING


def assert_isa_equals_l2(l2, tok, prompts, c, steps_per_prompt=16):
    machine = isa.Machine(c.program, c.wrom, c.srom, c.crom, c.lanes, c.dmem_words, c.context, **isa.modes(c.widths))
    for prompt in prompts:
        ids = tok.encode(prompt)
        steps: list = []
        l2.generate(ids, steps_per_prompt, steps)
        token = ids[0]
        for pos, expected in enumerate(steps):
            traces: list = []
            out = machine.step(token, pos, traces)
            got = {(t["point"], t["layer"]): t["values"] for t in traces}
            for layer, v in enumerate(expected["attn_residual"]):
                assert np.array_equal(got[(1, layer)], v)
            for layer, v in enumerate(expected["ffn_residual"]):
                assert np.array_equal(got[(2, layer)], v)
            assert np.array_equal(got[(3, 0)], expected["logits"])
            if pos >= len(ids) - 1:
                assert out == expected["token"]
            token = expected["token"]


def test_isa_program_equals_l2_bit_for_bit(setup):
    l2, tok, evalset, c = setup
    assert_isa_equals_l2(l2, tok, evalset["prompts"][:3], c)


@pytest.mark.parametrize(
    ("rounding", "overflow", "acc_bits"),
    [
        ("half-even", "saturate", None),
        ("truncate", "wrap", None),
        ("half-up", "saturate", 22),
        ("half-even", "wrap", 22),
    ],
)
def test_every_numeric_scheme_compiles_to_the_same_bits_as_l2(tmp_path, rounding, overflow, acc_bits):
    cfg, w = load_llama2c((FIXTURE / "stories260K.bin").read_bytes())
    tok = Tokenizer((FIXTURE / "tok512.bin").read_bytes(), cfg.vocab_size)
    evalset = load_evalset(EVALSET)
    spec, npz = build(
        cfg, w, tok, evalset["calibration"], rounding=rounding, overflow=overflow, accumulator_bits=acc_bits
    )
    (tmp_path / "s.json").write_text(json.dumps(spec))
    (tmp_path / "w.npz").write_bytes(npz)
    l2 = L2Model.load(tmp_path / "s.json", tmp_path / "w.npz")
    c = Compiled(l2.spec, l2.a, 8)
    params = c.widths
    assert isa.ROUND_MODES[params["ROUND_MODE"]] == rounding and bool(params["OVERFLOW_WRAP"]) == (overflow == "wrap")
    if acc_bits is not None:
        assert params["ACC_NARROW"] == 1 and params["ACCB"] == params["TOTW"] == acc_bits
    assert_isa_equals_l2(l2, tok, evalset["prompts"][:2], c, 12)
    if acc_bits is not None:
        assert l2.saturations.get("accumulator", 0) > 0  # the narrow accumulator really overflowed


def test_debug_traces_follow_every_operation_and_have_their_own_buffers(setup):
    *_, c = setup
    program = [isa.decode(w) for w in c.program]
    traced = [t["layer"] for t in c.trace_map if t["debug"]]
    computing = [i for i, (op, _) in enumerate(program) if op not in ("TRACE", "END", "ARGMAX")]
    assert traced == computing == c.debug_map
    for i in computing:  # the debug trace directly follows its operation
        op, f = program[i + 1]
        assert op == "TRACE" and f[4] & isa.FLAG_DEBUG and f[3] == i
    # Trace buffers lie after the working data and do not overlap.
    spans = sorted((t["addr"], t["addr"] + t["n"]) for t in c.trace_map)
    assert spans[0][0] >= c.data_words and all(a[1] <= b[0] for a, b in itertools.pairwise(spans))
    assert spans[-1][1] == c.dmem_words


def test_debug_mode_copies_more_traces_and_the_same_tokens(setup):
    _l2, tok, evalset, c = setup
    ids = tok.encode(evalset["prompts"][0])
    tokens = None
    for mode in (isa.MODE_POINTS, isa.MODE_POINTS | isa.MODE_DEBUG):
        machine = isa.Machine(c.program, c.wrom, c.srom, c.crom, c.lanes, c.dmem_words, c.context)
        traces: list = []
        out = machine.step(ids[0], 0, traces, mode=mode)
        debug = [t for t in traces if t["point"] == isa.POINTS["op"]]
        assert bool(debug) == bool(mode & isa.MODE_DEBUG)
        tokens = out
    # Debug traces only copy data: the token is the same in both modes.
    assert tokens == isa.Machine(c.program, c.wrom, c.srom, c.crom, c.lanes, c.dmem_words, c.context).step(ids[0], 0)


def test_widths_bound_every_value(setup):
    *_, c = setup
    w = c.widths
    assert w["DW"] >= 34 and w["ACCW"] <= w["DW"] and w["TOTW"] <= w["DW"] and w["CW"] <= 18
    assert (1 << w["AW"]) >= c.dmem_words and (1 << w["PCW"]) >= len(c.program)
    assert w["RSH_POSITIVE"] == 1 and w["SILU_FRAC_GE4"] == 1


def test_bundle_is_deterministic_and_self_contained(setup, tmp_path):
    *_, c = setup
    source = {"numericSpec": "a" * 64, "weights": "b" * 64}
    write_tar(bundle_files(c, source), tmp_path / "a.tar")
    write_tar(bundle_files(c, source), tmp_path / "b.tar")
    a, b = (tmp_path / "a.tar").read_bytes(), (tmp_path / "b.tar").read_bytes()
    assert hashlib.sha256(a).digest() == hashlib.sha256(b).digest()
    with tarfile.open(fileobj=io.BytesIO(a)) as tar:
        names = set(tar.getnames())
        design = json.loads(tar.extractfile("bundle/design.json").read())
        params = tar.extractfile("bundle/rtl/llmchip_params.vh").read().decode()
    for name in (
        "isa.py",
        "isa-image.npz",
        "rtl/llmchip_core.v",
        "rtl/llmchip_chip.v",
        "rtl/llmchip_roms.v",
        "rtl/tb.cpp",
        "rtl/tb_top.v",
        "rtl/trace.map",
    ):
        assert f"bundle/{name}" in names
    assert design["source"] == source
    assert f"`define VOCAB {design['model']['vocab_size']}" in params
