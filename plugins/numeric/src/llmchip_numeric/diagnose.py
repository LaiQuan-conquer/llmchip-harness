"""Diagnosis of the answers L0 gets right and L2 gets wrong (requirements FR-QNT-09).

For each such question the two models continue the benchmark's own prompt greedily; the report
gives the first token where the L2's choice differs from the float graph's, and, at that prefix,
the L2's observation points (residual stream after the embedding, after every decoder layer and
of the final norm, dequantized with their fraction bits) against the float graph's: the layer
with the largest relative deviation and its matrix products. That locates the first divergence
in the model, not only in the answer.
"""

from __future__ import annotations

import numpy as np
import torch

from .torch_float import FloatGraph

MATRICES = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")


def _points_float(graph: dict, weight, tokens: list[int]) -> list[np.ndarray]:
    """The float graph's residual after the embedding, after each layer, and the final norm."""
    fg = FloatGraph(graph, weight, "cpu")
    n = graph["nodes"]
    env = {"tokens": torch.as_tensor([tokens])}
    with torch.no_grad():
        fg.run(n["prologue"], None, env)
        points = [env["x"][0].numpy().astype(np.float64)]
        for i in range(n["repeat"]):
            fg.run(n["block"], i, env)
            points.append(env["x"][0].numpy().astype(np.float64))
        norm = [node for node in n["epilogue"] if node["op"] == "rmsnorm"]
        fg.run(norm, None, env)
        points.append(env[norm[0]["outputs"][0]][0].numpy().astype(np.float64))
    return points


def _points_l2(ref, tokens: list[int]) -> list[np.ndarray]:
    """The L2's same observation points as floats (its integers divided by 2^frac)."""
    hidden: list[np.ndarray] = []
    ref.forward(tokens, hidden, rows=[len(tokens) - 1])
    base = getattr(ref, "ref", ref)  # the fast L2 wraps the reference L2
    fracs = [base.f["x"]] * (len(hidden) - 1) + [base.f["xn"]]
    return [h.astype(np.float64) / 2.0**f for h, f in zip(hidden, fracs, strict=True)]


def _greedy(logits) -> int:
    return int(np.argmax(logits))


def diagnose_answer(graph: dict, weight, ref, tokens: list[int], steps: int) -> dict:
    """The L0/L2 continuations of `tokens` and, at their first divergence, the per-layer deviation."""
    fg = FloatGraph(graph, weight, "cpu")
    ids = list(tokens)
    l0: list[int] = []
    l2: list[int] = []
    first = -1
    for _ in range(steps):
        with torch.no_grad():
            a = _greedy(fg.logits(torch.as_tensor([ids]))[0, -1].numpy())
        b = _greedy(ref.forward(ids)[-1])
        l0.append(a)
        l2.append(b)
        if first < 0 and a != b:
            first = len(l0) - 1
            break
        ids.append(a)
    report: dict = {"tokens": len(tokens), "l0": l0, "l2": l2, "firstDivergence": first}
    if first < 0:
        report["layers"] = []
        return report
    prefix = list(tokens) + l0[:first]  # the tokens both models agree on, plus the last agreed one
    fl = _points_float(graph, weight, prefix)
    in_ = _points_l2(ref, prefix)
    layers = []
    for i, (a, b) in enumerate(zip(fl, in_, strict=True)):
        scale = float(np.linalg.norm(a)) or 1.0
        layers.append({"layer": i, "relativeError": float(np.linalg.norm(a - b) / scale)})
    worst = max(layers, key=lambda layer: layer["relativeError"])
    index = worst["layer"] - 1  # layer 0 is the embedding, layer i is decoder layer i - 1
    report["layers"] = layers
    report["worst"] = {
        **worst,
        "matrices": [f"{index}.{name}" for name in MATRICES] if 0 <= index < graph["nodes"]["repeat"] else ["lm_head"],
    }
    return report


def diagnose(graph: dict, weight, ref, tokenizer, prompts: list[tuple[str, str]], steps: int = 16) -> dict:
    """(question id, prompt text) pairs answered correctly by L0 and wrongly by L2."""
    questions = []
    for qid, text in prompts:
        ids = tokenizer(text, add_special_tokens=False)["input_ids"]
        if not ids:
            continue
        report = diagnose_answer(graph, weight, ref, ids, steps)
        questions.append({"id": qid, **report})
    return {
        "questions": len(questions),
        "diverged": sum(1 for q in questions if q["firstDivergence"] >= 0),
        "items": questions,
    }
