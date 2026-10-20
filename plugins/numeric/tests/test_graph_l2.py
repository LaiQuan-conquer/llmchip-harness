"""The graph L2 (any recovered model) against the chip L2 and against L0.

- On a llama2.c graph, with the fraction bits of a chip numeric spec, the graph L2
  computes exactly what `L2Model` computes: logits and residual stream, bit for bit,
  under every rounding and overflow mode and with a narrow accumulator.
- Its exact float32 products equal int64 products on extreme values.
- On tiny Hugging Face models (Llama, Qwen2 with QKV bias, Qwen3 with QK norm) it stays
  close to the float graph (L0) at int8.
"""

import json
from pathlib import Path

import numpy as np
import pytest
from llmchip_model import Tokenizer, load_llama2c
from llmchip_model.census import census
from llmchip_model.evalset import load_evalset
from llmchip_model.execute import forward
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import L2Model, build, graph_l2

ROOT = Path(__file__).parents[3]
STORIES = ROOT / "tests" / "fixtures" / "models" / "stories260K"
TINY_QWEN3 = ROOT / "tests" / "fixtures" / "models" / "tiny-qwen3"
EVALSET = Path(__file__).parents[1] / "data" / "evalset-tinystories.json"


def recovered_llama2c():
    path = STORIES / "stories260K.bin"
    layout = read_layout(path)
    return recover(census([layout], None)), WeightLoader([layout], {path.name: path})


def recovered_hf(directory: Path):
    path = directory / "model.safetensors"
    layout = read_layout(path)
    c = census([layout], json.loads((directory / "config.json").read_text()), paths={path.name: path})
    return recover(c), WeightLoader([layout], {path.name: path})


@pytest.mark.parametrize(
    ("fmt", "rounding", "overflow", "acc_bits"),
    [("int4", "half-up", "saturate", None), ("int8", "half-even", "wrap", 24), ("ternary", "truncate", "saturate", 22)],
)
def test_graph_l2_equals_the_chip_l2_on_a_llama2c_model(tmp_path, fmt, rounding, overflow, acc_bits):
    cfg, w = load_llama2c((STORIES / "stories260K.bin").read_bytes())
    tok = Tokenizer((STORIES / "tok512.bin").read_bytes(), cfg.vocab_size)
    modes = {"rounding": rounding, "overflow": overflow, "accumulator_bits": acc_bits}
    spec, npz = build(cfg, w, tok, load_evalset(EVALSET)["calibration"], weight_format=fmt, group_size=32, **modes)
    (tmp_path / "s.json").write_text(json.dumps(spec))
    (tmp_path / "w.npz").write_bytes(npz)
    chip = L2Model.load(tmp_path / "s.json", tmp_path / "w.npz")

    graph, loader = recovered_llama2c()
    gspec, arrays = graph_l2.build(
        graph,
        loader.peek,
        weight_format=fmt,
        group_size=32,
        context=spec["context"]["maxTokens"],
        frac=spec["activations"]["frac"] | {"qn": 0, "kn": 0},
        logits_frac=spec["logits"]["frac"],
        **modes,
    )
    assert gspec["normWeights"]["frac"] == spec["normWeights"]["frac"]
    model = graph_l2.GraphL2(graph, gspec, arrays)

    ids = tok.encode("Once upon a time there was a little girl")
    steps: list = []
    chip.generate(ids + [400] * 8, len(ids) + 8, steps)  # every position forced: teacher forcing
    hidden: list = []
    logits = model.forward(ids + [400] * 8, hidden)
    for pos, step in enumerate(steps):
        np.testing.assert_array_equal(logits[pos], step["logits"], err_msg=f"logits at {pos}")
        for layer, x in enumerate(step["ffn_residual"]):
            np.testing.assert_array_equal(hidden[layer + 1][pos], x, err_msg=f"layer {layer} at {pos}")
    # The same overflow events, counted the same way.
    assert model.saturations == chip.saturations


def test_exact_products_equal_integer_products():
    rng = np.random.default_rng(1)
    graph, loader = recovered_llama2c()
    spec, arrays = graph_l2.build(
        graph, loader.peek, weight_format="int8", group_size=256, frac=dict.fromkeys(graph_l2.KINDS, 8), logits_frac=8
    )
    model = graph_l2.GraphL2(graph, spec, arrays)
    key = "0.q_proj"
    q, m = arrays[f"{key}.q"].astype(np.int64), arrays[f"{key}.m"].astype(np.int64)
    x = rng.choice([-32768, 32767, -1, 0, 255, -256], size=(5, q.shape[1]))
    got = model.linear(key, x, 0, 0, "q", bits=64)
    groups = -(-q.shape[1] // 256)
    xp = np.zeros((5, groups * 256), np.int64)
    xp[:, : q.shape[1]] = x
    qp = np.zeros((q.shape[0], groups * 256), np.int64)
    qp[:, : q.shape[1]] = q
    parts = np.einsum("tgk,ogk->tog", xp.reshape(5, groups, 256), qp.reshape(-1, groups, 256))
    want = model._rsh((parts * m[None]).sum(axis=-1), spec["weights"]["exponents"][key])
    np.testing.assert_array_equal(got, want)


def tiny(tmp_path: Path, family: str) -> Path:
    """A tiny random Hugging Face model of the family, saved in bfloat16."""
    torch = pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    common = {
        "hidden_size": 32,
        "intermediate_size": 64,
        "num_hidden_layers": 2,
        "num_attention_heads": 4,
        "num_key_value_heads": 2,
        "vocab_size": 128,
        "max_position_embeddings": 64,
        "tie_word_embeddings": False,
    }
    config = (
        transformers.LlamaConfig(**common)
        if family == "llama"
        else transformers.Qwen2Config(**common, use_sliding_window=False)
    )
    torch.manual_seed(0)
    transformers.AutoModelForCausalLM.from_config(config).to(torch.bfloat16).save_pretrained(tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    ("family", "marker"), [("qwen3", "0.q_norm.w"), ("qwen2", "0.q_proj.b"), ("llama", "lm_head.q")]
)
def test_graph_l2_follows_l0(tmp_path, family, marker):
    graph, loader = recovered_hf(TINY_QWEN3 if family == "qwen3" else tiny(tmp_path, family))
    tokens = [1, 5, 9, 13, 17, 21, 25, 29, 33, 37]
    stats = graph_l2.calibrate(graph, loader.get, [tokens])
    assert {"x", "xn", "q", "k", "v", "h1", "h3", "g", "logits"} <= set(stats)
    spec, arrays = graph_l2.build(
        graph, loader.peek, weight_format="int8", group_size=16, stats=stats, per_layer=True, residual_bits=24
    )
    # QK norm weights (Qwen3), QKV biases (Qwen2), a separate output layer (untied Llama).
    assert spec["model"]["family"] == family and marker in arrays
    act = spec["activations"]
    assert act["bits"]["x"] == 24 and act["granularity"] == "layer" and len(act["layerFrac"]) == 2
    model = graph_l2.GraphL2(graph, spec, arrays)
    hidden: list = []
    logits = model.forward(tokens, hidden) * 2.0 ** -spec["logits"]["frac"]
    reference = forward(graph, loader.get, tokens)
    assert len(hidden) == graph["hyper"]["layers"] + 2
    rel = np.abs(logits - reference).max() / np.abs(reference).max()
    assert rel < 0.05, rel
    assert (logits.argmax(-1) == reference.argmax(-1)).mean() >= 0.8
