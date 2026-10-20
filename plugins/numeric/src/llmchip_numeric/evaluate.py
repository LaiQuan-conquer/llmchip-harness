"""Accuracy of L2 against L0 on an evaluation set (requirements FR-QNT-04, G1)."""

from __future__ import annotations

import math

import numpy as np
from llmchip_model import Model, Tokenizer

from .l2 import L2Model


def evaluate(l0: Model, l2: L2Model, tokenizer: Tokenizer, evalset: dict, progress=None) -> dict:
    steps = min(evalset["steps"], l2.context)
    rows, matches, positions, divergence = [], 0, 0, []
    for i, prompt in enumerate(evalset["prompts"]):
        ids = tokenizer.encode(prompt)
        a, b = l0.generate(ids, steps), l2.generate(ids, steps)
        generated = max(len(a), len(b)) - len(ids)
        same = sum(1 for x, y in zip(a[len(ids) :], b[len(ids) :], strict=False) if x == y)
        first = next((k for k, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
        matches, positions = matches + same, positions + max(generated, 0)
        divergence.append(max(first - len(ids), 0))
        rows.append(
            {
                "prompt": prompt,
                "l0": tokenizer.decode(a),
                "l2": tokenizer.decode(b),
                "agreement": same / generated if generated else 1.0,
            }
        )
        if progress:
            progress((i + 1) / (len(evalset["prompts"]) + 1))
    # Teacher forcing on the passages: perplexity, next-token agreement, and the residual
    # stream of every layer (L2 read as fixed point) against L0.
    nll0 = nll2 = 0.0
    count = same_next = 0
    layers = l2.layers
    worst = {"attn_residual": [0.0] * layers, "ffn_residual": [0.0] * layers}
    peak = {"attn_residual": [0.0] * layers, "ffn_residual": [0.0] * layers}
    fx = 2.0 ** -l2.f["x"]
    for passage in evalset["passages"]:
        ids = tokenizer.encode(passage)[: l2.context]
        l0.reset()
        l2.reset()
        for pos in range(len(ids) - 1):
            t0: dict = {}
            t2: dict = {}
            a = l0.forward(ids[pos], pos, trace=t0).astype(np.float64)
            b = l2.forward(ids[pos], pos, trace=t2).astype(np.float64) * 2.0**-l2.fl
            for logits, which in ((a, 0), (b, 2)):
                mx = logits.max()
                nll = mx + np.log(np.exp(logits - mx).sum()) - logits[ids[pos + 1]]
                nll0, nll2 = (nll0 + nll, nll2) if which == 0 else (nll0, nll2 + nll)
            same_next += int(np.argmax(a) == np.argmax(b))
            count += 1
            for point in worst:
                for i in range(layers):
                    ref = t0[point][i]
                    worst[point][i] = max(worst[point][i], float(np.abs(t2[point][i] * fx - ref).max()))
                    peak[point][i] = max(peak[point][i], float(np.abs(ref).max()))
    layer_error = [
        {"layer": i, "point": point, "maxAbs": worst[point][i], "maxRel": worst[point][i] / max(peak[point][i], 1e-30)}
        for i in range(layers)
        for point in ("attn_residual", "ffn_residual")
    ]
    ppl0 = math.exp(nll0 / count) if count else float("nan")
    ppl2 = math.exp(nll2 / count) if count else float("nan")
    return {
        "metrics": {
            "prompts_evaluated": len(rows),
            "tokens_per_prompt": steps,
            "passage_tokens": count,
            "token_agreement": matches / positions if positions else 1.0,
            "first_divergence_mean": sum(divergence) / len(divergence) if divergence else 0.0,
            "l0_perplexity": ppl0,
            "l2_perplexity": ppl2,
            "ppl_increase_pct": (ppl2 / ppl0 - 1) * 100 if count else float("nan"),
            "teacher_forced_top1": same_next / count if count else float("nan"),
            "layer_error_max_rel": max((r["maxRel"] for r in layer_error), default=0.0),
            "saturations": sum(l2.saturations.values()),
        },
        "scheme": scheme(l2.spec),
        "layerError": layer_error,
        "saturationsByKind": l2.saturations,
        "samples": rows,
    }


def scheme(spec: dict) -> dict:
    """The numeric choices a report was made with, for comparing schemes side by side."""
    w = spec["weights"]
    return {
        "weightFormat": w["format"],
        "weightBits": w["storageBits"],
        "groupSize": w["groupSize"],
        "accumulatorBits": spec["accumulator"]["bits"],
        "accumulatorCanOverflow": spec["accumulator"].get("canOverflow", False),
        "rounding": spec.get("rounding", {}).get("mode", "half-up"),
        "overflow": spec.get("overflow", {}).get("mode", "saturate"),
        "perLayer": spec["activations"].get("granularity") == "layer",
        "residualBits": spec["activations"].get("bits", {}).get("x", 16),
    }
