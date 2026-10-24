"""The fast L2 (PyTorch, GPU when there is one) against the reference graph L2, and the KV cache.

- `TorchL2` computes exactly what `GraphL2` computes: logits and every observation point,
  bit for bit, under every rounding and overflow mode and with a narrow accumulator, on a
  llama2.c graph (interleaved RoPE, ternary weights) and on tiny Qwen2 / Qwen3 graphs.
- Feeding a sequence through the KV cache, a prompt first and then token by token, gives the
  same logits and observation points as one pass over the whole sequence, in both models.
- The exact float32 product helper equals int64 products on extreme values, and TF32 is off.
"""

import json
from pathlib import Path

import numpy as np
import pytest
from llmchip_model.census import census
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover
from llmchip_numeric import graph_l2

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")
from llmchip_numeric.l2_torch import TorchKVCache, TorchL2, exact_mode, mm16  # noqa: E402

ROOT = Path(__file__).parents[3]
STORIES = ROOT / "tests" / "fixtures" / "models" / "stories260K" / "stories260K.bin"
TINY_QWEN3 = ROOT / "tests" / "fixtures" / "models" / "tiny-qwen3"


def recovered_llama2c():
    layout = read_layout(STORIES)
    return recover(census([layout], None)), WeightLoader([layout], {STORIES.name: STORIES})


def recovered_hf(directory: Path):
    path = directory / "model.safetensors"
    layout = read_layout(path)
    c = census([layout], json.loads((directory / "config.json").read_text()), paths={path.name: path})
    return recover(c), WeightLoader([layout], {path.name: path})


def tiny(tmp_path: Path, family: str) -> Path:
    """A tiny random Qwen2 model (QKV biases, half RoPE), saved in bfloat16."""
    assert family == "qwen2"
    config = transformers.Qwen2Config(
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        vocab_size=128,
        max_position_embeddings=64,
        tie_word_embeddings=True,
        use_sliding_window=False,
    )
    torch.manual_seed(0)
    transformers.AutoModelForCausalLM.from_config(config).to(torch.bfloat16).save_pretrained(tmp_path)
    return tmp_path


TOKENS = [1, 5, 9, 13, 17, 21, 25, 29, 33, 37, 41, 45]


def build(graph, loader, tokens, **options):
    stats = graph_l2.calibrate(graph, loader.get, [tokens])
    spec, arrays = graph_l2.build(graph, loader.peek, stats=stats, context=64, **options)
    return graph_l2.GraphL2(graph, spec, arrays)


def same_run(ref: graph_l2.GraphL2, fast: TorchL2, tokens: list[int]) -> None:
    h_ref: list = []
    h_fast: list = []
    np.testing.assert_array_equal(fast.forward(tokens, h_fast), ref.forward(tokens, h_ref))
    assert len(h_ref) == len(h_fast)
    for i, (a, b) in enumerate(zip(h_ref, h_fast, strict=True)):
        np.testing.assert_array_equal(b, a, err_msg=f"observation point {i}")
    assert fast.saturations == ref.saturations


@pytest.mark.parametrize(
    ("fmt", "rounding", "overflow", "acc_bits", "residual"),
    [
        ("int4", "half-up", "saturate", None, 24),
        ("int8", "half-even", "wrap", 24, 16),
        ("ternary", "truncate", "saturate", 22, 24),
    ],
)
def test_fast_l2_equals_the_reference_on_a_llama2c_graph(fmt, rounding, overflow, acc_bits, residual):
    graph, loader = recovered_llama2c()
    ref = build(
        graph,
        loader,
        TOKENS,
        weight_format=fmt,
        group_size=32,
        rounding=rounding,
        overflow=overflow,
        accumulator_bits=acc_bits,
        residual_bits=residual,
        per_layer=True,
    )
    same_run(ref, TorchL2(ref), [*TOKENS, 400, 401, 7, 8])


@pytest.mark.parametrize("family", ["qwen2", "qwen3"])
def test_fast_l2_equals_the_reference_on_qwen_graphs(tmp_path, family):
    graph, loader = recovered_hf(TINY_QWEN3 if family == "qwen3" else tiny(tmp_path, family))
    ref = build(graph, loader, TOKENS, weight_format="int4", group_size=16, per_layer=True, residual_bits=24)
    same_run(ref, TorchL2(ref), TOKENS)


@pytest.mark.parametrize("fast", [False, True])
def test_the_kv_cache_gives_the_same_results_as_one_pass(tmp_path, fast):
    graph, loader = recovered_hf(tiny(tmp_path, "qwen2"))
    ref = build(graph, loader, TOKENS, weight_format="int8", group_size=16, per_layer=True, residual_bits=24)
    model = TorchL2(ref) if fast else ref
    whole_hidden: list = []
    whole = model.forward(TOKENS, whole_hidden)
    one_pass = dict(model.saturations)
    if fast:
        model._sat = {}
    else:
        model.saturations = {}
    cache = TorchKVCache() if fast else graph_l2.KVCache()
    prompt = 5
    hidden: list = []
    first = model.forward(TOKENS[:prompt], hidden, cache=cache, rows=[prompt - 1])
    np.testing.assert_array_equal(first[0], whole[prompt - 1])
    for i in range(prompt, len(TOKENS)):
        step_hidden: list = []
        logits = model.forward([TOKENS[i]], step_hidden, cache=cache)
        np.testing.assert_array_equal(logits[0], whole[i], err_msg=f"position {i}")
        for layer, (a, b) in enumerate(zip(whole_hidden, step_hidden, strict=True)):
            np.testing.assert_array_equal(b[0], a[i], err_msg=f"point {layer} at {i}")
    assert model.saturations == one_pass, "the overflow counters of the steps add up to one pass"
    assert cache.length == len(TOKENS)
    with pytest.raises(ValueError, match="exceed the context"):
        model.forward([1] * (ref.context - cache.length + 1), cache=cache)


def test_exact_products_and_no_tf32():
    rng = np.random.default_rng(3)
    for shape_a, shape_b in (((7, 300), (300, 5)), ((2, 3, 600), (2, 600, 4))):
        a = rng.choice([-32768, 32767, -1, 0, 255, -256, 65535], size=shape_a)
        b = rng.choice([-32768, 32767, -1, 0, 255, -256], size=shape_b)
        got = mm16(torch.as_tensor(a), torch.as_tensor(b)).numpy()
        np.testing.assert_array_equal(got, np.matmul(a.astype(object), b.astype(object)).astype(np.int64))
    # Not the defaults on this machine: set the wrong values, then let the module put them back.
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.set_float32_matmul_precision("medium")
    exact_mode()
    assert torch.backends.cuda.matmul.allow_tf32 is False
    assert torch.backends.cudnn.allow_tf32 is False
    assert torch.get_float32_matmul_precision() == "highest"


@pytest.mark.skipif(not torch.cuda.is_available(), reason="no CUDA device")
def test_fast_l2_on_the_gpu_equals_the_reference(tmp_path):
    graph, loader = recovered_hf(tiny(tmp_path, "qwen2"))
    ref = build(graph, loader, TOKENS, weight_format="int4", group_size=16, per_layer=True, residual_bits=24)
    fast = TorchL2(ref, "cuda")
    assert fast.device.type == "cuda"
    same_run(ref, fast, TOKENS)


def test_a_batch_computes_every_sequence_as_alone(tmp_path):
    graph, loader = recovered_hf(tiny(tmp_path, "qwen2"))
    ref = build(graph, loader, TOKENS, weight_format="int4", group_size=16, per_layer=True, residual_bits=24)
    fast = TorchL2(ref)
    prompts = [TOKENS[:3], TOKENS, TOKENS[4:9], [7]]
    # Without a cache: every position of every sequence, padded to the longest.
    for got, p in zip(fast.forward_batch(prompts), prompts, strict=True):
        np.testing.assert_array_equal(got, ref.forward(p))
    # With a cache: prompts into slots, then steps where every slot is at a different position,
    # then a slot reused for a new sequence.
    cache = TorchKVCache(slots=4)
    last = fast.forward_batch(prompts, cache, [0, 1, 2, 3], [[len(p) - 1] for p in prompts])
    refs = [graph_l2.KVCache() for _ in prompts]
    for b, p in enumerate(prompts):
        np.testing.assert_array_equal(last[b][0], ref.forward(p, cache=refs[b])[-1])
    for step in range(3):
        nxt = [[int(np.argmax(x[-1])) + step] for x in last]
        last = fast.forward_batch(nxt, cache, [0, 1, 2, 3])
        for b in range(4):
            np.testing.assert_array_equal(last[b][0], ref.forward(nxt[b], cache=refs[b])[0], err_msg=f"{step} {b}")
    cache.reset(2)
    again = fast.forward_batch([TOKENS[:6]], cache, [2], [[5]])
    np.testing.assert_array_equal(again[0][0], ref.forward(TOKENS[:6])[5])
    assert cache.lengths == [6, 15, 6, 4]
