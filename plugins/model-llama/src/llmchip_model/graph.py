"""Graph recovery: from a census (tensor table + config) to the computation graph.

A checkpoint is a bag of tensors; the graph says what is computed with them, in
which order. This module recovers it for the Llama family:

- `llama`   Llama 2/3, Mistral, SmolLM (Hugging Face layout);
- `qwen2`   as llama, with biases on the q/k/v projections;
- `qwen3`   as llama, with an RMSNorm over each head of q and k before RoPE;
- `llama2c` llama2.c checkpoints (Meta layout, interleaved RoPE).

The graph is a list of nodes in execution order: a prologue, one decoder block
(repeated for every layer, weight names contain `{i}`) and an epilogue. Every
decision taken while recovering it is recorded with its evidence, and every
tensor in the files is either bound to exactly one node or listed as ignored
with a reason. Anything else returns `supported: false` with the reason: the
graph is never guessed.
"""

from __future__ import annotations

FORMAT = "llmchip-model-graph/1"

HF_TYPES = {"llama": "llama", "mistral": "llama", "qwen2": "qwen2", "qwen3": "qwen3"}

# Weight names per layout. `{i}` is the layer index.
HF_NAMES = {
    "embed": "model.embed_tokens.weight",
    "attn_norm": "model.layers.{i}.input_layernorm.weight",
    "q": "model.layers.{i}.self_attn.q_proj.weight",
    "k": "model.layers.{i}.self_attn.k_proj.weight",
    "v": "model.layers.{i}.self_attn.v_proj.weight",
    "q_bias": "model.layers.{i}.self_attn.q_proj.bias",
    "k_bias": "model.layers.{i}.self_attn.k_proj.bias",
    "v_bias": "model.layers.{i}.self_attn.v_proj.bias",
    "q_norm": "model.layers.{i}.self_attn.q_norm.weight",
    "k_norm": "model.layers.{i}.self_attn.k_norm.weight",
    "o": "model.layers.{i}.self_attn.o_proj.weight",
    "mlp_norm": "model.layers.{i}.post_attention_layernorm.weight",
    "gate": "model.layers.{i}.mlp.gate_proj.weight",
    "up": "model.layers.{i}.mlp.up_proj.weight",
    "down": "model.layers.{i}.mlp.down_proj.weight",
    "final_norm": "model.norm.weight",
    "lm_head": "lm_head.weight",
}
LLAMA2C_NAMES = {
    "embed": "tok_embeddings.weight",
    "attn_norm": "layers.{i}.attention_norm.weight",
    "q": "layers.{i}.attention.wq.weight",
    "k": "layers.{i}.attention.wk.weight",
    "v": "layers.{i}.attention.wv.weight",
    "o": "layers.{i}.attention.wo.weight",
    "mlp_norm": "layers.{i}.ffn_norm.weight",
    "gate": "layers.{i}.feed_forward.w1.weight",
    "up": "layers.{i}.feed_forward.w3.weight",
    "down": "layers.{i}.feed_forward.w2.weight",
    "final_norm": "norm.weight",
    "lm_head": "output.weight",
}


def _unsupported(code: str, **params) -> dict:
    return {"format": FORMAT, "supported": False, "reason": {"code": code, "params": params}}


def _v(census: dict, key: str):
    return census["hyper"].get(key, {}).get("value")


def _src(census: dict, key: str) -> str | None:
    return census["hyper"].get(key, {}).get("source")


def recover(census: dict) -> dict:
    model_type = _v(census, "modelType")
    if model_type == "llama2c":
        family, names = "llama2c", LLAMA2C_NAMES
    elif census.get("config") is None:
        return _unsupported("CONFIG_MISSING", formats=census.get("fileFormats", []))
    elif model_type in HF_TYPES:
        family, names = HF_TYPES[model_type], HF_NAMES
    else:
        return _unsupported("FAMILY_UNSUPPORTED", modelType=model_type, architecture=_v(census, "architecture"))
    if "gguf" in census.get("fileFormats", []):
        return _unsupported("QUANTIZED_FORMAT", formats=census["fileFormats"])
    act = _v(census, "activation") or "silu"
    if act != "silu":
        return _unsupported("ACTIVATION_UNSUPPORTED", activation=act)
    scaling = _v(census, "ropeScaling")
    if scaling and scaling.get("rope_type", scaling.get("type")) not in (None, "default"):
        return _unsupported("ROPE_SCALING_UNSUPPORTED", ropeScaling=scaling)

    tensors = {t["name"]: t for t in census["tensors"]}
    L, d, H, KV, hd, ffn, V = (
        _v(census, k) for k in ("layers", "hidden", "heads", "kvHeads", "headDim", "intermediate", "vocab")
    )
    if None in (L, d, H, KV, hd, ffn, V):
        missing = [
            k
            for k, v in zip(
                ("layers", "hidden", "heads", "kvHeads", "headDim", "intermediate", "vocab"),
                (L, d, H, KV, hd, ffn, V),
                strict=True,
            )
            if v is None
        ]
        return _unsupported("HYPER_MISSING", missing=missing)
    eps = float(_v(census, "normEps") or 1e-6)
    theta = float(_v(census, "ropeTheta") or 10000.0)
    qk_norm = names.get("q_norm", "").format(i=0) in tensors
    qkv_bias = names.get("q_bias", "").format(i=0) in tensors
    tied = bool(census["totals"]["tiedOutput"])
    window = _v(census, "slidingWindow") if family == "llama" and _v(census, "useSlidingWindow") is not False else None
    rope_style = "interleaved" if family == "llama2c" else "half"

    decisions = [
        {
            "id": "family",
            "value": family,
            "params": {"modelType": model_type, "architecture": _v(census, "architecture")},
            "source": _src(census, "modelType"),
        },
        {
            "id": "gqa",
            "value": H // KV,
            "params": {"heads": H, "kvHeads": KV, "headDim": hd},
            "source": _src(census, "kvHeads"),
        },
        {
            "id": "qkvBias",
            "value": qkv_bias,
            "params": {"tensor": names.get("q_bias", "").format(i=0) if qkv_bias else None},
            "source": "tensors",
        },
        {
            "id": "qkNorm",
            "value": qk_norm,
            "params": {"tensor": names["q_norm"].format(i=0) if qk_norm else None},
            "source": "tensors",
        },
        {
            "id": "rope",
            "value": rope_style,
            "params": {"theta": theta, "headDim": hd},
            "source": _src(census, "ropeTheta"),
        },
        {"id": "norm", "value": "rmsnorm", "params": {"eps": eps}, "source": _src(census, "normEps")},
        {"id": "activation", "value": "swiglu", "params": {"activation": act}, "source": _src(census, "activation")},
        {
            "id": "tied",
            "value": tied,
            "params": {"duplicate": census["totals"].get("duplicateOutput")},
            "source": _src(census, "tied"),
        },
        {"id": "window", "value": window, "params": {}, "source": _src(census, "slidingWindow") if window else None},
        {"id": "decoding", "value": "greedy", "params": {}, "source": "workbench rule: argmax, lowest id on ties"},
    ]

    def linear(
        nid: str, src: str, dst: str, w: str, n_out: int, n_in: int, bias: str | None = None, sym: str | None = None
    ) -> dict:
        node = {
            "id": nid,
            "op": "linear",
            "inputs": [src],
            "outputs": [dst],
            "weight": w,
            "shape": ["T", sym or str(n_out)],
            "dims": [n_out, n_in],
            "macsPerToken": n_out * n_in,
        }
        if bias:
            node["bias"] = bias
        return node

    def rmsnorm(nid: str, src: str, dst: str, w: str) -> dict:
        return {
            "id": nid,
            "op": "rmsnorm",
            "inputs": [src],
            "outputs": [dst],
            "weight": w,
            "attrs": {"eps": eps},
            "shape": ["T", "d"],
            "dims": [d],
            "macsPerToken": 0,
        }

    prologue = [
        {
            "id": "embed",
            "op": "embedding",
            "inputs": ["tokens"],
            "outputs": ["x"],
            "weight": names["embed"],
            "shape": ["T", "d"],
            "dims": [V, d],
            "macsPerToken": 0,
        }
    ]
    block = [rmsnorm("attn_norm", "x", "h", names["attn_norm"])]
    block += [
        linear("q_proj", "h", "q", names["q"], H * hd, d, names["q_bias"] if qkv_bias else None, "H·hd"),
        linear("k_proj", "h", "k", names["k"], KV * hd, d, names["k_bias"] if qkv_bias else None, "KV·hd"),
        linear("v_proj", "h", "v", names["v"], KV * hd, d, names["v_bias"] if qkv_bias else None, "KV·hd"),
    ]
    if qk_norm:
        block += [
            {
                "id": "q_norm",
                "op": "head_rmsnorm",
                "inputs": ["q"],
                "outputs": ["q"],
                "weight": names["q_norm"],
                "attrs": {"eps": eps, "heads": H, "headDim": hd},
                "shape": ["T", "H·hd"],
                "dims": [hd],
                "macsPerToken": 0,
            },
            {
                "id": "k_norm",
                "op": "head_rmsnorm",
                "inputs": ["k"],
                "outputs": ["k"],
                "weight": names["k_norm"],
                "attrs": {"eps": eps, "heads": KV, "headDim": hd},
                "shape": ["T", "KV·hd"],
                "dims": [hd],
                "macsPerToken": 0,
            },
        ]
    block += [
        {
            "id": "rope",
            "op": "rope",
            "inputs": ["q", "k"],
            "outputs": ["q", "k"],
            "attrs": {"theta": theta, "style": rope_style, "headDim": hd, "heads": H, "kvHeads": KV},
            "shape": ["T", "H·hd"],
            "macsPerToken": 0,
        },
        {
            "id": "attention",
            "op": "attention",
            "inputs": ["q", "k", "v"],
            "outputs": ["a"],
            "attrs": {
                "heads": H,
                "kvHeads": KV,
                "headDim": hd,
                "causal": True,
                "window": window,
                "scale": f"1/sqrt({hd})",
            },
            "shape": ["T", "H·hd"],
            "macsPerToken": None,
            "macsFormula": f"2·{H}·{hd}·context",
        },
        linear("o_proj", "a", "o", names["o"], d, H * hd, sym="d"),
        {
            "id": "residual_attn",
            "op": "add",
            "inputs": ["x", "o"],
            "outputs": ["x"],
            "shape": ["T", "d"],
            "macsPerToken": 0,
        },
        rmsnorm("mlp_norm", "x", "h", names["mlp_norm"]),
        linear("gate_proj", "h", "g", names["gate"], ffn, d, sym="ffn"),
        linear("up_proj", "h", "u", names["up"], ffn, d, sym="ffn"),
        {"id": "act", "op": "silu", "inputs": ["g"], "outputs": ["g"], "shape": ["T", "ffn"], "macsPerToken": 0},
        {
            "id": "gate_mul",
            "op": "mul",
            "inputs": ["g", "u"],
            "outputs": ["m"],
            "shape": ["T", "ffn"],
            "macsPerToken": 0,
        },
        linear("down_proj", "m", "f", names["down"], d, ffn, sym="d"),
        {
            "id": "residual_mlp",
            "op": "add",
            "inputs": ["x", "f"],
            "outputs": ["x"],
            "shape": ["T", "d"],
            "macsPerToken": 0,
        },
    ]
    head = names["embed"] if tied else names["lm_head"]
    epilogue = [
        rmsnorm("final_norm", "x", "h", names["final_norm"]),
        linear("lm_head", "h", "logits", head, V, d, sym="V") | {"tiedTo": names["embed"] if tied else None},
        {
            "id": "next_token",
            "op": "argmax",
            "inputs": ["logits"],
            "outputs": ["token"],
            "shape": ["1"],
            "macsPerToken": 0,
            "attrs": {"rule": "lowest id on ties", "where": "host"},
        },
    ]

    # Every file tensor is bound to exactly one node, or ignored with a reason.
    bound: dict[str, str] = {}
    missing: list[str] = []

    def bind(name: str, node: str) -> None:
        if name in tensors:
            bound.setdefault(name, node)
        else:
            missing.append(name)

    for n in prologue + epilogue:
        if n.get("weight"):
            bind(n["weight"], n["id"])
    for i in range(L):
        for n in block:
            for key in ("weight", "bias"):
                if n.get(key):
                    bind(n[key].format(i=i), f"{n['id']}[{i}]")
    ignored = []
    for name, t in tensors.items():
        if name in bound:
            continue
        if t["role"] == "rope_table":
            ignored.append({"name": name, "reason": "ROPE_TABLE_UNUSED"})
        elif tied and t["role"] == "lm_head" and census["totals"].get("duplicateOutput") == name:
            ignored.append({"name": name, "reason": "DUPLICATE_OF_EMBEDDING"})
    unmatched = [name for name in tensors if name not in bound and name not in {x["name"] for x in ignored}]

    macs: dict[str, int] = {}
    for n in prologue + epilogue:
        macs[n["op"]] = macs.get(n["op"], 0) + (n["macsPerToken"] or 0)
    for n in block:
        macs[n["op"]] = macs.get(n["op"], 0) + L * (n["macsPerToken"] or 0)

    return {
        "format": FORMAT,
        "supported": True,
        "reason": None,
        "family": family,
        "architecture": _v(census, "architecture") or ("llama2.c" if family == "llama2c" else None),
        "hyper": {
            "layers": L,
            "hidden": d,
            "heads": H,
            "kvHeads": KV,
            "headDim": hd,
            "intermediate": ffn,
            "vocab": V,
            "normEps": eps,
            "ropeTheta": theta,
            "ropeStyle": rope_style,
            "qkNorm": qk_norm,
            "qkvBias": qkv_bias,
            "tied": tied,
            "window": window,
        },
        "decisions": decisions,
        "nodes": {"prologue": prologue, "block": block, "epilogue": epilogue, "repeat": L},
        "coverage": {
            "tensors": len(tensors),
            "bound": len(bound),
            "boundTo": bound,
            "ignored": ignored,
            "unmatched": unmatched,
            "missing": missing,
        },
        "macsPerTokenByOp": macs,
        "macsPerToken": sum(macs.values()),
    }
