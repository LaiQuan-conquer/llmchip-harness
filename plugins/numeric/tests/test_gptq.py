"""Calibrated quantization (GPTQ) of a recovered graph.

- The float graph on PyTorch (`torch_float.FloatGraph`) gives the NumPy interpreter's logits.
- GPTQ produces the spec's weight format (q within the levels, 15-bit mantissas, one exponent;
  the dequantized weights are exactly q * m * 2^-e) and, on correlated inputs, changes the
  product XW less than rounding each weight on the same grid does.
- `calibrate` quantizes every matrix of a tiny Qwen3 graph (the tied output layer once) and
  records the activation ranges `graph_l2.build` needs; a matrix can get another format.
"""

import json
from pathlib import Path

import numpy as np
import pytest
from llmchip_model.census import census
from llmchip_model.execute import forward
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover

torch = pytest.importorskip("torch")
from llmchip_numeric import graph_l2  # noqa: E402
from llmchip_numeric.gptq import calibrate, gptq  # noqa: E402
from llmchip_numeric.spec import WEIGHT_FORMATS, mantissas, matrix_exponent  # noqa: E402
from llmchip_numeric.torch_float import FloatGraph  # noqa: E402

TINY_QWEN3 = Path(__file__).parents[3] / "tests" / "fixtures" / "models" / "tiny-qwen3"


def tiny():
    path = TINY_QWEN3 / "model.safetensors"
    layout = read_layout(path)
    graph = recover(census([layout], json.loads((TINY_QWEN3 / "config.json").read_text()), paths={path.name: path}))
    return graph, WeightLoader([layout], {path.name: path})


def test_the_torch_float_graph_gives_the_numpy_logits():
    graph, loader = tiny()
    tokens = [3, 9, 17, 25, 33, 41, 49, 57]
    want = forward(graph, loader.get, tokens)
    got = FloatGraph(graph, loader.get, "cpu").logits(torch.as_tensor([tokens])).numpy()[0]
    np.testing.assert_allclose(got, want, rtol=1e-4, atol=1e-5)


def rtn(w: np.ndarray, fmt: str, group: int, e: int) -> np.ndarray:
    """Rounding on the same grid as GPTQ's first group scales (for comparison)."""
    qmax = WEIGHT_FORMATS[fmt]["qmax"]
    g = w.reshape(w.shape[0], -1, group)
    m = mantissas(np.abs(g).max(axis=2) / qmax, e)
    eff = m * 2.0**-e
    return (np.clip(np.round(g / eff[:, :, None]), -qmax, qmax) * eff[:, :, None]).reshape(w.shape)


@pytest.mark.parametrize("fmt", ["int4", "int8", "int3"])
def test_gptq_keeps_the_format_and_beats_rounding_on_correlated_inputs(fmt):
    rng = np.random.default_rng(0)
    out, inp, group = 48, 256, 64
    w = rng.normal(size=(out, inp)).astype(np.float32)
    # Correlated inputs: a few strong directions, as activations have.
    x = rng.normal(size=(2048, 16)) @ rng.normal(size=(16, inp))
    x = (x + 0.1 * rng.normal(size=(2048, inp))).astype(np.float32)
    h = torch.as_tensor(x.T.astype(np.float64) @ x)
    q, m, e, deq = gptq(torch.as_tensor(w), h, fmt, group)
    qmax = WEIGHT_FORMATS[fmt]["qmax"]
    assert q.shape == (out, inp) and m.shape == (out, inp // group)
    assert np.abs(q).max() <= qmax and m.min() >= 1 and m.max() <= 32767
    np.testing.assert_array_equal(deq.numpy(), (q * np.repeat(m, group, axis=1) * 2.0**-e).astype(np.float32))
    err_gptq = np.linalg.norm(x @ (w - deq.numpy()).T)
    e_rtn = matrix_exponent(np.abs(w.reshape(out, -1, group)).max(axis=2) / qmax) - 1
    err_rtn = np.linalg.norm(x @ (w - rtn(w, fmt, group, e_rtn)).T)
    assert err_gptq < err_rtn, (err_gptq, err_rtn)


def test_calibrate_quantizes_every_matrix_once_and_records_the_ranges():
    graph, loader = tiny()
    seqs = [[3, 9, 17, 25, 33, 41, 49, 57], [5, 6, 7, 8, 9, 10, 11, 12]]
    quantized, stats = calibrate(graph, loader.get, seqs, "int4", 16, {r"1\.down_proj": "int8"}, device="cpu", batch=1)
    names = {
        node.get("weight").format(i=i)
        for i in range(graph["nodes"]["repeat"])
        for node in graph["nodes"]["block"]
        if node["op"] == "linear"
    }
    head = graph["nodes"]["epilogue"][1]
    assert set(quantized) == names | {head["weight"]}  # tied: the embedding table is the output layer
    assert quantized["model.layers.1.mlp.down_proj.weight"][3] == "int8"
    assert quantized["model.layers.0.mlp.down_proj.weight"][3] == "int4"
    for kind in ("x", "xn", "q", "k", "v", "qn", "kn", "h1", "h3", "g", "logits", "0.x", "1.g"):
        assert stats[kind] > 0, kind


def test_a_calibrated_spec_runs_as_l2_and_keeps_per_matrix_formats():
    graph, loader = tiny()
    seqs = [[3, 9, 17, 25, 33, 41, 49, 57], [5, 6, 7, 8, 9, 10, 11, 12]]
    quantized, stats = calibrate(graph, loader.get, seqs, "int4", 16, {r"1\.down_proj": "int8"}, device="cpu", batch=1)
    spec, arrays = graph_l2.build(
        graph, loader.peek, weight_format="int4", group_size=16, context=32, stats=stats, quantized=quantized
    )
    # The calibrated format of every matrix is recorded, and storage counts each matrix's bits.
    formats = spec["weights"]["formats"]
    assert formats["1.down_proj"] == "int8" and formats["0.down_proj"] == "int4"
    bits = graph_l2.storage_bits(spec, arrays)
    wide = sum(a.size for name, a in arrays.items() if name.endswith(".q") and formats[name[:-2]] == "int8")
    narrow = sum(a.size for name, a in arrays.items() if name.endswith(".q") and formats[name[:-2]] == "int4")
    assert bits["weights"] == 8 * wide + 4 * narrow
    # The spec loads as the integer L2 and generates: the same decode rule as any graph spec.
    l2 = graph_l2.GraphL2(graph, spec, arrays)
    logits = l2.forward(seqs[0][:8])
    assert logits.shape == (8, spec["hyper"]["vocab"])
    assert (spec["weights"]["levels"][1] == 7) and int(np.abs(arrays["1.down_proj.q"]).max()) <= 127
