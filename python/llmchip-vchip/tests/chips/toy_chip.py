"""A process chip for the harness's own tests (adapters/process.py protocol).

Serves either a toy model (`next = (31 * token + 7 * pos + 11) % vocab`, never the stop
token 1 unless asked) or, with --wrap DIR, an existing chip, and adds a cycle model.
Faults on demand: --crash-at P (exit), --hang-at P (stop answering), --garbage-at P (write
non-JSON), --wrong-at P (return a different token), --stop-at P (return the stop token),
--bad-token-at P (return a token outside the vocabulary). Each fault fires once (a marker
file in the chip directory survives the restart after a crash) unless --sticky.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from llmchip_vchip.interface import BaseChip, ChipMeta, StepResult
from llmchip_vchip.manifest import load
from llmchip_vchip.registry import open_chip
from llmchip_vchip.serve import serve


class ToyChip(BaseChip):
    def __init__(self, meta: ChipMeta, args) -> None:
        super().__init__(meta)
        self.args = args
        self.inner = open_chip(load(Path(args.wrap))) if args.wrap else None
        self.dir = Path(args.chip_dir)

    def reset(self) -> None:
        if self.inner:
            self.inner.reset()

    def _fault(self, name: str, pos: int) -> bool:
        at = getattr(self.args, f"{name}_at")
        marker = self.dir / f".fired-{name}"
        if at is None or pos != at or (marker.exists() and not self.args.sticky):
            return False
        marker.touch()
        return True

    def step(self, token: int, pos: int) -> StepResult:
        self.check_input(token, pos)
        if self._fault("crash", pos):
            sys.exit(3)
        if self._fault("hang", pos):
            time.sleep(3600)
        if self._fault("garbage", pos):
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
        if self.inner:
            r = self.inner.step(token, pos)
        else:
            nxt = (31 * token + 7 * pos + 11) % self.meta.vocab
            r = StepResult(nxt if nxt != 1 else 2)
        if self._fault("wrong", pos):
            r.token = (r.token + 1) % self.meta.vocab
        if self._fault("stop", pos):
            r.token = 1
        if self._fault("bad_token", pos):
            r.token = self.meta.vocab + 5
        r.cycles = self.args.cycles + pos if self.args.cycles else 0  # --cycles 0: reports none
        if self.args.host_cycles is not None:
            r.host_cycles = self.args.host_cycles
        return r


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("chip_dir")
    p.add_argument("--wrap")
    p.add_argument("--cycles", type=int, default=1000)
    p.add_argument("--host-cycles", type=int)
    p.add_argument("--sticky", action="store_true")
    for fault in ("crash", "hang", "garbage", "wrong", "stop", "bad-token"):
        p.add_argument(f"--{fault}-at", type=int)
    args = p.parse_args()
    meta = load(Path(args.chip_dir)).meta()
    serve(ToyChip(meta, args))


if __name__ == "__main__":
    main()
