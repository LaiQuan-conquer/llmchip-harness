"""NumPy float32 interpreter of a recovered graph (L0).

The L0 reference executes the graph produced by `graph.recover`, node by node,
over the whole token sequence at once (no KV cache). Matching the official
implementation therefore proves the recovered graph, not a separate port.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

Weights = Callable[[str], np.ndarray]


def rmsnorm(x: np.ndarray, w: np.ndarray, eps: float) -> np.ndarray:
    var = np.mean(x * x, axis=-1, keepdims=True, dtype=np.float32)
    return (w * (x / np.sqrt(var + np.float32(eps)))).astype(np.float32)


def rope(x: np.ndarray, heads: int, hd: int, theta: float, style: str, pos: np.ndarray) -> np.ndarray:
    t = x.reshape(x.shape[0], heads, hd)
    inv = (
        np.float32(1.0) / np.power(np.float32(theta), np.arange(0, hd, 2, dtype=np.float32) / np.float32(hd))
    ).astype(np.float32)
    freqs = (pos.astype(np.float32)[:, None] * inv[None, :]).astype(np.float32)  # (T, hd/2)
    cos, sin = np.cos(freqs)[:, None, :], np.sin(freqs)[:, None, :]
    if style == "half":
        # Hugging Face layout: the first and second halves of each head are rotated together.
        a, b = t[..., : hd // 2], t[..., hd // 2 :]
        out = np.concatenate([a * cos - b * sin, b * cos + a * sin], axis=-1)
    else:
        # llama.c layout: adjacent pairs are rotated together.
        a, b = t[..., 0::2], t[..., 1::2]
        out = np.empty_like(t)
        out[..., 0::2], out[..., 1::2] = a * cos - b * sin, a * sin + b * cos
    return out.reshape(x.shape).astype(np.float32)


def attention(
    q: np.ndarray, k: np.ndarray, v: np.ndarray, heads: int, kv_heads: int, hd: int, window: int | None
) -> np.ndarray:
    T = q.shape[0]
    q = q.reshape(T, heads, hd).transpose(1, 0, 2)
    group = heads // kv_heads
    k = np.repeat(k.reshape(T, kv_heads, hd).transpose(1, 0, 2), group, axis=0)
    v = np.repeat(v.reshape(T, kv_heads, hd).transpose(1, 0, 2), group, axis=0)
    scores = (q @ k.transpose(0, 2, 1)) / np.sqrt(np.float32(hd))
    i, j = np.arange(T)[:, None], np.arange(T)[None, :]
    allowed = j <= i
    if window:
        allowed &= j > i - window
    scores = np.where(allowed[None], scores, -np.inf).astype(np.float32)
    scores = np.exp(scores - scores.max(axis=-1, keepdims=True))
    probs = (scores / scores.sum(axis=-1, keepdims=True)).astype(np.float32)
    return (probs @ v).transpose(1, 0, 2).reshape(T, heads * hd).astype(np.float32)


def run_node(node: dict, env: dict[str, np.ndarray], w: Weights, i: int | None, pos: np.ndarray) -> None:
    def weight(key: str) -> np.ndarray:
        name = node[key] if i is None else node[key].format(i=i)
        return w(name)

    op, a = node["op"], node.get("attrs", {})
    ins = [env[x] for x in node["inputs"]]
    if op == "embedding":
        out = [weight("weight")[ins[0]]]
    elif op == "rmsnorm":
        out = [rmsnorm(ins[0], weight("weight"), a["eps"])]
    elif op == "linear":
        y = ins[0] @ weight("weight").T
        if node.get("bias"):
            y = y + weight("bias")
        out = [y.astype(np.float32)]
    elif op == "head_rmsnorm":
        x = ins[0].reshape(ins[0].shape[0], a["heads"], a["headDim"])
        out = [rmsnorm(x, weight("weight"), a["eps"]).reshape(ins[0].shape)]
    elif op == "rope":
        out = [
            rope(ins[0], a["heads"], a["headDim"], a["theta"], a["style"], pos),
            rope(ins[1], a["kvHeads"], a["headDim"], a["theta"], a["style"], pos),
        ]
    elif op == "attention":
        out = [attention(*ins, a["heads"], a["kvHeads"], a["headDim"], a.get("window"))]
    elif op == "add":
        out = [(ins[0] + ins[1]).astype(np.float32)]
    elif op == "silu":
        x = ins[0]
        with np.errstate(over="ignore"):  # exp(-x) overflows to inf for large negative x; x / inf is the right 0
            out = [(x / (np.float32(1.0) + np.exp(-x))).astype(np.float32)]
    elif op == "mul":
        out = [(ins[0] * ins[1]).astype(np.float32)]
    elif op == "argmax":
        out = [np.argmax(ins[0], axis=-1)]
    else:
        raise ValueError(f"unknown op {op}")
    for name, value in zip(node["outputs"], out, strict=True):
        env[name] = value


Observer = Callable[[dict, list[np.ndarray], int | None], None]


def forward(
    graph: dict,
    w: Weights,
    tokens: list[int],
    hidden: list[np.ndarray] | None = None,
    observe: Observer | None = None,
) -> np.ndarray:
    """Logits (T, vocab) for every position of `tokens`.

    `hidden`, when given, receives the residual stream after the embedding, after
    each decoder layer, and after the final norm (layers + 2 arrays of shape (T, d)).
    `observe(node, outputs, layer)` is called after every node (e.g. to calibrate a quantization).
    """
    if not graph.get("supported"):
        raise ValueError("the graph was not recovered")
    nodes = graph["nodes"]
    env: dict[str, np.ndarray] = {"tokens": np.asarray(tokens, dtype=np.int64)}
    pos = np.arange(len(tokens))

    def run(node: dict, i: int | None) -> None:
        run_node(node, env, w, i, pos)
        if observe is not None:
            observe(node, [env[name] for name in node["outputs"]], i)

    for node in nodes["prologue"]:
        run(node, None)
    if hidden is not None:
        hidden.append(env["x"])
    for i in range(nodes["repeat"]):
        for node in nodes["block"]:
            run(node, i)
        if hidden is not None:
            hidden.append(env["x"])
    for node in nodes["epilogue"]:
        if node["op"] == "argmax":
            continue
        run(node, None)
        if hidden is not None and node["op"] == "rmsnorm":
            hidden.append(env[node["outputs"][0]])
    return env["logits"]


def greedy(graph: dict, w: Weights, tokens: list[int], steps: int) -> tuple[list[int], np.ndarray]:
    """Greedy continuation (argmax, lowest id on ties) and the logits after the prompt."""
    out = list(tokens)
    first: np.ndarray | None = None
    for _ in range(steps):
        logits = forward(graph, w, out)[-1]
        if first is None:
            first = logits
        out.append(int(np.argmax(logits)))
    if first is None:
        first = forward(graph, w, out)[-1]
    return out[len(tokens) :], first
