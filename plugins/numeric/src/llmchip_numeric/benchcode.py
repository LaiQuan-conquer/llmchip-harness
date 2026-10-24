"""HumanEval+ and MBPP+ (requirements FR-QNT-04): code answers, judged by unit tests in a sandbox.

Generation follows EvalPlus's instruction-model path: its chat prompt (`make_raw_chat_prompt`
with the prefixes below), greedy decoding up to 768 new tokens, its stop strings plus the end
of the code block, tabs as four spaces, then its sanitizer. The model is L0 or L2 through
`benchlm.generate_texts`, so both answer through the same code.

The answers run only inside a container: `evalplus.evaluate` (base and plus tests) under
`podman run --network=none --read-only`, with limited memory and processes, from this
workbench's Python environment mounted read-only. Model-generated code never runs on the
host. EvalPlus scores every problem of its dataset file, so it gets a file with only the
selected problems. A container still running when the run is cancelled is removed by
`remove_sandboxes` (the plugin's SIGTERM handler).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

IMAGE = "docker.io/library/python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
INSTRUCTION = (
    "Please provide a self-contained Python script that solves the following problem in a markdown code block:"
)
RESPONSE = (
    "Below is a Python script with a self-contained function that solves the problem and passes corresponding tests:"
)
MAX_NEW_TOKENS = 768
OVERRIDE = {"humaneval": "HUMANEVAL_OVERRIDE_PATH", "mbpp": "MBPP_OVERRIDE_PATH"}

_SANDBOXES: set[str] = set()


def remove_sandboxes(grace: float = 60.0) -> None:
    """Kills and removes the running sandboxes (about 0.4 s each; Podman 3.4 has no `rm -t`).
    `grace` bounds each call: the kernel cancels a run within `scheduler.cancelGraceMs` (2 s),
    so the SIGTERM handler passes 1 s instead of the default 60 s."""
    for name in list(_SANDBOXES):
        for command in (["podman", "kill", name], ["podman", "rm", "-f", name]):
            with contextlib.suppress(subprocess.TimeoutExpired, FileNotFoundError):
                subprocess.run(command, capture_output=True, timeout=grace)


def image_present() -> bool:
    try:
        return subprocess.run(["podman", "image", "exists", IMAGE], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def answers(lm, problems: list[dict]) -> list[dict]:
    """Code answers of the model to the problems: {task_id, raw, solution}."""
    from evalplus.provider.utility import EOS, make_raw_chat_prompt
    from evalplus.sanitize import sanitize

    from .benchlm import generate_texts

    prompts = [make_raw_chat_prompt(p["prompt"].strip() + "\n", INSTRUCTION, RESPONSE, lm.tokenizer) for p in problems]
    raw = generate_texts(lm, prompts, [*EOS, "\n```\n"], MAX_NEW_TOKENS)
    out = []
    for p, text in zip(problems, raw, strict=True):
        text = text.replace("\t", "    ")
        out.append({"task_id": p["task_id"], "raw": text, "solution": sanitize(text, entrypoint=p["entry_point"])})
    return out


def score(bench: str, problems_file: Path, answers_: list[dict], work: Path) -> dict[str, dict]:
    """Runs EvalPlus on the answers in the sandbox: {task_id: {base, plus}} (plus = base and plus tests)."""
    if not image_present():
        raise RuntimeError(f"the sandbox image {IMAGE} is not present (podman pull it)")
    work.mkdir(parents=True, exist_ok=True)
    wanted = {a["task_id"] for a in answers_}
    lines = [line for line in problems_file.read_text().splitlines() if line.strip()]
    subset = work / f"{bench}_problems.jsonl"
    subset.write_text("".join(line + "\n" for line in lines if json.loads(line)["task_id"] in wanted))
    samples = work / f"{bench}.jsonl"
    # EvalPlus asks on stdin whether to reuse an earlier result file: never leave one.
    samples.with_name(samples.stem + "_eval_results.json").unlink(missing_ok=True)
    samples.write_text(
        "".join(json.dumps({"task_id": a["task_id"], "solution": a["solution"]}) + "\n" for a in answers_)
    )
    prefix = Path(sys.prefix)
    python_home = Path(os.path.realpath(sys.executable)).parents[2]
    cache = work / "cache"
    cache.mkdir(exist_ok=True)
    name = f"llmchip-sandbox-{uuid.uuid4().hex[:12]}"
    cmd = [
        "podman", "run", "--rm", "--name", name, "--network=none", "--read-only", "--tmpfs", "/tmp",
        "--memory=4g", "--pids-limit=512",
        "-v", f"{prefix}:{prefix}:ro", "-v", f"{python_home}:{python_home}:ro",
        "-v", f"{work}:{work}", "-e", f"XDG_CACHE_HOME={cache}", "-e", "HOME=/tmp",
        "-e", f"{OVERRIDE[bench]}={subset}",
        IMAGE, str(prefix / "bin" / "python"), "-m", "evalplus.evaluate",
        "--dataset", bench, "--samples", str(samples), "--i_just_wanna_run",
    ]  # fmt: skip
    _SANDBOXES.add(name)
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    finally:
        _SANDBOXES.discard(name)
    if r.returncode != 0:
        raise RuntimeError(f"evalplus.evaluate failed in the sandbox ({r.returncode}): {r.stderr[-2000:]}")
    result = json.loads(samples.with_name(samples.stem + "_eval_results.json").read_text())
    out = {}
    for tid, rows in result["eval"].items():
        base = rows[0]["base_status"] == "pass"
        out[tid] = {"base": base, "plus": base and rows[0]["plus_status"] == "pass"}
    missing = wanted - set(out)
    if missing:
        raise RuntimeError(f"EvalPlus returned no result for {sorted(missing)}")
    return out
