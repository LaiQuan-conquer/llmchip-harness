"""Accuracy of L2 against L0 on an evaluation set (requirements FR-QNT-04, G1)."""

from __future__ import annotations

import math

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
    nll0 = nll2 = 0.0
    count = 0
    for passage in evalset["passages"]:
        ids = tokenizer.encode(passage)[: l2.context]
        t0, n = l0.nll(ids)
        t2, _ = l2.nll(ids)
        nll0, nll2, count = nll0 + t0, nll2 + t2, count + n
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
            "saturations": sum(l2.saturations.values()),
        },
        "saturationsByKind": l2.saturations,
        "samples": rows,
    }
