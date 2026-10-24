"""Model quality by open benchmarks (requirements FR-QNT-04/08): run a set on L0 or L2, compare.

`run` answers every question of the chosen set (the quick set or all questions) with one
model and judges each answer the way its benchmark does:

- MMLU, CMMLU, C-Eval (multiple choice): the option with the highest log-likelihood is the
  answer (`acc`);
- GSM8K (generation): the number in the answer's last `\\boxed{}` equals the reference
  (`exact_match`);
- IFEval (generation): every instruction of the prompt is followed, strictly
  (`prompt_level_strict_acc`);
- HumanEval+, MBPP+ (code): the base and the plus tests pass in the sandbox (`benchcode`).

The lm-eval settings are lm-eval's task defaults with the chat template and few-shot examples
as a multi-turn conversation, except GSM8K: lm-eval's 5-shot default stops the answers of an
instruction model at 256 tokens (L0 scored 33.5% on the first 200 questions, its 8-shot and
zero-shot chain-of-thought variants 36.5% and 42.0%); `GSM8K_QWEN` asks zero-shot with the
instruction Qwen uses for its own evaluations and reads the boxed answer (44.5%; Qwen reports
49.6% on the whole set). The report records every setting it used.

`compare` pairs the two reports question by question. For each benchmark it gives the drop
of L2 against L0 in percentage points and its 95% paired-bootstrap interval; the checks
`spec.accuracy.quick` and `spec.accuracy` judge these against the allowed drops (owner
decision 2026-10-24). This module never judges (AP-7).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from .benchdata import CHOICE, CODE, GENERATION, Data

REPORT_FORMAT = "llmchip-benchmark-report/1"
COMPARISON_FORMAT = "llmchip-benchmark-comparison/1"
BOOTSTRAP = 10_000
BOOTSTRAP_SEED = 20261024
# benchmark -> (lm-eval metric, filter) of a correct answer
METRIC = {
    "mmlu": ("acc", "none"),
    "cmmlu": ("acc", "none"),
    "ceval": ("acc", "none"),
    "gsm8k": ("exact_match", "boxed"),
    "ifeval": ("prompt_level_strict_acc", "none"),
}


GSM8K_QWEN = {
    "task": "gsm8k_qwen",
    "dataset_path": "openai/gsm8k",
    "dataset_name": "main",
    "output_type": "generate_until",
    "test_split": "test",
    "num_fewshot": 0,
    "doc_to_text": "{{question}}\nPlease reason step by step, and put your final answer within \\boxed{}.",
    "doc_to_target": "{{answer.split('#### ')[-1]}}",
    "generation_kwargs": {"until": ["<|im_end|>"], "do_sample": False, "temperature": 0.0, "max_gen_toks": 1024},
    "filter_list": [
        {
            "name": "boxed",
            "filter": [
                {
                    "function": "regex",
                    "regex_pattern": "\\\\boxed\\{(?:\\\\text\\{)?\\$?(-?[0-9][0-9,]*(?:\\.[0-9]+)?)",
                    "group_select": -1,
                },
                {"function": "take_first"},
            ],
        }
    ],
    "metric_list": [
        {
            "metric": "exact_match",
            "aggregation": "mean",
            "higher_is_better": True,
            "ignore_case": True,
            "ignore_punctuation": False,
            "regexes_to_ignore": [",", "\\$", "\\.$", "\\.0+$"],
        }
    ],
    "metadata": {"version": 1.0},
}


def task_specs(data: Data, bench: str) -> tuple[list, dict[str, list[int]]]:
    """The lm-eval tasks of a benchmark (names, or a config) and their quick-set questions."""
    if bench == "gsm8k":
        return [GSM8K_QWEN], {"gsm8k_qwen": data.index["quick"]["gsm8k"]}
    names = data.tasks(bench)
    return names, {t: data.index["quick"][t] for t in names}


def _text(value) -> str:
    if isinstance(value, list | tuple):
        return _text(value[0]) if value else ""
    return str(value)


def _lm_eval_items(results: dict, bench: str, tasks: list[str]) -> list[dict]:
    metric, filt = METRIC[bench]
    items = []
    for task in tasks:
        for s in results["samples"].get(task, []):
            if s.get("filter", "none") != filt:
                continue
            item = {
                "id": f"{task}/{s['doc_id']}",
                "task": task,
                "correct": bool(s[metric]),
                "target": _text(s.get("target")),
                "prompt": _text(s["arguments"][0]) if s.get("arguments") else "",
            }
            if bench in CHOICE:
                scores = [float(r[0][0]) for r in s["resps"]]
                item["answer"] = int(np.argmax(scores))
                item["scores"] = scores
            else:
                item["answer"] = _text(s["resps"][0])
                item["extracted"] = _text(s.get("filtered_resps"))
            items.append(item)
    items.sort(key=lambda it: (it["task"], int(it["id"].rsplit("/", 1)[1])))
    return items


def run_lm_eval(lm, data: Data, bench: str, quick: bool) -> tuple[list[dict], dict]:
    from lm_eval import simple_evaluate
    from lm_eval.tasks import TaskManager

    specs, quick_samples = task_specs(data, bench)
    tasks = list(quick_samples)
    samples = quick_samples if quick else None
    with data.route():
        results = simple_evaluate(
            model=lm,
            tasks=specs,
            samples=samples,
            apply_chat_template=True,
            fewshot_as_multiturn=True,
            log_samples=True,
            bootstrap_iters=0,
            task_manager=TaskManager(),
        )
    settings = {
        t: {
            "nShot": results["n-shot"].get(t),
            "version": results["versions"].get(t),
            "generationKwargs": results["configs"][t].get("generation_kwargs"),
        }
        for t in tasks
    }
    return _lm_eval_items(results, bench, tasks), settings


def run_code(lm, data: Data, bench: str, quick: bool, work: Path) -> tuple[list[dict], dict]:
    from . import benchcode

    problems = data.code_problems(bench)
    if quick:
        wanted = set(data.index["quick"][bench])
        problems = [p for p in problems if p["task_id"] in wanted]
    answers = benchcode.answers(lm, problems)
    scored = benchcode.score(bench, data.code_file(bench), answers, work / f"sandbox-{bench}")
    items = [
        {
            "id": a["task_id"],
            "task": bench,
            "correct": scored[a["task_id"]]["plus"],
            "base": scored[a["task_id"]]["base"],
            "answer": a["solution"],
            "raw": a["raw"],
            "prompt": p["prompt"],
        }
        for a, p in zip(answers, problems, strict=True)
    ]
    settings = {
        "prompt": "EvalPlus instruction prompt (make_raw_chat_prompt)",
        "maxNewTokens": benchcode.MAX_NEW_TOKENS,
        "sandbox": benchcode.IMAGE,
        "correct": "base and plus tests pass",
    }
    return items, settings


def run(
    lm,
    data: Data,
    benchmarks: list[str],
    quick: bool,
    work: Path,
    done: dict | None = None,
    after_each=lambda report: None,
    progress=lambda message: None,
) -> dict:
    """Runs the benchmarks one after the other; `done` holds the ones finished by an earlier
    attempt (from its checkpoint), `after_each` is called with the report after each one."""
    report = {"benchmarks": dict(done or {})}
    for bench in benchmarks:
        if bench in report["benchmarks"]:
            progress(f"{bench}: finished by an earlier attempt")
            continue
        progress(f"{bench}: running")
        started = time.monotonic()
        if bench in CODE:
            items, settings = run_code(lm, data, bench, quick, work)
        elif bench in CHOICE or bench in GENERATION:
            items, settings = run_lm_eval(lm, data, bench, quick)
        else:
            raise ValueError(f"unknown benchmark {bench}")
        report["benchmarks"][bench] = {
            "questions": len(items),
            "correct": sum(it["correct"] for it in items),
            "score": sum(it["correct"] for it in items) / len(items) if items else None,
            "seconds": time.monotonic() - started,
            "settings": settings,
            "items": items,
        }
        after_each(report)
    return report


def paired_bootstrap(c0: np.ndarray, c2: np.ndarray, seed: int = BOOTSTRAP_SEED) -> tuple[float, float, float]:
    """(drop, low, high) of L0 minus L2 in percentage points, with the 95% interval of the
    drop under resampling of the questions (pairs kept together)."""
    n = len(c0)
    diff = c0.astype(np.float64) - c2.astype(np.float64)
    rng = np.random.default_rng(seed)
    means = np.empty(BOOTSTRAP)
    for start in range(0, BOOTSTRAP, 1000):
        idx = rng.integers(0, n, size=(min(1000, BOOTSTRAP - start), n))
        means[start : start + len(idx)] = diff[idx].mean(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return 100 * float(diff.mean()), 100 * float(low), 100 * float(high)


def compare(l0: dict, l2: dict, reference: dict | None = None) -> tuple[dict, dict]:
    """(comparison report, metrics) of two benchmark reports on the same questions."""
    for key in ("set", "maxLength", "data", "versions"):
        if l0.get(key) != l2.get(key):
            raise ValueError(f"the reports differ in {key}: {l0.get(key)} and {l2.get(key)}")
    if l0.get("layer") != "L0" or l2.get("layer") != "L2":
        raise ValueError("compare needs an L0 report and an L2 report")
    source = lambda r: {k: r.get("model", {}).get(k) for k in ("model", "bundle")}  # noqa: E731
    if source(l0) != source(l2):
        raise ValueError(f"the reports answered with different models: {source(l0)} and {source(l2)}")
    metrics: dict = {"set": l0["set"], "benchmarks": 0}
    out = {"format": COMPARISON_FORMAT, "set": l0["set"], "benchmarks": {}, "reference": reference or {}}
    for bench, a in l0["benchmarks"].items():
        b = l2["benchmarks"].get(bench)
        if b is None:
            continue
        ids0 = [it["id"] for it in a["items"]]
        if ids0 != [it["id"] for it in b["items"]]:
            raise ValueError(f"{bench}: the reports answered different questions")
        if a.get("settings") != b.get("settings"):
            raise ValueError(f"{bench}: the reports were evaluated with different settings")
        if not ids0:
            raise ValueError(f"{bench}: no questions")
        c0 = np.array([it["correct"] for it in a["items"]], bool)
        c2 = np.array([it["correct"] for it in b["items"]], bool)
        drop, low, high = paired_bootstrap(c0, c2)
        only0 = [i for i, (x, y) in enumerate(zip(c0, c2, strict=True)) if x and not y]
        only2 = [i for i, (x, y) in enumerate(zip(c0, c2, strict=True)) if y and not x]
        out["benchmarks"][bench] = {
            "questions": len(ids0),
            "l0": 100 * float(c0.mean()),
            "l2": 100 * float(c2.mean()),
            "drop": drop,
            "dropLow": low,
            "dropHigh": high,
            "l0OnlyCorrect": [ids0[i] for i in only0],
            "l2OnlyCorrect": [ids0[i] for i in only2],
        }
        metrics |= {
            f"n.{bench}": len(ids0),
            f"l0.{bench}": 100 * float(c0.mean()),
            f"l2.{bench}": 100 * float(c2.mean()),
            f"drop.{bench}": drop,
            f"drop_low.{bench}": low,
            f"drop_high.{bench}": high,
            f"l0_only.{bench}": len(only0),
            f"l2_only.{bench}": len(only2),
        }
        metrics["benchmarks"] += 1
    return out, metrics


def load(path: str | Path) -> dict:
    report = json.loads(Path(path).read_text())
    if report.get("format") != REPORT_FORMAT:
        raise ValueError("not a benchmark report")
    return report
