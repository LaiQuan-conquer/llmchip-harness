import json
import subprocess
import sys
import textwrap


def run_plugin(script: str, messages: list[dict]) -> list[dict]:
    lines = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)], input=lines, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines()]


ECHO = """
    from llmchip_sdk import Output, Result, run_plugin

    def echo(ctx):
        print("this goes to stderr, not the protocol stream")
        ctx.progress(0.5, "halfway")
        with open(f"{ctx.out_dir}/echo.txt", "w") as f:
            f.write(str(ctx.params["text"]))
        return Result(outputs=[Output("echo", "echo.txt", "text.echo")], metrics={"length": len(ctx.params["text"])},
                      parse={"status": "complete", "parser": "echo/1"})

    def boom(ctx):
        raise ValueError("bad input")

    run_plugin("echo", "1.0.0", {"echo": echo, "boom": boom})
"""


def test_initialize_invoke_and_shutdown(tmp_path):
    invoke = {
        "run": "r1",
        "handler": "echo",
        "params": {"text": "hi"},
        "inputs": [],
        "outDir": str(tmp_path),
        "workDir": str(tmp_path),
    }
    replies = run_plugin(
        ECHO,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "0.1.0", "kernelApiVersion": "0.2.0", "pluginDir": "."},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "invoke", "params": invoke},
        ],
    )
    assert replies[0] == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"id": "echo", "version": "1.0.0", "handlers": ["echo", "boom"]},
    }
    by_kind = {r.get("method", r.get("id")): r for r in replies}
    assert by_kind["progress"]["params"] == {"run": "r1", "fraction": 0.5, "message": "halfway"}
    assert by_kind[2]["result"]["metrics"] == {"length": 2}
    assert (tmp_path / "echo.txt").read_text() == "hi"


def test_handler_errors_and_protocol_mismatch(tmp_path):
    invoke = {
        "run": "r2",
        "handler": "boom",
        "params": {},
        "inputs": [],
        "outDir": str(tmp_path),
        "workDir": str(tmp_path),
    }
    replies = run_plugin(
        ECHO,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "9.9.9"}},
            {"jsonrpc": "2.0", "id": 2, "method": "invoke", "params": invoke},
            {"jsonrpc": "2.0", "id": 3, "method": "nope"},
        ],
    )
    errors = {r["id"]: r["error"] for r in replies if "error" in r}
    assert errors[1]["code"] == -32000
    assert errors[2]["message"] == "ValueError: bad input"
    assert errors[3]["code"] == -32601
