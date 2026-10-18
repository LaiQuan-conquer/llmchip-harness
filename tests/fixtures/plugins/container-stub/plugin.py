"""Probes the container sandbox: network, read-only inputs, writable outputs."""

import socket
import time
from pathlib import Path

from llmchip_sdk import Output, Result, run_plugin


def probe(ctx):
    data = Path(ctx.input("data").path)
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
        network = True
    except OSError:
        network = False
    try:
        data.write_text("tampered")
        inputs_writable = True
    except OSError:
        inputs_writable = False
    Path(ctx.out_dir, "copy.txt").write_text(data.read_text().upper())
    return Result(
        outputs=[Output("copy", "copy.txt", "test.report")],
        metrics={"network": network, "inputs_writable": inputs_writable, "input_path": str(data)},
        parse={"status": "complete", "parser": "container-stub/1"},
    )


def sleep(ctx):
    deadline = time.monotonic() + ctx.params["ms"] / 1000
    while time.monotonic() < deadline:
        ctx.check_cancelled()
        time.sleep(0.05)
    return Result(metrics={"slept": ctx.params["ms"]})


run_plugin("container-stub", "1.0.0", {"probe": probe, "sleep": sleep})
