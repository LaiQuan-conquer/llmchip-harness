"""Census, graph recovery and the graph interpreter, checked against the official implementations.

Each Llama-family variant is built as a tiny random model with transformers
(fixed seed), saved as a real Hugging Face checkpoint, then imported the way a
downloaded model is: census from the headers, graph from the census, L0 by
interpreting the graph. Its logits must match transformers, and its greedy
continuation must be identical.
"""

import json
import struct
from pathlib import Path

import numpy as np
import pytest
from llmchip_model import Model, load_llama2c
from llmchip_model.census import census, classify
from llmchip_model.execute import forward, greedy
from llmchip_model.formats import WeightLoader, read_layout
from llmchip_model.graph import recover

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")

ROOT = Path(__file__).parents[3]
STORIES = ROOT / "tests" / "fixtures" / "models" / "stories260K" / "stories260K.bin"
TINY = {
    "hidden_size": 32,
    "intermediate_size": 64,
    "num_hidden_layers": 2,
    "num_attention_heads": 4,
    "num_key_value_heads": 2,
    "vocab_size": 128,
    "max_position_embeddings": 64,
}
TOKENS = [5, 17, 3, 99, 42, 7, 64, 1, 23, 88]


def save(tmp_path: Path, config) -> Path:
    torch.manual_seed(0)
    model = transformers.AutoModelForCausalLM.from_config(config).to(torch.bfloat16)
    model.save_pretrained(tmp_path)
    return tmp_path


def add_tensor_copy(path: Path, source: str, name: str) -> None:
    """Rewrites a safetensors file with tensor `name` added as a byte copy of `source`."""
    data = path.read_bytes()
    (n,) = struct.unpack_from("<Q", data, 0)
    header = json.loads(data[8 : 8 + n])
    body = data[8 + n :]
    begin, end = header[source]["data_offsets"]
    header[name] = {**header[source], "data_offsets": [len(body), len(body) + end - begin]}
    raw = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + body + body[begin:end])


def recovered(directory: Path):
    path = directory / "model.safetensors"
    layout = read_layout(path)
    c = census([layout], json.loads((directory / "config.json").read_text()), paths={path.name: path})
    return c, recover(c), WeightLoader([layout], {path.name: path})


def reference(directory: Path, tokens: list[int], steps: int):
    model = transformers.AutoModelForCausalLM.from_pretrained(
        directory, dtype=torch.float32, attn_implementation="eager"
    )
    with torch.no_grad():
        logits = model(torch.tensor([tokens])).logits[0].numpy()
        seq = list(tokens)
        for _ in range(steps):
            seq.append(int(model(torch.tensor([seq])).logits[0, -1].argmax()))
    return logits, seq[len(tokens) :]


@pytest.mark.parametrize(
    ("name", "config", "family", "decisions"),
    [
        (
            "llama-tied",
            transformers.LlamaConfig(**TINY, tie_word_embeddings=True),
            "llama",
            {"tied": True, "qkNorm": False, "qkvBias": False},
        ),
        (
            "llama",
            transformers.LlamaConfig(**TINY, tie_word_embeddings=False, rope_theta=500000.0),
            "llama",
            {"tied": False},
        ),
        (
            "mistral-window",
            transformers.MistralConfig(**TINY, sliding_window=4, tie_word_embeddings=False),
            "llama",
            {"window": 4},
        ),
        (
            "qwen2",
            transformers.Qwen2Config(**TINY, tie_word_embeddings=False, use_sliding_window=False),
            "qwen2",
            {"qkvBias": True, "qkNorm": False},
        ),
        (
            "qwen3",
            transformers.Qwen3Config(**TINY, head_dim=16, tie_word_embeddings=True),
            "qwen3",
            {"qkNorm": True, "qkvBias": False},
        ),
    ],
)
def test_recovered_graph_matches_transformers(tmp_path, name, config, family, decisions):
    directory = save(tmp_path, config)
    c, g, w = recovered(directory)
    assert all(check["ok"] for check in c["checks"]), c["checks"]
    assert g["supported"] and g["family"] == family
    values = {d["id"]: d["value"] for d in g["decisions"]}
    for key, value in decisions.items():
        assert values[key] == value, (key, values[key])
    assert g["coverage"]["unmatched"] == [] and g["coverage"]["missing"] == []
    ours = forward(g, w.get, TOKENS)
    generated, _ = greedy(g, w.get, TOKENS, 6)
    ref, ref_generated = reference(directory, TOKENS, 6)
    assert np.abs(ours - ref).max() / np.abs(ref).max() < 1e-4, name
    assert generated == ref_generated, name


def test_llama2c_graph_matches_run_c_port():
    layout = read_layout(STORIES)
    c = census([layout], None)
    g = recover(c)
    assert g["family"] == "llama2c" and g["hyper"]["ropeStyle"] == "interleaved"
    assert {x["reason"] for x in g["coverage"]["ignored"]} == {"ROPE_TABLE_UNUSED"}
    cfg, weights = load_llama2c(STORIES.read_bytes())
    model = Model(cfg, weights)
    tokens = [1, 403, 407, 261, 378]
    port = np.stack([model.forward(t, pos) for pos, t in enumerate(tokens)])
    ours = forward(g, WeightLoader([layout], {STORIES.name: STORIES}).get, tokens)
    assert np.abs(ours - port).max() < 1e-4


def test_census_counts_a_duplicated_tied_output_once(tmp_path):
    directory = save(tmp_path, transformers.LlamaConfig(**TINY, tie_word_embeddings=True))
    # Write the tied output layer a second time, as some checkpoints (Qwen3-0.6B) do.
    add_tensor_copy(directory / "model.safetensors", "model.embed_tokens.weight", "lm_head.weight")
    c, g, _ = recovered(directory)
    assert c["totals"]["duplicateOutput"] == "lm_head.weight"
    assert c["totals"]["storedParameters"] - c["totals"]["parameters"] == 128 * 32
    assert g["coverage"]["ignored"] == [{"name": "lm_head.weight", "reason": "DUPLICATE_OF_EMBEDDING"}]


def test_census_detects_a_truncated_file(tmp_path):
    directory = save(tmp_path, transformers.LlamaConfig(**TINY))
    path = directory / "model.safetensors"
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    c, _, w = recovered(directory)
    assert {x["id"]: x["ok"] for x in c["checks"]}["bytes"] is False
    last = max(c["tensors"], key=lambda t: t["offset"])["name"]
    with pytest.raises(ValueError, match="truncated"):
        w.get(last)


def test_unknown_tensors_and_unsupported_models_are_reported_not_guessed(tmp_path):
    assert classify("transformer.h.0.attn.c_attn.weight") == "qkv"
    assert classify("model.layers.0.mystery.weight") == "other"
    directory = save(tmp_path, transformers.LlamaConfig(**TINY))
    path = directory / "model.safetensors"
    layout = read_layout(path)
    config = json.loads((directory / "config.json").read_text())
    assert recover(census([layout], None))["reason"]["code"] == "CONFIG_MISSING"
    assert recover(census([layout], config | {"model_type": "gpt2"}))["reason"]["code"] == "FAMILY_UNSUPPORTED"
    assert census([layout], config)["hyper"]["ropeTheta"]["value"] == config["rope_parameters"]["rope_theta"]
    assert (
        census([layout], {k: v for k, v in config.items() if k != "rope_parameters"} | {"rope_theta": 123.0})["hyper"][
            "ropeTheta"
        ]["value"]
        == 123.0
    )
    scaled = config | {"rope_scaling": {"rope_type": "llama3", "factor": 8.0}}
    assert recover(census([layout], scaled))["reason"]["code"] == "ROPE_SCALING_UNSUPPORTED"
    assert recover(census([layout], config | {"hidden_act": "gelu"}))["reason"]["code"] == "ACTIVATION_UNSUPPORTED"


def test_llama2c_header_layout_covers_the_file():
    layout = read_layout(STORIES)
    assert struct.unpack_from("<7i", STORIES.read_bytes(), 0)[0] == 64
    assert sum(t.nbytes for t in layout.tensors) + 28 == layout.size
