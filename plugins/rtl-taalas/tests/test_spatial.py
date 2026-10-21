"""The spatial engine generator (architecture taalas-spatial/1): the weights become circuits.

The bit-exact comparison of the generated RTL with L2 needs Verilator (the EDA image) and runs
in sim-verilator; here: the CSD digits, the round trip of the weights through the generated
netlist (FR-RTL-02), the fixed schedule, and the refusals."""

import io
import itertools
import json
from pathlib import Path

import numpy as np
import pytest
from llmchip_model import Config, Tokenizer, load_llama2c
from llmchip_model.evalset import load_evalset
from llmchip_model.proxy import char_tokenizer, random_checkpoint
from llmchip_numeric import build
from llmchip_rtl.spatial.generator import Spatial, check_round_trip, csd

ROOT = Path(__file__).parents[3]

PROXY = Config(dim=16, hidden_dim=32, n_layers=2, n_heads=2, n_kv_heads=1, vocab_size=35, seq_len=64)


def proxy(fmt: str = "ternary", seed: int = 1, **modes):
    cfg, w = load_llama2c(random_checkpoint(PROXY, seed))
    tok = Tokenizer(char_tokenizer(), cfg.vocab_size)
    prompts = load_evalset(ROOT / "plugins" / "model-llama" / "data" / "evalset-proxy.json")["calibration"]
    spec, npz = build(cfg, w, tok, prompts, weight_format=fmt, group_size=16, **modes)
    with np.load(io.BytesIO(npz)) as z:
        arrays = {k: z[k].astype(np.int64) for k in z.files if k != "meta"}
    return spec, arrays


@pytest.mark.parametrize("m", [1, 2, 3, 7, 11, 255, 21845, 32767, 32768, 27148])
def test_csd_digits_sum_to_the_value_without_adjacent_digits(m):
    digits = csd(m)
    assert sum(d * 2**j for j, d in digits) == m
    shifts = [j for j, _ in digits]
    assert all(b - a >= 2 for a, b in itertools.pairwise(shifts))
    assert len(digits) <= bin(m).count("1")


@pytest.mark.parametrize("fmt", ["ternary", "int4", "int8"])
def test_the_circuits_implement_the_weights_and_a_changed_wire_is_found(fmt):
    sp = Spatial(*proxy(fmt))
    files = sp.files()
    check_round_trip(sp, files)
    for old, new in ((".SUB(0)", ".SUB(1)"), ("xs[3]", "xs[4]")):
        bad = dict(files, **{"sp_mv_l1_w13.v": files["sp_mv_l1_w13.v"].replace(old, new, 1)})
        with pytest.raises(ValueError, match="does not implement"):
            check_round_trip(sp, bad)


def test_the_schedule_is_fixed_and_meets_one_thousand_tokens_per_second():
    sp = Spatial(*proxy())
    stages = sp.schedule()
    assert [n for n, _ in stages][:2] == ["embedding", "layer 0 rmsnorm (attention)"]
    assert stages[-1][0] == "argmax"
    cycles = sp.cycles_per_token()
    assert cycles == sum(c for _, c in stages) + 1
    assert cycles <= 8000
    # Every stage is a state of the generated core.
    core = sp.core_verilog()
    for name, n in stages:
        assert f"// {name}: {n} cycles" in core


def test_the_core_has_no_function_locals_in_clocked_blocks():
    core = Spatial(*proxy()).core_verilog()
    for block in core.split("function automatic")[1:]:
        header = block.split("endfunction")[0]
        assert "  reg " not in header and "integer" not in header


def test_unsupported_designs_are_refused_with_a_reason():
    spec, arrays = proxy()
    narrow = json.loads(json.dumps(spec))
    narrow["accumulator"]["canOverflow"] = True
    with pytest.raises(ValueError, match="sums exactly"):
        Spatial(narrow, arrays)
    other = json.loads(json.dumps(spec))
    other["context"]["maxTokens"] = 32
    with pytest.raises(ValueError, match="context of 64"):
        Spatial(other, arrays)


@pytest.mark.parametrize(
    ("fmt", "modes", "bump"),
    [
        ("ternary", {}, 0),
        ("int4", {"rounding": "half-even", "overflow": "wrap"}, 0),
        ("ternary", {"rounding": "truncate"}, 3),  # more fraction bits: many saturations
    ],
)
def test_the_model_of_the_circuits_equals_l2(fmt, modes, bump):
    from llmchip_numeric import L2Model
    from llmchip_rtl.spatial.bundle import image, params
    from llmchip_rtl.spatial.sp_model import Machine

    spec, arrays = proxy(fmt, **modes)
    for k in spec["activations"]["frac"]:
        spec["activations"]["frac"][k] += bump
    l2 = L2Model(spec, arrays)
    sp = Spatial(spec, arrays)
    machine = Machine(image(sp), params(sp))
    rng = np.random.default_rng(7)
    for _ in range(2):
        l2.reset()
        token = int(rng.integers(0, sp.vocab))
        for pos in range(sp.ctx):
            step: dict = {}
            logits = l2.forward(token, pos, step)
            traces: list = []
            out = machine.step(token, pos, traces)
            got = {(t["point"], t["layer"]): t["values"] for t in traces}
            for layer in range(sp.layers):
                assert np.array_equal(got[(1, layer)], step["attn_residual"][layer]), (pos, layer)
                assert np.array_equal(got[(2, layer)], step["ffn_residual"][layer]), (pos, layer)
            assert np.array_equal(got[(3, 0)], logits)
            assert out == int(np.argmax(logits))
            token = out if pos % 3 else int(rng.integers(0, sp.vocab))
    if bump:
        assert sum(l2.saturations.values()) > 100


def test_the_bundle_is_complete_and_deterministic(tmp_path):
    import tarfile

    from llmchip_rtl.bundle import write_tar
    from llmchip_rtl.spatial.bundle import bundle_files

    sp = Spatial(*proxy())
    a, b = tmp_path / "a.tar", tmp_path / "b.tar"
    write_tar(bundle_files(sp, {"numericSpec": "s"}), a)
    write_tar(bundle_files(sp, {"numericSpec": "s"}), b)
    assert a.read_bytes() == b.read_bytes()
    with tarfile.open(a) as tar:
        names = set(tar.getnames())
        design = json.loads(tar.extractfile("bundle/design.json").read())
    for path in design["sources"] + design["testbench"]["sources"] + [design["testbench"]["cpp"]]:
        assert f"bundle/{path}" in names
    assert {"bundle/sp_model.py", "bundle/sp-image.npz", "bundle/rtl/trace.map"} <= names
    assert design["params"]["CYCLES_PER_TOKEN"] == sp.cycles_per_token()
    assert sum(s["cycles"] for s in design["schedule"]) + 1 == sp.cycles_per_token()


def stream_out(v: int, s: int, w: int, length: int, mode: str, overflow: str) -> int:
    """The row output stage (sp_out) bit by bit: the stream of v, LSB first, for `length` bits."""
    bits = [(v >> k) & 1 for k in range(length)]
    sticky = half = c = ovf = 0
    y = []
    for k, a in enumerate(bits):
        if k + 1 < s:
            sticky |= a
        if k + 1 == s:
            half = a
        up = half if mode == "half-up" else (half & (sticky | a)) if mode == "half-even" else 0
        ci = up if k == s else c
        o = a ^ ci
        if k >= s:
            c = a & ci
            if k < s + w:
                y.append(o)
            elif o != y[-1]:
                ovf = 1
    r = sum(b << i for i, b in enumerate(y))
    r -= (r >> (w - 1)) << w
    if overflow == "saturate" and (ovf or o != y[-1]):
        return -(1 << (w - 1)) if o else (1 << (w - 1)) - 1
    return r


@pytest.mark.parametrize("fmt", ["ternary", "int4", "int8"])
def test_every_row_stream_is_long_enough_for_its_largest_sum(fmt):
    from llmchip_numeric import arith

    sp = Spatial(*proxy(fmt))
    for mtx in sp.matrices.values():
        idx = np.arange(mtx.q.shape[1]) // mtx.group
        coef = mtx.q.astype(object) * mtx.m[:, idx].astype(object)
        for row in coef:
            hi = sum(c * (32767 if c > 0 else -32768) for c in row)
            lo = sum(c * (-32768 if c > 0 else 32767) for c in row)
            for v in (hi, lo, hi // 2, lo // 2):
                want = arith.overflow(arith.rshift(v, mtx.shift, sp.rounding), mtx.width, sp.overflow)
                assert stream_out(v, mtx.shift, mtx.width, mtx.length, sp.rounding, sp.overflow) == want, mtx.name


@pytest.mark.parametrize(
    "shape",
    [
        {"dim": 32, "hidden_dim": 64, "n_layers": 3},
        {"dim": 64, "hidden_dim": 128, "n_layers": 2},
        {"dim": 16, "hidden_dim": 48, "n_layers": 8},
    ],
)
def test_other_shapes_generate_with_registers_wide_enough(shape):
    cfg, w = load_llama2c(random_checkpoint(Config(n_heads=2, n_kv_heads=1, vocab_size=35, seq_len=64, **shape), 3))
    tok = Tokenizer(char_tokenizer(), cfg.vocab_size)
    prompts = load_evalset(ROOT / "plugins" / "model-llama" / "data" / "evalset-proxy.json")["calibration"]
    spec, npz = build(cfg, w, tok, prompts, weight_format="ternary", group_size=16)
    with np.load(io.BytesIO(npz)) as z:
        arrays = {k: z[k].astype(np.int64) for k in z.files if k != "meta"}
    sp = Spatial(spec, arrays)
    files = sp.files()
    check_round_trip(sp, files)
    core = files["sp_core.v"]
    naw = ((2 * shape["n_layers"] + 1) * shape["dim"] - 1).bit_length()
    assert f"reg [{naw - 1}:0] nbase;" in core
    assert f"nbase <= {naw}'d{shape['n_layers'] * 2 * shape['dim']};" in core  # the final norm's weights


def test_shapes_the_engine_cannot_build_are_refused():
    spec, arrays = proxy()
    for key, value, reason in (
        ("vocab_size", 2000, "11-bit READ"),
        ("dim", 4, "head size"),
    ):
        other = json.loads(json.dumps(spec))
        other["model"]["config"][key] = value
        with pytest.raises(ValueError, match=reason):
            Spatial(other, arrays)
