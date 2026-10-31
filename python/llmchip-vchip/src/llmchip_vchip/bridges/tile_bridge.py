"""The tile bridge: a generated `taalas-tile/1` chip under the pin-level tile testbench, served as
a virtual chip (the `process` protocol of adapters/process.py).

    python -m llmchip_vchip.bridges.tile_bridge CHIP_DIR BUILD_DIR

The testbench (tb/tile_tb.cpp) drives the chip's parallel host pins and counts the busy cycles;
the chip tracks its own position, and the host gives it the starting position of each step. The
bridge passes token and cycles on unchanged.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

from ..interface import BaseChip, ChipError, StepResult
from ..manifest import load
from ..serve import serve
from .verilator import container, remove_container

TESTBENCH = "llmchip-vchip tile pin-level testbench (tb/tile_tb.cpp) on Verilator"
BINARY = "bundle/rtl/obj_tile/Vtile"
HOST_CHECKS = ("busy_pin_counted",)


class TileSim(BaseChip):
    def __init__(self, chip_dir: Path, build_dir: Path) -> None:
        m = load(chip_dir)
        super().__init__(m.meta())
        self.design = json.loads((build_dir / "bundle" / "design.json").read_text())
        if self.design.get("architecture") != "taalas-tile/1":
            raise ChipError("unsupported", f"not a tile bundle: {self.design.get('architecture')!r}")
        image = m.data["simulator"]["image"]
        self.name = f"llmchip-vchip-tile-{os.getpid()}-{secrets.token_hex(3)}"
        command = container(image, [f"{build_dir}:/work:ro"], f"/work/{BINARY}", name=self.name)
        # The chip's ROM and norm files sit at the bundle root; the binary reads them from there.
        command.insert(command.index("run") + 1, "--workdir=/work/bundle")
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        signal.signal(signal.SIGTERM, self._terminate)

    def _terminate(self, *_args) -> None:
        self._remove()
        sys.exit(143)

    def _remove(self) -> None:
        remove_container(self.name, timeout=1.0)

    def _cmd(self, line: str, expect: str) -> list[str]:
        if self.proc.stdin is None or self.proc.stdout is None:
            raise ChipError("crash", "the testbench has no pipes")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        answer = self.proc.stdout.readline().split()
        if not answer or answer[0] != expect or self.proc.poll() not in (None, 0):
            raise ChipError("crash", f"the tile testbench answered {answer!r} to {line!r}")
        return answer

    def hello_info(self) -> dict:
        return {"measurement": "testbench", "testbench": TESTBENCH}

    def reset(self) -> None:
        self._cmd("RESET", "OK")

    def step(self, token: int, pos: int) -> StepResult:
        self.check_input(token, pos)
        answer = self._cmd(f"STEP {token} {pos}", "STEP")
        if len(answer) < 3 or "TIMEOUT" in answer:
            raise ChipError("timeout", f"the chip did not finish the step at position {pos}")
        return StepResult(
            int(answer[1]),
            cycles=int(answer[2]),
            host_cycles=0,
            cycles_source="testbench",
            host_cycles_source="testbench",
        )

    def op_host_check(self) -> dict:
        """The tile host's pins: the busy pin must be high for every step (it is what the
        testbench counts). The spatial host's STATUS/READ checks do not apply."""
        self.reset()
        first = self._cmd("STEP 2 0", "STEP")
        self.reset()
        checks = {"busy_pin_counted": int(first[2]) > 0}
        return {"checks": checks, "details": {"first": first}}

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
    chip = TileSim(Path(sys.argv[1]), Path(sys.argv[2]))
    try:
        serve(chip)
    finally:
        chip._remove()


if __name__ == "__main__":
    main()
