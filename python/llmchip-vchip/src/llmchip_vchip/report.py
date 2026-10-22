"""report.md: the report in words, for a reader who will not open report.json."""

from __future__ import annotations


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


VERDICT_WORDS = {
    "pass": "PASS — every rule of the chip's policy holds",
    "fail": "FAIL — at least one rule fails",
    "incomplete": "INCOMPLETE — no rule fails, but some could not be measured",
    "no_policy": "NO POLICY — the chip declares no thresholds; the numbers below are not judged",
}


def markdown(r: dict) -> str:
    chip, q, sp = r["chip"], r["quality"], r["speed"]
    lines = [
        f"# Virtual chip report: {chip['id']}",
        "",
        f"Run `{r['run_id']}` — backend `{chip['backend']}`, host `{chip['host']}`, "
        f"clock {_fmt(chip['clock_mhz'])} MHz, model family `{chip['model'].get('family')}` "
        f"(context {chip['model'].get('context')}, vocabulary {chip['model'].get('vocab')}).",
        f"{r['prompt_set']['prompts']} prompts from `{r['prompt_set']['source']}`, "
        f"at most {r['max_tokens']} tokens each, "
        f"{r['decoding']}. All numbers are from simulation, not silicon.",
        "",
        f"**Verdict: {VERDICT_WORDS[r['verdict']['verdict']]}.**",
        "",
        "| Rule | Value | Limit | Verdict |",
        "|---|---|---|---|",
    ]
    for rule in r["verdict"]["rules"]:
        note = f" ({rule['reason']})" if rule.get("reason") else ""
        limit = f"{rule['op']} {_fmt(rule['limit'])}"
        lines.append(f"| `{rule['rule']}` | {_fmt(rule['value'])} | {limit} | {rule['verdict']}{note} |")
    lines += ["", "## Quality", "", "| Metric | Value |", "|---|---|"]
    for key in (
        "prompts",
        "errors",
        "length_mean",
        "repetition_rate",
        "expected_match",
        "next_token_agreement",
        "sequence_match",
        "bit_exact",
        "perplexity",
        "reference_perplexity",
    ):
        if key in q:
            lines.append(f"| {key} | {_fmt(q[key])} |")
    lines.append(f"| stop reasons | {', '.join(f'{k}: {v}' for k, v in q['stop_reasons'].items())} |")
    lines += ["", "## Speed", "", "| Metric | Value |", "|---|---|"]
    for key, v in sp.items():
        lines.append(f"| {key} | {_fmt(v)} |")
    m = r.get("measurement") or {}
    if m:
        trusted = "trusted" if m["trusted"] else "NOT trusted"
        lines += [
            "",
            f"Measurement: {trusted}. Cycles counted by {', '.join(m['cycles_source'])}, host cycles by "
            f"{', '.join(m['host_cycles_source'])}" + (f" ({m['testbench']})." if m.get("testbench") else "."),
        ]
        if not m["trusted"] and r["missing"].get("measurement"):
            lines.append(f"Why not: {r['missing']['measurement']}.")
    if r["comparisons"]:
        lines += ["", "## Comparisons", ""]
        for role, c in r["comparisons"].items():
            if "error" in c:
                lines.append(f"- {role} `{c['chip']}`: not compared — {c['error']}")
            else:
                text = (
                    f"- {role} `{c['chip']}`: next-token agreement {_fmt(c['next_token_agreement'])} "
                    f"over {c['agreement_positions']} positions; identical generations {_fmt(c['sequence_match'])}"
                )
                if "bit_exact" in c:
                    text += f"; bit-exact {c['bit_exact']} (compared {c['compared']})"
                lines.append(text)
    if r["missing"]:
        lines += ["", "## Not measured", ""]
        lines += [f"- {k}: {v}" for k, v in r["missing"].items()]
    lines += ["", "## Samples", ""]
    for s in r["samples"][:5]:
        if s["error"]:
            lines.append(f"- `{s['id']}` {s['prompt']!r} → error {s['error']['code']}: {s['error']['message']}")
        else:
            lines.append(f"- `{s['id']}` {s['prompt']!r} → {s['text']!r} ({s['stop_reason']})")
    errors = [s for s in r["samples"][5:] if s["error"]]
    for s in errors:
        lines.append(f"- `{s['id']}` {s['prompt']!r} → error {s['error']['code']}: {s['error']['message']}")
    return "\n".join(lines) + "\n"
