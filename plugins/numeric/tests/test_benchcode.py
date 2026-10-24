"""The code sandbox of HumanEval+ / MBPP+ (requirements FR-QNT-04; AGENTS.md "Adding a plugin" 6).

- EvalPlus judges answers inside the container: the reference solution passes, a wrong one
  fails, and an answer that tries to write a file on the host fails without touching the host.
- Cancelling a run (SIGTERM to the plugin) removes the container at once, within the kernel's
  grace period, as the plugin's handler `_stop` does.

Needs Podman, the sandbox image and the EvalPlus problem file of the tools directory (../tools,
or LLMCHIP_TOOLS); otherwise skipped, visibly.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from llmchip_numeric import benchcode

TOOLS = Path(os.environ.get("LLMCHIP_TOOLS", Path(__file__).parents[4] / "tools"))
HUMANEVAL = TOOLS / "datasets" / "evalplus" / "HumanEvalPlus.jsonl"

pytestmark = pytest.mark.skipif(
    not benchcode.image_present() or not HUMANEVAL.exists(),
    reason=f"needs the sandbox image {benchcode.IMAGE} and {HUMANEVAL}",
)


def problem(task_id: str) -> dict:
    for line in HUMANEVAL.read_text().splitlines():
        p = json.loads(line)
        if p["task_id"] == task_id:
            return p
    raise KeyError(task_id)


def test_the_sandbox_judges_answers_and_keeps_them_off_the_host(tmp_path):
    p0, p1, p2 = problem("HumanEval/0"), problem("HumanEval/2"), problem("HumanEval/4")
    marker = tmp_path / "written-by-the-model"
    answers = [
        {"task_id": p0["task_id"], "solution": p0["prompt"] + p0["canonical_solution"]},
        {"task_id": p1["task_id"], "solution": p1["prompt"] + "    return 0.0\n"},
        {
            "task_id": p2["task_id"],
            "solution": f"open({str(marker)!r}, 'w').write('x')\n" + p2["prompt"] + p2["canonical_solution"],
        },
    ]
    result = benchcode.score("humaneval", HUMANEVAL, answers, tmp_path / "work")
    assert result[p0["task_id"]] == {"base": True, "plus": True}
    assert result[p1["task_id"]] == {"base": False, "plus": False}
    assert result[p2["task_id"]]["base"] is False
    assert not marker.exists()


CHILD = """
import json, signal, sys
from pathlib import Path
from llmchip_numeric import benchcode
from llmchip_numeric.__main__ import _stop
signal.signal(signal.SIGTERM, _stop)
answers = [{"task_id": "HumanEval/0", "solution": "import time\\ntime.sleep(600)\\n"}]
benchcode.score("humaneval", Path(sys.argv[1]), answers, Path(sys.argv[2]))
"""


def sandboxes() -> list[str]:
    out = subprocess.run(
        ["podman", "ps", "-a", "--filter", "name=llmchip-sandbox-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


def test_cancelling_removes_the_sandbox_within_the_grace_period(tmp_path):
    before = set(sandboxes())
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(HUMANEVAL), str(tmp_path / "work")])
    try:
        for _ in range(600):
            if set(sandboxes()) - before:
                break
            time.sleep(0.1)
        assert set(sandboxes()) - before, "the sandbox did not start"
        child.send_signal(signal.SIGTERM)
        started = time.monotonic()
        child.wait(timeout=10)
        assert time.monotonic() - started < 2.0, "the plugin did not end within the cancel grace period"
        assert child.returncode == 128 + signal.SIGTERM
        assert not set(sandboxes()) - before
    finally:
        child.kill()
