"""The RTL bridge: a process that runs an RTL bundle under the pin-level testbench and serves it
as a virtual chip (the `process` protocol of adapters/process.py).

    python -m llmchip_vchip.bridges.rtl_bridge CHIP_DIR BUILD_DIR

The testbench (tb/vchip_tb.cpp) drives only the chip's pins and counts the cycles itself, so
every step's `cycles` (busy pin high) and `host_cycles` (the rest of the token's time at the
host: START, the STATUS polls) carry `source: testbench`. The bridge passes them on unchanged.
Token and position must fit the 16-bit START fields; whether they are legal for the model is
the chip's decision (its error flag), not the bridge's.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

import numpy as np

from ..interface import BaseChip, ChipError, StepResult
from ..manifest import load
from ..serve import serve
from .verilator import BINARY, container

POINT_NAMES = {1: "attn_residual", 2: "ffn_residual", 3: "logits"}
# The checks of op_host_check; conform's T-17 requires exactly these, all true.
HOST_CHECKS = (
    "start_while_busy_ignored",
    "status_polled_until_done",
    "busy_pin_counted",
    "read_repeatable",
    "read_ends_with_token",
    "read_unchanged_by_ignored_start",
    "illegal_token_flagged",
    "illegal_position_flagged",
    "usable_after_error_without_reset",
)
TESTBENCH = "llmchip-vchip pin-level testbench (tb/vchip_tb.cpp) on Verilator"


class RtlSim(BaseChip):
    def __init__(self, chip_dir: Path, build_dir: Path) -> None:
        m = load(chip_dir)
        super().__init__(m.meta())
        self.design = json.loads((build_dir / "bundle" / "design.json").read_text())
        self.trace_map = self.design.get("traceMap", [])
        self.obs_words = int(self.design["params"].get("OBS_WORDS", 0))
        caps = self.meta.capabilities
        self.want_logits, self.want_traces = "logits" in caps, "traces" in caps
        self.read_in_host = int(self.meta.host.get("read_words", 0)) > 0
        self.scale = 1.0
        if self.want_logits:
            spec = json.loads(m.artifact("spec").read_text())
            self.scale = 2.0 ** -int(spec["logits"]["frac"])
        image = m.data["simulator"]["image"]
        self.name = f"llmchip-vchip-{os.getpid()}-{secrets.token_hex(3)}"
        self.proc = subprocess.Popen(
            container(image, [f"{build_dir}:/work:ro"], f"/work/{BINARY}", name=self.name),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        signal.signal(signal.SIGTERM, self._terminate)

    # -- testbench ---------------------------------------------------------------------

    def _terminate(self, *_args) -> None:
        self._remove()
        sys.exit(143)

    def _remove(self) -> None:
        # -t 0: the testbench is PID 1 of its container and ignores SIGTERM; do not wait for it.
        subprocess.run(["podman", "rm", "-f", "-t", "0", self.name], capture_output=True, timeout=60)

    def _cmd(self, line: str, expect: str) -> list[str]:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        answer = self.proc.stdout.readline().split()
        if not answer or answer[0] != expect:
            raise ChipError("crash", f"the testbench answered {' '.join(answer)!r} to {line!r}")
        return answer[1:]

    def _step(self, token: int, pos: int, inject: int = 0) -> dict:
        out, err, busy, total, polls, injected, timeout, stale = self._cmd(f"STEP {token} {pos} {inject}", "STEP")
        return {
            "stale": int(stale),
            "token": int(out),
            "error": int(err),
            "busy": int(busy),
            "total": int(total),
            "polls": int(polls),
            "injected": int(injected),
            "timeout": int(timeout),
        }

    def _read(self) -> tuple[list[int], int]:
        cycles, *words = self._cmd(f"READ 0 {self.obs_words}", "WORDS")
        return [int(w) for w in words], int(cycles)

    # -- VirtualChip ---------------------------------------------------------------------

    def hello_info(self) -> dict:
        return {"measurement": "testbench", "testbench": TESTBENCH}

    def reset(self) -> None:
        _cycles, timeout = self._cmd("RESET", "OK")
        if int(timeout):
            raise ChipError("timeout", "the chip did not become idle after reset (testbench limit)")

    def step(self, token: int, pos: int) -> StepResult:
        if not 0 <= token < 1 << 16 or not 0 <= pos < 1 << 16:
            raise ChipError("invalid_input", f"token {token} or position {pos} does not fit the 16-bit START fields")
        s = self._step(token, pos)
        if s["timeout"]:
            raise ChipError("timeout", f"the chip did not finish the step at position {pos} (testbench limit)")
        if s["error"]:
            raise ChipError("invalid_input", f"the chip set its error flag for token {token} at position {pos}")
        if s["stale"]:
            raise ChipError("invalid_output", f"STATUS and the busy pin disagree at position {pos} (stale done?)")
        r = StepResult(
            s["token"],
            cycles=s["busy"],
            host_cycles=s["total"] - s["busy"],
            cycles_source="testbench",
            host_cycles_source="testbench",
        )
        if self.want_logits or self.want_traces:
            words, read_cycles = self._read()
            if self.read_in_host:
                r.host_cycles = (r.host_cycles or 0) + read_cycles
            else:
                r.read_cycles = read_cycles
            traces: dict[str, list] = {}
            for t in self.trace_map:
                values = words[t["addr"] : t["addr"] + t["n"]]
                name = POINT_NAMES.get(t["point"])
                if name == "logits":
                    if self.want_logits:
                        r.logits = (np.asarray(values, dtype=np.float64) * self.scale).tolist()
                elif name and self.want_traces:
                    traces.setdefault(name, []).append(values)
            r.traces = traces or None
        return r

    def op_host_check(self) -> dict:
        """Pin-level checks of the host interface (HOST_CHECKS): a START sent right after the
        first, while the busy pin is high, is ignored (same token, cycles and observation words);
        STATUS is polled until done; READ while idle is repeatable and ends with the step's token;
        an illegal token and an illegal position set the error flag, and the chip then runs a
        legal step without a reset."""
        token = 2
        self.reset()
        plain = self._step(token, 0)
        plain_words, _ = self._read()
        self.reset()
        disturbed = self._step(token, 0, inject=1)
        first, _ = self._read()
        second, _ = self._read()
        illegal_token = self._step(self.meta.vocab, 1)
        illegal_pos = self._step(token, self.meta.context)
        after = self._step(token, 1)  # no reset: position 1 after the legal step at 0
        self.reset()
        self._step(token, 0)
        expected = self._step(token, 1)
        checks = {
            "start_while_busy_ignored": bool(
                disturbed["injected"] and disturbed["token"] == plain["token"] and disturbed["busy"] == plain["busy"]
            ),
            "status_polled_until_done": plain["polls"] >= 2
            and not (plain["error"] or plain["timeout"] or plain["stale"]),
            "busy_pin_counted": plain["busy"] > 0,
            "read_repeatable": first == second and len(first) == self.obs_words,
            "read_ends_with_token": bool(first) and first[-1] == disturbed["token"],
            "read_unchanged_by_ignored_start": first == plain_words,
            "illegal_token_flagged": bool(illegal_token["error"]),
            "illegal_position_flagged": bool(illegal_pos["error"]),
            "usable_after_error_without_reset": after["token"] == expected["token"]
            and not (after["error"] or after["stale"]),
        }
        assert tuple(checks) == HOST_CHECKS
        details = {
            "plain": plain,
            "disturbed": disturbed,
            "illegal_token": illegal_token,
            "illegal_position": illegal_pos,
        }
        return {"checks": checks, "details": details}

    def close(self) -> None:
        try:
            if self.proc.poll() is None and self.proc.stdin:
                self.proc.stdin.write("QUIT\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._remove()


def main() -> None:
    chip = RtlSim(Path(sys.argv[1]), Path(sys.argv[2]))
    try:
        serve(chip)
    finally:
        chip._remove()


if __name__ == "__main__":
    main()
