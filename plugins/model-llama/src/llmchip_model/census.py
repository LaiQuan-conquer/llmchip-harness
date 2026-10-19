"""Tensor census: what the weight files contain, before anything is computed.

From headers (and config.json when present) the census says, for every tensor,
which role it plays; derives the model's hyper-parameters and where each value
came from; counts parameters and multiply-accumulates per token; and checks
itself. Every check can fail:

- every tensor has a known role;
- the header accounts for every byte of each file (no gaps, overlaps or size mismatches);
- all layers have the same tensors with the same shapes;
- config.json agrees with the tensor shapes;
- the output layer is either stored or declared tied to the embedding; when it is
  both stored and declared tied, the two copies must be byte-identical (some
  checkpoints, e.g. Qwen3-0.6B, store the tied output layer a second time).
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

from .formats import FileLayout, TensorInfo

# (pattern, role); first match wins. Covers Hugging Face, Meta/llama2.c, GGUF and GPT-2 names.
ROLE_PATTERNS: list[tuple[str, str]] = [
    (r"(^|\.)(embed_tokens|tok_embeddings|token_embd|wte|word_embeddings)\.weight$", "embed"),
    (r"(^|\.)(wpe|position_embeddings)\.weight$", "pos_embed"),
    (r"(^|\.)(input_layernorm|attention_norm|attn_norm|ln_1)\.(weight|bias)$", "attn_norm"),
    (r"(^|\.)(post_attention_layernorm|ffn_norm|ln_2)\.(weight|bias)$", "mlp_norm"),
    (r"(^|\.)(self_attn\.q_norm|attn_q_norm)\.weight$", "q_norm"),
    (r"(^|\.)(self_attn\.k_norm|attn_k_norm)\.weight$", "k_norm"),
    (r"(^|\.)(self_attn\.q_proj|attention\.wq|attn_q)\.weight$", "q"),
    (r"(^|\.)(self_attn\.k_proj|attention\.wk|attn_k)\.weight$", "k"),
    (r"(^|\.)(self_attn\.v_proj|attention\.wv|attn_v)\.weight$", "v"),
    (r"(^|\.)(self_attn\.o_proj|attention\.wo|attn_output)\.weight$", "o"),
    (r"(^|\.)(self_attn\.q_proj|attn_q)\.bias$", "q_bias"),
    (r"(^|\.)(self_attn\.k_proj|attn_k)\.bias$", "k_bias"),
    (r"(^|\.)(self_attn\.v_proj|attn_v)\.bias$", "v_bias"),
    (r"(^|\.)(self_attn\.o_proj|attn_output)\.bias$", "o_bias"),
    (r"(^|\.)attn\.c_attn\.(weight|bias)$", "qkv"),
    (r"(^|\.)attn\.c_proj\.(weight|bias)$", "o"),
    (r"(^|\.)(mlp\.gate_proj|feed_forward\.w1|ffn_gate)\.weight$", "gate"),
    (r"(^|\.)(mlp\.up_proj|feed_forward\.w3|ffn_up|mlp\.c_fc)\.(weight|bias)$", "up"),
    (r"(^|\.)(mlp\.down_proj|feed_forward\.w2|ffn_down|mlp\.c_proj)\.(weight|bias)$", "down"),
    (r"(^|\.)(mlp\.router|mlp\.gate|ffn_gate_inp)\.weight$", "router"),
    (r"(^|\.)experts\.", "expert"),
    (r"^(model\.norm|norm|output_norm|transformer\.ln_f|ln_f)\.(weight|bias)$", "final_norm"),
    (r"^(lm_head|output)\.weight$", "lm_head"),
    (r"^freq_cis_(real|imag)$", "rope_table"),
]

GROUP = {
    "embed": "embedding",
    "pos_embed": "embedding",
    "lm_head": "lm_head",
    "q": "attention",
    "k": "attention",
    "v": "attention",
    "o": "attention",
    "qkv": "attention",
    "q_bias": "attention",
    "k_bias": "attention",
    "v_bias": "attention",
    "o_bias": "attention",
    "gate": "mlp",
    "up": "mlp",
    "down": "mlp",
    "router": "mlp",
    "expert": "mlp",
    "attn_norm": "norm",
    "mlp_norm": "norm",
    "final_norm": "norm",
    "q_norm": "norm",
    "k_norm": "norm",
    "rope_table": "unused",
    "other": "other",
}
#: roles whose weights are multiplied once per token per weight (batch 1)
GEMM_ROLES = {"q", "k", "v", "o", "qkv", "gate", "up", "down", "lm_head", "router"}
LAYER_RE = re.compile(r"(?:^|\.)(?:layers|blk|h)\.(\d+)\.")


def classify(name: str) -> str:
    for pattern, role in ROLE_PATTERNS:
        if re.search(pattern, name):
            return role
    return "other"


def layer_of(name: str) -> int | None:
    m = LAYER_RE.search(name)
    return int(m.group(1)) if m else None


def _hyper(config: dict | None, layouts: list[FileLayout], tensors: list[TensorInfo], roles: dict[str, str]) -> dict:
    """Hyper-parameters, each as {value, source}: config.json, the file header, or a tensor shape."""
    h: dict[str, dict] = {}

    def put(key: str, value, source: str) -> None:
        if value is not None and key not in h:
            h[key] = {"value": value, "source": source}

    first = {role: t for t in reversed(tensors) for role in [roles[t.name]]}  # first tensor of each role
    if config:
        for key, field in [
            ("modelType", "model_type"),
            ("hidden", "hidden_size"),
            ("layers", "num_hidden_layers"),
            ("heads", "num_attention_heads"),
            ("kvHeads", "num_key_value_heads"),
            ("headDim", "head_dim"),
            ("intermediate", "intermediate_size"),
            ("vocab", "vocab_size"),
            ("normEps", "rms_norm_eps"),
            ("ropeTheta", "rope_theta"),
            ("ropeScaling", "rope_scaling"),
            ("tied", "tie_word_embeddings"),
            ("activation", "hidden_act"),
            ("attentionBias", "attention_bias"),
            ("maxPositions", "max_position_embeddings"),
            ("slidingWindow", "sliding_window"),
            ("useSlidingWindow", "use_sliding_window"),
            ("dtype", "torch_dtype"),
        ]:
            if field in config:
                put(key, config[field], f"config.json:{field}")
        # transformers >= 5 writes RoPE settings as rope_parameters: {rope_theta, rope_type, ...}.
        rope = config.get("rope_parameters")
        if isinstance(rope, dict):
            put("ropeTheta", rope.get("rope_theta"), "config.json:rope_parameters.rope_theta")
            if rope.get("rope_type") not in (None, "default"):
                put("ropeScaling", rope, "config.json:rope_parameters")
        if config.get("architectures"):
            put("architecture", config["architectures"][0], "config.json:architectures")
        if "num_attention_heads" in config and "num_key_value_heads" not in config:
            put("kvHeads", config["num_attention_heads"], "config.json:num_attention_heads (no num_key_value_heads)")
    for layout in layouts:
        c = layout.metadata.get("llama2c")
        if c:
            src = f"{layout.name}: llama2.c header"
            put("modelType", "llama2c", src)
            for key, field in [
                ("hidden", "dim"),
                ("layers", "n_layers"),
                ("heads", "n_heads"),
                ("kvHeads", "n_kv_heads"),
                ("intermediate", "hidden_dim"),
                ("vocab", "vocab_size"),
                ("maxPositions", "seq_len"),
            ]:
                put(key, c[field], src)
            put("tied", c["shared_classifier"], src)
            put("normEps", 1e-5, "run.c (fixed)")
            put("ropeTheta", 10000.0, "run.c (fixed)")
            put("activation", "silu", "run.c (fixed)")
        if layout.format == "gguf":
            arch = layout.metadata.get("general.architecture")
            src = f"{layout.name}: GGUF metadata"
            put("modelType", arch, src)
            for key, field in [
                ("hidden", "embedding_length"),
                ("layers", "block_count"),
                ("heads", "attention.head_count"),
                ("kvHeads", "attention.head_count_kv"),
                ("intermediate", "feed_forward_length"),
                ("ropeTheta", "rope.freq_base"),
                ("normEps", "attention.layer_norm_rms_epsilon"),
            ]:
                put(key, layout.metadata.get(f"{arch}.{field}"), src)
    # From shapes, when nothing else said so.
    if "embed" in first:
        e = first["embed"]
        put("vocab", e.shape[0], f"tensor:{e.name}[0]")
        put("hidden", e.shape[1], f"tensor:{e.name}[1]")
    layers = {layer_of(t.name) for t in tensors} - {None}
    if layers:
        put("layers", max(layers) + 1, "tensor names: highest layer index + 1")
    if "gate" in first:
        put("intermediate", first["gate"].shape[0], f"tensor:{first['gate'].name}[0]")
    if "q_norm" in first:
        put("headDim", first["q_norm"].shape[0], f"tensor:{first['q_norm'].name}[0]")
    hidden, heads = h.get("hidden", {}).get("value"), h.get("heads", {}).get("value")
    if "headDim" not in h and hidden and heads:
        put("headDim", hidden // heads, "hidden / heads")
    put(
        "tied",
        "lm_head" not in first,
        "no separate output tensor" if "lm_head" not in first else "output tensor present",
    )
    return h


def _value(h: dict, key: str):
    return h.get(key, {}).get("value")


def _checks(
    config: dict | None, layouts: list[FileLayout], tensors: list[TensorInfo], roles: dict[str, str], h: dict
) -> list[dict]:
    checks = []
    other = [t.name for t in tensors if roles[t.name] == "other"]
    checks.append({"id": "classified", "ok": not other, "params": {"count": len(other), "examples": other[:5]}})

    problems = []
    for layout in layouts:
        if layout.format == "gguf":
            continue  # quantized block sizes vary; the tensor table is still listed
        spans = sorted((t.offset, t.offset + t.nbytes, t.name) for t in layout.tensors)
        cursor = layout.data_start
        for begin, end, name in spans:
            if begin != cursor:
                problems.append(f"{layout.name}: {'gap' if begin > cursor else 'overlap'} before {name}")
            cursor = end
        if cursor != layout.size:
            problems.append(f"{layout.name}: header covers {cursor} of {layout.size} bytes")
        for t in layout.tensors:
            if t.bits and t.nbytes * 8 != t.params * t.bits:
                problems.append(f"{t.name}: {t.nbytes} bytes for {t.params} x {t.bits:g} bit")
    checks.append({"id": "bytes", "ok": not problems, "params": {"count": len(problems), "examples": problems[:5]}})

    by_layer: dict[int, dict[str, list[int]]] = defaultdict(dict)
    for t in tensors:
        i = layer_of(t.name)
        if i is not None:
            by_layer[i][LAYER_RE.sub(".#.", t.name, count=1)] = t.shape
    differ = []
    if by_layer:
        ref_i = min(by_layer)
        ref = by_layer[ref_i]
        for i, shapes in sorted(by_layer.items()):
            if shapes != ref:
                differ.append(i)
    checks.append({"id": "layers", "ok": not differ, "params": {"layers": len(by_layer), "differ": differ[:10]}})

    mismatch = []
    first = {}
    for t in tensors:
        first.setdefault(roles[t.name], t)
    kv, heads, hd = (_value(h, k) for k in ("kvHeads", "heads", "headDim"))
    expect = []
    if "embed" in first:
        expect += [("hidden", first["embed"].shape[1]), ("vocab", first["embed"].shape[0])]
    if "q" in first and heads and hd:
        expect.append(("heads x headDim", (first["q"].shape[0], heads * hd)))
    if "k" in first and kv and hd:
        expect.append(("kvHeads x headDim", (first["k"].shape[0], kv * hd)))
    if "gate" in first:
        expect.append(("intermediate", first["gate"].shape[0]))
    for key, shape_value in expect:
        if isinstance(shape_value, tuple):
            if shape_value[0] != shape_value[1]:
                mismatch.append(f"{key}: tensor {shape_value[0]} vs config {shape_value[1]}")
        elif _value(h, key) is not None and _value(h, key) != shape_value:
            mismatch.append(f"{key}: config {_value(h, key)} vs tensor {shape_value}")
    layers = {layer_of(t.name) for t in tensors} - {None}
    if layers and _value(h, "layers") not in (None, max(layers) + 1):
        mismatch.append(f"layers: config {_value(h, 'layers')} vs tensors {max(layers) + 1}")
    checks.append(
        {
            "id": "config",
            "ok": not mismatch,
            "params": {"present": config is not None, "count": len(mismatch), "examples": mismatch[:5]},
        }
    )

    has_head = "lm_head" in first
    declared = config.get("tie_word_embeddings") if config else None
    ok = has_head or declared is not False
    checks.append({"id": "output", "ok": ok, "params": {"stored": has_head, "declaredTied": declared}})
    return checks


def _same_bytes(a: TensorInfo, b: TensorInfo, paths: dict[str, Path]) -> bool:
    if a.dtype != b.dtype or a.shape != b.shape:
        return False
    digests = []
    for t in (a, b):
        m = np.memmap(paths[t.file], dtype=np.uint8, mode="r")
        digests.append(hashlib.sha256(m[t.offset : t.offset + t.nbytes]).digest())
    return digests[0] == digests[1]


def census(
    layouts: list[FileLayout], config: dict | None, context: int = 64, paths: dict[str, Path] | None = None
) -> dict:
    """`paths` (file name -> path) lets the census compare tensor contents, e.g. a duplicated tied output layer."""
    tensors = [t for layout in layouts for t in layout.tensors]
    roles = {t.name: classify(t.name) for t in tensors}
    h = _hyper(config, layouts, tensors, roles)
    embed = next((t for t in tensors if roles[t.name] == "embed"), None)
    head = next((t for t in tensors if roles[t.name] == "lm_head"), None)
    duplicate = bool(
        config and config.get("tie_word_embeddings") and embed and head and paths and _same_bytes(embed, head, paths)
    )
    if duplicate:
        h["tied"] = {
            "value": True,
            "source": "config.json:tie_word_embeddings; the stored output layer is byte-identical to the embedding",
        }
    groups: dict[str, dict] = defaultdict(lambda: {"tensors": 0, "params": 0, "bytes": 0})
    for t in tensors:
        g = groups[GROUP[roles[t.name]]]
        g["tensors"] += 1
        g["params"] += t.params
        g["bytes"] += t.nbytes
    weight_params = sum(t.params for t in tensors if roles[t.name] != "rope_table") - (head.params if duplicate else 0)
    gemm = sum(t.params for t in tensors if roles[t.name] in GEMM_ROLES and t.name.endswith("weight"))
    tied = duplicate or (bool(_value(h, "tied")) and head is None)
    if tied and not duplicate:
        # The output projection reuses the embedding: one multiply per weight per token.
        gemm += sum(t.params for t in tensors if roles[t.name] == "embed")
    layers, heads, hd = _value(h, "layers"), _value(h, "heads"), _value(h, "headDim")
    attention = 2 * layers * heads * hd * context if layers and heads and hd else None
    checks = _checks(config, layouts, tensors, roles, h)
    if head is not None and config and config.get("tie_word_embeddings") and not duplicate:
        checks[-1] = {"id": "output", "ok": False, "params": {"stored": True, "declaredTied": True, "identical": False}}
    elif duplicate:
        checks[-1]["params"] |= {"identical": True, "duplicateParams": head.params}
    fmt = sorted({layout.format for layout in layouts})
    return {
        "format": "llmchip-model-census/1",
        "fileFormats": fmt,
        "files": [
            {"name": layout.name, "format": layout.format, "bytes": layout.size, "tensors": len(layout.tensors)}
            for layout in layouts
        ],
        "config": config,
        "hyper": h,
        "tensors": [
            t.to_json() | {"role": roles[t.name], "group": GROUP[roles[t.name]], "layer": layer_of(t.name)}
            for t in tensors
        ],
        "totals": {
            "tensors": len(tensors),
            "parameters": weight_params,
            "bytes": sum(t.nbytes for t in tensors),
            "byGroup": dict(groups),
            "tiedOutput": tied,
            #: the tied output layer is also stored, as a byte-identical copy of the embedding
            "duplicateOutput": head.name if duplicate else None,
            "storedParameters": sum(t.params for t in tensors if roles[t.name] != "rope_table"),
        },
        "macsPerToken": {"gemm": gemm, "attention": attention, "context": context, "total": gemm + (attention or 0)},
        "checks": checks,
    }
