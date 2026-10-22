"""`process`: a chip running in its own process, spoken to with JSON lines over stdin/stdout.

This is how a hardware model plugs in: an RTL or gate-level simulation, a cycle model, an
FPGA bridge or a container. One request per line, one response per line:

    {"op": "hello"}                       -> {"ok": true, "protocol": "llmchip-vchip-process/1"}
    {"op": "reset"}                       -> {"ok": true}
    {"op": "prefill", "tokens": [...]}    -> {"ok": true, "results": [<step>, ...]}
    {"op": "step", "token": t, "pos": p}  -> {"ok": true, <step>}
    {"op": "close"}                       -> (the process exits)
    <step> = "token": int, "cycles": int, optional "host_cycles", "logits", "traces"
    failure: {"ok": false, "error": {"code": <interface.ERROR_CODES>, "message": "..."}}

`command` in chip.json starts the process (`{chip_dir}` and `{python}` are substituted; it
runs in the chip directory). Every request waits at most `timeout_s` (default 60 s): a
chip that does not answer is killed and the step ends with `timeout`; a process that exits
or writes something that is not a response ends it with `crash`. `reset` starts a new
process if the previous one died, so one failed prompt does not stop a benchmark.
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time

from ..interface import ERROR_CODES, BaseChip, ChipError, StepResult
from ..manifest import Manifest

PROTOCOL = "llmchip-vchip-process/1"


class ProcessChip(BaseChip):
    def __init__(self, m: Manifest) -> None:
        super().__init__(m.meta())
        subst = {"chip_dir": str(m.dir.resolve()), "python": sys.executable}
        self.command = [part.format(**subst) for part in m.data["command"]]
        self.cwd = m.dir
        self.timeout = float(m.data.get("timeout_s", 60))
        self.proc: subprocess.Popen | None = None
        self.starts = 0
        self._buf = b""
        self._start()

    # -- process -------------------------------------------------------------------

    def _start(self) -> None:
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 (lives as long as the process)
        # A session of its own: a timeout kills the whole group, including a simulator
        # started by a shell or a container wrapper.
        self.proc = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            start_new_session=True,
        )
        assert self.proc.stdin and self.proc.stdout
        os.set_blocking(self.proc.stdin.fileno(), False)
        self.starts += 1
        self._buf = b""
        hello = self._call({"op": "hello"})
        if hello.get("protocol") != PROTOCOL:
            self._kill()
            raise ChipError("crash", f"the chip process speaks {hello.get('protocol')!r}, not {PROTOCOL}")

    def _stderr_tail(self) -> str:
        self._stderr.seek(0)
        tail = self._stderr.read()[-800:].decode("utf-8", "replace").strip()
        return f" (stderr: {tail})" if tail else ""

    def _release(self) -> None:
        """Closes the pipes and the stderr file of the (ended) process."""
        if self.proc:
            for f in (self.proc.stdin, self.proc.stdout):
                if f:
                    with contextlib.suppress(OSError):
                        f.close()
        self._stderr.close()

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        elif self.proc:
            # The leader has exited; its group may live on.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGKILL)

    def _crashed(self, what: str) -> ChipError:
        assert self.proc
        try:
            code = self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        tail = self._stderr_tail()
        self._kill()
        self._release()
        return ChipError("crash", f"{what} (exit code {code}){tail}")

    def _write(self, data: bytes, deadline: float) -> None:
        assert self.proc and self.proc.stdin
        fd = self.proc.stdin.fileno()
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_WRITE)
            while data:
                left = deadline - time.monotonic()
                if left <= 0 or not sel.select(left):
                    self._kill()
                    self._release()
                    raise ChipError("timeout", f"the request was not taken within {self.timeout:g} s; killed")
                try:
                    data = data[os.write(fd, data) :]
                except BlockingIOError:
                    continue
                except (BrokenPipeError, ConnectionResetError) as e:
                    raise self._crashed("the chip process closed its input") from e

    def _readline(self, deadline: float) -> bytes:
        assert self.proc and self.proc.stdout
        fd = self.proc.stdout.fileno()
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while b"\n" not in self._buf:
                left = deadline - time.monotonic()
                if left <= 0 or not sel.select(left):
                    self._kill()
                    self._release()
                    raise ChipError("timeout", f"no answer within {self.timeout:g} s; the process was killed")
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    raise self._crashed("the chip process ended its output")
                self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def _call(self, request: dict) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            raise ChipError("crash", "the chip process is not running (reset starts a new one)")
        deadline = time.monotonic() + self.timeout
        self._write(json.dumps(request).encode() + b"\n", deadline)
        line = self._readline(deadline)
        try:
            response = json.loads(line)
        except json.JSONDecodeError as e:
            self._kill()
            self._release()
            raise ChipError("crash", f"the chip process wrote {line[:120]!r}, not a JSON response") from e
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            self._kill()
            self._release()
            raise ChipError("crash", f"malformed response {line[:120]!r}")
        if not response["ok"]:
            err = response.get("error") or {}
            code = err.get("code") if err.get("code") in ERROR_CODES else "crash"
            raise ChipError(code, str(err.get("message", "the chip reported an error")))
        return response

    # -- VirtualChip --------------------------------------------------------------

    @staticmethod
    def _step(d: dict) -> StepResult:
        token = d.get("token")
        if isinstance(token, bool) or not isinstance(token, int):
            raise ChipError("invalid_output", f"the chip returned token {token!r}")
        return StepResult(
            token=token,
            cycles=d.get("cycles", 0),  # absent: unknown (0); speed metrics then say so
            host_cycles=d.get("host_cycles"),
            logits=d.get("logits"),
            traces=d.get("traces"),
        )

    def reset(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        self._call({"op": "reset"})

    def prefill(self, tokens: list[int]) -> list[StepResult]:
        results = self._call({"op": "prefill", "tokens": tokens}).get("results")
        if not isinstance(results, list):
            raise ChipError("invalid_output", "prefill returned no results list")
        return [self._step(r) for r in results]

    def step(self, token: int, pos: int) -> StepResult:
        return self._step(self._call({"op": "step", "token": token, "pos": pos}))

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self._write(b'{"op": "close"}\n', time.monotonic() + 5)
                self.proc.wait(timeout=5)
            except (ChipError, subprocess.TimeoutExpired):
                pass
        if self.proc:
            self._kill()
            self._release()
        self.proc = None
