"""The chip side of the `process` protocol: serves any `VirtualChip` over stdin/stdout.

A chip process only has to build its chip and call `serve(chip)`; see adapters/process.py
for the protocol.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import IO

from .adapters.process import PROTOCOL
from .interface import ChipError, StepResult, VirtualChip


def _step(r: StepResult) -> dict:
    return {k: v for k, v in asdict(r).items() if v is not None}


def handle(chip: VirtualChip, request: dict) -> dict | None:
    op = request.get("op")
    try:
        if op == "hello":
            info = getattr(chip, "hello_info", dict)()  # e.g. a bridge's {"measurement": "testbench"}
            return {"ok": True, "protocol": PROTOCOL, "id": chip.metadata().id, **info}
        if op == "reset":
            chip.reset()
            return {"ok": True}
        if op == "prefill":
            return {"ok": True, "results": [_step(r) for r in chip.prefill(list(request["tokens"]))]}
        if op == "step":
            return {"ok": True, **_step(chip.step(int(request["token"]), int(request["pos"])))}
        if op == "close":
            return None
        extra = getattr(chip, f"op_{op}", None) if isinstance(op, str) else None
        if extra is not None:
            params = {k: v for k, v in request.items() if k != "op"}
            return {"ok": True, **extra(**params)}
        return {"ok": False, "error": {"code": "unsupported", "message": f"unknown op {op!r}"}}
    except ChipError as e:
        return {"ok": False, "error": {"code": e.code, "message": e.message}}
    except Exception as e:
        return {"ok": False, "error": {"code": "crash", "message": f"{type(e).__name__}: {e}"}}


def serve(chip: VirtualChip, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> None:
    for line in stdin:
        if not line.strip():
            continue
        response = handle(chip, json.loads(line))
        if response is None:
            break
        stdout.write(json.dumps(response) + "\n")
        stdout.flush()
    chip.close()
