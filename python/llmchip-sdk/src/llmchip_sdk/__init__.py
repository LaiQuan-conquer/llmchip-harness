"""Python SDK for llmchip-harness out-of-process plugins (IF-2, architecture §9.2).

A plugin process reads newline-delimited JSON-RPC 2.0 requests from stdin and
writes responses and notifications to stdout. ``print`` and anything else that
writes to ``sys.stdout`` is redirected to stderr, which the kernel captures as
the run log, so handler output can never corrupt the protocol stream.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

PROTOCOL_VERSION = "0.1.0"

LogLevel = Literal["debug", "info", "warn", "error"]


@dataclass
class Input:
    name: str
    hash: str
    type: str
    label: str
    path: str


@dataclass
class Output:
    """An output file, relative to ``InvokeContext.out_dir``."""

    name: str
    path: str
    type: str
    media_type: str | None = None
    label: str | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"name": self.name, "path": self.path, "type": self.type}
        if self.media_type:
            wire["mediaType"] = self.media_type
        if self.label:
            wire["label"] = self.label
        return wire


@dataclass
class Result:
    """Outputs, metrics and parse status of a handler. Plugins never report verdicts (AP-7)."""

    outputs: list[Output] = field(default_factory=list)
    metrics: dict[str, float | int | str | bool] = field(default_factory=dict)
    parse: dict[str, str] | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"outputs": [o.to_wire() for o in self.outputs], "metrics": self.metrics, "parse": self.parse}


class Cancelled(Exception):
    """Raised by ``InvokeContext.check_cancelled`` after the kernel cancels the run."""


class InvokeContext:
    def __init__(self, plugin: Plugin, params: dict[str, Any]) -> None:
        self._plugin = plugin
        self.run: str = params["run"]
        self.params: dict[str, Any] = params.get("params", {})
        self.inputs: list[Input] = [Input(**i) for i in params.get("inputs", [])]
        self.out_dir: str = params["outDir"]
        self.work_dir: str = params["workDir"]
        self.cancelled = threading.Event()

    def input(self, name: str) -> Input:
        for item in self.inputs:
            if item.name == name:
                return item
        raise KeyError(f"missing input {name}")

    @property
    def resume(self) -> Input | None:
        """The checkpoint to continue from, when the kernel restarts this work (input ``resume``).

        The kernel passes it only to handlers that declare an optional input ``resume`` of type
        ``run.checkpoint``.
        """
        return next((item for item in self.inputs if item.name == "resume"), None)

    def checkpoint(self, path: str, name: str = "checkpoint") -> None:
        """Reports a checkpoint file, relative to ``work_dir``.

        The kernel stores it as an artifact at once, so that a retry, or a pipeline restarting
        this work after an interruption, gets it back as the input ``resume``. Write the file
        completely (for example to a temporary name, then rename) before calling this.
        """
        self._plugin._send(
            {"jsonrpc": "2.0", "method": "checkpoint", "params": {"run": self.run, "name": name, "path": path}}
        )

    def progress(self, fraction: float | None = None, message: str | None = None) -> None:
        params: dict[str, Any] = {"run": self.run}
        if fraction is not None:
            params["fraction"] = fraction
        if message:
            params["message"] = message
        self._plugin._send({"jsonrpc": "2.0", "method": "progress", "params": params})

    def log(self, level: LogLevel, message: str) -> None:
        self._plugin._send(
            {"jsonrpc": "2.0", "method": "log", "params": {"run": self.run, "level": level, "message": message}}
        )

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled(f"run {self.run} was cancelled")


Handler = Callable[[InvokeContext], Result]


class Plugin:
    def __init__(self, plugin_id: str, version: str, handlers: dict[str, Handler]) -> None:
        self.id = plugin_id
        self.version = version
        self.handlers = handlers
        self._lock = threading.Lock()
        self._contexts: dict[str, InvokeContext] = {}
        self._threads: list[threading.Thread] = []
        # Keep the real stdout for the protocol; everything else goes to stderr.
        self._out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr

    def _send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":"))
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()

    def serve(self, stdin: Any = None) -> None:
        """Serves requests until ``shutdown`` or end of input."""
        stream = stdin or sys.stdin
        for line in stream:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "invalid JSON"}})
                continue
            if self._dispatch(message) == "shutdown":
                break
        # Let running handlers reply before the process exits.
        for thread in self._threads:
            thread.join()

    def _dispatch(self, message: dict[str, Any]) -> str | None:
        msg_id, method, params = message.get("id"), message.get("method"), message.get("params") or {}
        if method == "initialize":
            if params.get("protocolVersion") != PROTOCOL_VERSION:
                self._error(msg_id, -32000, f"unsupported protocol version {params.get('protocolVersion')}")
            else:
                self._reply(msg_id, {"id": self.id, "version": self.version, "handlers": list(self.handlers)})
        elif method == "invoke":
            # Run in a thread so that `cancel` notifications are still read.
            thread = threading.Thread(target=self._invoke, args=(msg_id, params), daemon=True)
            self._threads.append(thread)
            thread.start()
        elif method == "cancel":
            ctx = self._contexts.get(params.get("run", ""))
            if ctx:
                ctx.cancelled.set()
        elif method == "shutdown":
            self._reply(msg_id, {"ok": True})
            return "shutdown"
        elif msg_id is not None:
            self._error(msg_id, -32601, f"unknown method {method}")
        return None

    def _invoke(self, msg_id: Any, params: dict[str, Any]) -> None:
        handler = self.handlers.get(params.get("handler", ""))
        if handler is None:
            self._error(msg_id, -32001, f"plugin {self.id} has no handler {params.get('handler')}")
            return
        ctx = InvokeContext(self, params)
        self._contexts[ctx.run] = ctx
        try:
            self._reply(msg_id, handler(ctx).to_wire())
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            self._error(msg_id, -32001, f"{type(error).__name__}: {error}")
        finally:
            self._contexts.pop(ctx.run, None)

    def _reply(self, msg_id: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _error(self, msg_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def run_plugin(plugin_id: str, version: str, handlers: dict[str, Handler]) -> None:
    """Serves a plugin over stdio. Call from the plugin's ``__main__``."""
    Plugin(plugin_id, version, handlers).serve()
