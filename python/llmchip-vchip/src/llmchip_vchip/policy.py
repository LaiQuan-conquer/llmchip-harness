"""Acceptance policy of a chip (`policy` in chip.json or policy.json beside it).

The harness sets no thresholds of its own: a toy model and a 0.5B model have different
quality baselines. It only applies the chip's rules and says pass, fail or n/a (the metric
was not measured). Rule names: `<metric>_min`, `<metric>_max`, or `<metric>` for an exact
value (e.g. `"bit_exact": true`), under `quality` or `speed`.

    {"max_tokens": 64, "reference": "<chip id>", "golden": "<chip id>",
     "quality": {"next_token_agreement_min": 0.9, "perplexity_max": 4.0, "bit_exact": true},
     "speed": {"tok_s_min": 1000}}

One rule always applies: a run in which any step ended with an error (in a generation, while replaying
a comparison chip's sequence, or on a perplexity passage) does not pass.
"""

from __future__ import annotations


def _rule(name: str, limit, metrics: dict, missing: dict) -> dict:
    if name.endswith("_min"):
        metric, op = name[:-4], ">="
    elif name.endswith("_max"):
        metric, op = name[:-4], "<="
    else:
        metric, op = name, "=="
    value = metrics.get(metric)
    row = {"rule": name, "metric": metric, "op": op, "limit": limit, "value": value}
    if value is None:
        return {**row, "verdict": "n/a", "reason": missing.get(metric, "not measured in this run")}
    ok = value >= limit if op == ">=" else value <= limit if op == "<=" else value == limit
    return {**row, "verdict": "pass" if ok else "fail"}


def evaluate(policy: dict, quality: dict, speed: dict, missing: dict) -> dict:
    # Generations, replays of a comparison chip's sequences, and perplexity passages.
    errors = quality["errors"] + quality.get("comparison_errors", 0) + quality.get("perplexity_errors", 0)
    rules = [
        {
            "rule": "errors_max",
            "metric": "errors (generation, comparison, perplexity)",
            "op": "<=",
            "limit": 0,
            "value": errors,
            "verdict": "pass" if errors == 0 else "fail",
            "implicit": True,
        }
    ]
    for section, metrics in (("quality", quality), ("speed", speed)):
        for name, limit in policy.get(section, {}).items():
            rules.append({**_rule(name, limit, metrics, missing), "section": section})
    verdicts = {r["verdict"] for r in rules}
    if not policy.get("quality") and not policy.get("speed"):
        overall = "fail" if "fail" in verdicts else "no_policy"
    else:
        overall = "fail" if "fail" in verdicts else "incomplete" if "n/a" in verdicts else "pass"
    return {"verdict": overall, "rules": rules}
