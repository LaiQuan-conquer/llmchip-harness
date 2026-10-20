"""Accuracy of the graph L2 against L0 (the float graph), for any recovered model.

The same measures as the chip evaluation (`evaluate.py`), from the same kind of
evaluation set, so that schemes can be compared side by side:

- teacher forcing on the passages: perplexity of L0 and L2, the share of positions
  where both choose the same next token, and the residual stream of every layer
  (L2 read as fixed point) against L0;
- greedy generation from the prompts: token agreement and the first divergence;
- overflow events of L2 by kind.

L0 runs first and its weights are released before L2 runs, so that large models need
the memory of only one of them at a time.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
from llmchip_model.evalset import load_evalset
from llmchip_model.execute import forward

from .evaluate import scheme
from .graph_l2 import GraphL2

DATA = Path(__file__).parents[2] / "data"


def default_evalset(kind: str) -> dict:
    """The bundled evaluation set for a tokenizer kind: TinyStories-style text for llama2.c models."""
    return load_evalset(DATA / ("evalset-tinystories.json" if kind == "llama2c" else "evalset-general.json"))


def select_texts(evalset: dict, tokenizer, max_prompts: int, passage_tokens: int) -> tuple[list[str], list[list[int]]]:
    """The prompts and passages (token ids, truncated) the tokenizer can encode."""
    prompts = [p for p in evalset["prompts"] if tokenizer.encodable(p)][:max_prompts]
    passages = [tokenizer.encode(p)[:passage_tokens] for p in evalset.get("passages", []) if tokenizer.encodable(p)]
    passages = [p for p in passages if len(p) >= 2]
    if not prompts or not passages:
        raise ValueError("the evaluation set has no prompts or passages this model's tokenizer can encode")
    return prompts, passages


def calibration_sequences(evalset: dict, tokenizer, tokens: int) -> list[list[int]]:
    texts = [*(evalset.get("calibration") or evalset["prompts"]), *evalset.get("passages", [])]
    return [tokenizer.encode(t)[:tokens] for t in texts if tokenizer.encodable(t)]


def _nll(logits: np.ndarray, targets: np.ndarray) -> np.ndarray:
    z = logits.astype(np.float64)
    mx = z.max(axis=-1, keepdims=True)
    lse = mx[:, 0] + np.log(np.exp(z - mx).sum(axis=-1))
    return lse - z[np.arange(len(targets)), targets]


def _greedy(step: Callable[[list[int]], np.ndarray], ids: list[int], steps: int) -> list[int]:
    out = list(ids)
    for _ in range(steps):
        out.append(int(np.argmax(step(out)[-1])))
    return out[len(ids) :]


def evaluate_graph(
    graph: dict,
    l0: Callable[[str], np.ndarray],
    release_l0: Callable[[], None],
    l2: GraphL2,
    tokenizer,
    prompts: list[str],
    passages: list[list[int]],
    steps: int,
    progress: Callable[[float], None] = lambda f: None,
) -> dict:
    started = time.monotonic()
    # L0: teacher forcing, residual streams, greedy continuations.
    ref = []
    for ids in passages:
        hidden: list = []
        logits = forward(graph, l0, ids, hidden)
        ref.append({"nll": _nll(logits[:-1], np.asarray(ids[1:])), "top": logits[:-1].argmax(-1), "hidden": hidden})
    progress(0.2)
    encoded = [tokenizer.encode(p) for p in prompts]
    ref_gen = [_greedy(lambda seq: forward(graph, l0, seq), ids, steps) for ids in encoded]
    release_l0()
    progress(0.4)
    l0_seconds = time.monotonic() - started

    # L2: the same, with integer arithmetic.
    started = time.monotonic()
    fl = 2.0**-l2.fl
    fx = 2.0 ** -l2.f["x"]
    layers = graph["hyper"]["layers"]
    worst = [0.0] * layers
    peak = [0.0] * layers
    nll0 = nll2 = 0.0
    count = same_next = 0
    for p, (ids, r) in enumerate(zip(passages, ref, strict=True)):
        hidden: list = []
        logits = l2.forward(ids, hidden)[:-1] * fl
        nll0 += float(r["nll"].sum())
        nll2 += float(_nll(logits, np.asarray(ids[1:])).sum())
        same_next += int((logits.argmax(-1) == r["top"]).sum())
        count += len(ids) - 1
        for i in range(layers):
            ref_x = r["hidden"][i + 1]
            worst[i] = max(worst[i], float(np.abs(hidden[i + 1] * fx - ref_x).max()))
            peak[i] = max(peak[i], float(np.abs(ref_x).max()))
        progress(0.4 + 0.3 * (p + 1) / len(passages))
    rows, matches, positions, divergence = [], 0, 0, []
    for k, (prompt, ids, a) in enumerate(zip(prompts, encoded, ref_gen, strict=True)):
        b = _greedy(lambda seq: l2.forward(seq), ids, steps)
        same = sum(1 for x, y in zip(a, b, strict=True) if x == y)
        first = next((i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y), len(a))
        matches, positions = matches + same, positions + len(a)
        divergence.append(first)
        rows.append(
            {
                "prompt": prompt,
                "l0": tokenizer.continuation(ids, a),
                "l2": tokenizer.continuation(ids, b),
                "agreement": same / len(a) if a else 1.0,
            }
        )
        progress(0.7 + 0.3 * (k + 1) / len(prompts))
    ppl0, ppl2 = math.exp(nll0 / count), math.exp(nll2 / count)
    layer_error = [
        {"layer": i, "point": "ffn_residual", "maxAbs": worst[i], "maxRel": worst[i] / max(peak[i], 1e-30)}
        for i in range(layers)
    ]
    return {
        "metrics": {
            "prompts_evaluated": len(rows),
            "tokens_per_prompt": steps,
            "passage_tokens": count,
            "token_agreement": matches / positions if positions else 1.0,
            "first_divergence_mean": sum(divergence) / len(divergence) if divergence else 0.0,
            "l0_perplexity": ppl0,
            "l2_perplexity": ppl2,
            "ppl_increase_pct": (ppl2 / ppl0 - 1) * 100,
            "teacher_forced_top1": same_next / count,
            "layer_error_max_rel": max((r["maxRel"] for r in layer_error), default=0.0),
            "saturations": sum(l2.saturations.values()),
        },
        "scheme": scheme(l2.spec),
        "layerError": layer_error,
        "saturationsByKind": l2.saturations,
        "samples": rows,
        "seconds": {"l0": l0_seconds, "l2": time.monotonic() - started},
    }
