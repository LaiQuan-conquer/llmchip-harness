"""IF-2 entry point of the sim-verilator plugin (runs in the EDA container, Python 3.10).

Handlers: `lint` (FR-RTL-03), `qualify` (capability qualification, FR-VER-01),
`verify` (executes a verification plan, FR-VER-04..07) and `generate` (L3
generations for the Chip Playground, FR-PLY-01).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

import simlib
from llmchip_sdk import InvokeContext, Output, Result, run_plugin

HERE = Path(__file__).parent
JOBS = max(1, min(16, os.cpu_count() or 1))  # parallel simulator processes


def tool_versions() -> dict:
    out = {}
    for name, args in {"verilator": ["--version"], "iverilog": ["-V"]}.items():
        try:
            text = subprocess.run(
                [f"{simlib.TOOLS}/{name}", *args], capture_output=True, text=True, timeout=60, env=simlib.ENV
            ).stdout
            out[name] = text.strip().splitlines()[0]
        except Exception as error:  # reported, never fatal here
            out[name] = f"unavailable: {error}"
    try:
        import cocotb

        out["cocotb"] = cocotb.__version__
    except Exception as error:
        out["cocotb"] = f"unavailable: {error}"
    return out


# -- lint -----------------------------------------------------------------------


def lint(ctx: InvokeContext) -> Result:
    bundle = simlib.Bundle(ctx.input("rtl").path, Path(ctx.work_dir))
    report = bundle.lint()
    Path(ctx.out_dir, "lint.json").write_text(json.dumps({**report, "tools": tool_versions()}, indent=2))
    return Result(
        outputs=[Output("report", "lint.json", "lint.report", "application/json")],
        metrics={"lint_errors": report["lint_errors"], "lint_warnings": report["lint_warnings"]},
        parse={"status": "complete", "parser": "sim-verilator/lint"},
    )


# -- qualification ----------------------------------------------------------------


def qual_expected() -> list[tuple[int, int, int]]:
    acc, out = 0, []
    for i in range(96):
        a = (i * 977 + 13) % 65536 - 32768
        b = (i * 611 + 7) % 65536 - 32768
        acc = min(max(acc + a * b, -(2**31)), 2**31 - 1)
        out.append((i, acc, (acc + 4) >> 3))
    return out


def qual_run(simulator: str, source: str, work: Path) -> list[tuple[int, int, int]]:
    work.mkdir(parents=True, exist_ok=True)
    (work / "qual.v").write_text(source)
    (work / "qual_tb.v").write_text((HERE / "qual" / "qual_tb.v").read_text())
    if simulator == "verilator":
        simlib.run(
            [f"{simlib.TOOLS}/verilator", "--binary", "--top-module", "qual_tb", "-o", "Vqual", "qual_tb.v", "qual.v"],
            work,
        )
        text = simlib.run([str(work / "obj_dir" / "Vqual")], work)
    else:
        simlib.run([f"{simlib.TOOLS}/iverilog", "-g2012", "-o", "qual.vvp", "qual_tb.v", "qual.v"], work)
        text = simlib.run([f"{simlib.TOOLS}/vvp", "-n", "qual.vvp"], work)
    return [tuple(int(v) for v in line.split()[1:4]) for line in text.splitlines() if line.startswith("Q ")]


def qualify(ctx: InvokeContext) -> Result:
    """The simulators must reproduce a known design bit for bit and detect a seeded defect."""
    source = (HERE / "qual" / "qual.v").read_text()
    mutant = source.replace("acc} + 33'sd4", "acc} + 33'sd3")
    assert mutant != source
    expected = qual_expected()
    report: dict = {"capability": "rtl-functional-sim", "tools": tool_versions(), "simulators": {}}
    metrics: dict = {}
    for sim in ("verilator", "icarus"):
        good = qual_run(sim, source, Path(ctx.work_dir) / sim / "good")
        bad = qual_run(sim, mutant, Path(ctx.work_dir) / sim / "mutant")
        report["simulators"][sim] = {
            "vectors": len(expected),
            "matches": good == expected,
            "mutantDetected": bad != expected,
        }
        metrics[f"{sim}.known_good_match"] = good == expected
        metrics[f"{sim}.mutant_detected"] = bad != expected
    metrics["qualified"] = all(metrics.values())
    Path(ctx.out_dir, "qualification.json").write_text(json.dumps(report, indent=2))
    return Result(
        outputs=[Output("report", "qualification.json", "qualification.report", "application/json")],
        metrics=metrics,
        parse={"status": "complete", "parser": "sim-verilator/qualify"},
    )


# -- verification -------------------------------------------------------------------


class Verification:
    def __init__(self, ctx: InvokeContext, bundle: simlib.Bundle, trace: dict, plan: dict) -> None:
        self.ctx = ctx
        self.bundle = bundle
        self.trace = trace
        self.plan = plan
        self.layers = bundle.design["model"]["n_layers"]
        self.prompts = sorted(int(k[1:].split(".")[0]) for k in trace if k.startswith("p") and k.endswith(".tokens"))
        self.randoms = sorted(int(k[1:].split(".")[0]) for k in trace if k.startswith("r") and k.endswith(".tokens"))
        self.steps = int(trace["meta.steps"])
        self.divergence: dict | None = None
        self.step_cycles: int | None = None

    # Every item returns (status, metrics, details); status passed | failed.

    def compare_runs(self, keys: list[str], name: str, coverage: bool = False) -> tuple[list[dict], list]:
        # As many steps as L2 ran: the eval set's step count, or every position of a random sequence.
        lines = [
            simlib.run_line(
                i,
                len(self.trace[f"{k}.tokens"]) if k.startswith("r") else self.steps,
                [int(t) for t in self.trace[f"{k}.prompt"]],
            )
            for i, k in enumerate(keys)
        ]
        records = self.bundle.simulate(lines, coverage=coverage, name=name, jobs=JOBS)
        results = [simlib.compare_with_l2(records, self.trace, i, k, self.layers) for i, k in enumerate(keys)]
        return results, records

    def localize(self, key: str, first: dict) -> dict:
        """First divergent operation and cycle of a sequence, with a waveform window around it."""
        tokens = simlib.tokens_in(self.trace, key)
        where = simlib.locate(self.bundle, tokens, min(first.get("pos", 0), len(tokens) - 1))
        divergence = {"sequence": key, "observation": first, "operation": where}
        if where and where.get("cycle") is not None:
            start = max(where["opCycles"][0], where["cycle"] - 5000)
            try:
                simlib.waveform(
                    self.bundle,
                    tokens,
                    where["pos"],
                    start,
                    where["cycle"] + 20,
                    Path(self.ctx.out_dir) / "divergence.vcd",
                )
                divergence["waveform"] = {
                    "file": "divergence.vcd",
                    "pos": where["pos"],
                    "cycles": [start, where["cycle"] + 20],
                    "program": "debug",
                }
            except Exception as error:
                divergence["waveform"] = {"error": str(error)[-1000:]}
        return divergence

    def summarize(self, results: list[dict], keys: list[str], prefix: str) -> tuple[str, dict, dict]:
        mismatches = sum(r["mismatches"] for r in results)
        metrics = {
            f"{prefix}_sequences": len(results),
            f"{prefix}_steps": sum(r["steps"] for r in results),
            f"{prefix}_values": sum(r["values"] for r in results),
            f"{prefix}_mismatches": mismatches,
        }
        details: dict = {"sequences": [{"key": k, **r} for k, r in zip(keys, results, strict=False)]}
        if mismatches:
            k, r = next((k, r) for k, r in zip(keys, results, strict=False) if r["mismatches"])
            div = self.localize(k, r["first"])
            details["divergence"] = div
            self.divergence = self.divergence or div
        return ("failed" if mismatches else "passed"), metrics, details

    def regression_evalset(self, item: dict) -> tuple:
        keys = [f"p{i}" for i in self.prompts]
        results, records = self.compare_runs(keys, "regression")
        status, metrics, details = self.summarize(results, keys, "regression")
        lengths = [len(self.trace[f"{k}.tokens"]) for k in keys]
        cycles = [r["cycles"] for r in records]
        self.step_cycles = int(np.median(cycles)) if cycles else None
        metrics |= {
            "regression_prompts": len(keys),
            "tokens_per_prompt": min(lengths) if lengths else 0,
            "regression_cycles": int(sum(cycles)),
            "cycles_per_token": self.step_cycles or 0,
        }
        return status, metrics, details

    def directed_reset_mid_inference(self, item: dict) -> tuple:
        key = f"p{self.prompts[0]}"
        reset_at = max((self.step_cycles or 2000) // 2, 20)
        line = simlib.run_line(0, self.steps, [int(t) for t in self.trace[f"{key}.prompt"]], reset_cycles=reset_at)
        records = self.bundle.simulate([line], name="reset")
        result = simlib.compare_with_l2(records, self.trace, 0, key, self.layers)
        status, metrics, details = self.summarize([result], [key], "reset")
        return (
            status,
            metrics,
            details
            | {
                "resetAfterCycles": reset_at,
                "note": "reset during position 1, then the sequence restarts from position 0",
            },
        )

    def isa_check(self, tokens: list[int], name: str) -> dict:
        records = self.bundle.simulate([simlib.run_line(0, len(tokens), tokens)], name=name)
        return simlib.compare_with_isa(records, self.bundle.machine(), 0, tokens, self.layers) | {"records": records}

    def isa_greedy(self, prompt: list[int], steps: int) -> list[int]:
        """Input tokens of a greedy run on the instruction-level simulator (BOS does not stop it)."""
        machine = self.bundle.machine()
        tokens = list(prompt)
        for pos in range(steps):
            out = machine.step(tokens[pos], pos, [])
            if pos == len(tokens) - 1 and pos < steps - 1:
                tokens.append(out)
        return tokens[:steps]

    def directed_single_token(self, item: dict) -> tuple:
        # An empty prompt encodes to BOS alone, so this also covers the empty input.
        steps = min(self.steps, self.bundle.params["CTX"])
        records = self.bundle.simulate([simlib.run_line(0, steps, [1])], name="single")
        tokens = [1, *[r["next"] for r in records][:-1]]
        result = simlib.compare_with_isa(records, self.bundle.machine(), 0, tokens, self.layers)
        reference = self.isa_greedy([1], len(records))
        ok = result["mismatches"] == 0 and tokens == reference[: len(tokens)]
        return (
            ("passed" if ok else "failed"),
            {"single_token_steps": len(records), "single_token_mismatches": result["mismatches"]},
            {"reference": "isa", **result},
        )

    def directed_max_context(self, item: dict) -> tuple:
        ctx_len = self.bundle.params["CTX"]
        tokens = self.isa_greedy([1], ctx_len)
        result = self.isa_check(tokens, "maxctx")
        records = result.pop("records")
        full = len(records) == ctx_len and not any(r["error"] for r in records)
        beyond = [c for c in self.illegal_inputs() if c.get("pos", -1) >= ctx_len]
        ok = result["mismatches"] == 0 and full and len(beyond) == 2 and all(c["error"] for c in beyond)
        return (
            ("passed" if ok else "failed"),
            {"max_context_steps": len(records), "max_context_mismatches": result["mismatches"]},
            {"reference": "isa", **result, "beyondContext": beyond},
        )

    def illegal_inputs(self, coverage: bool = False) -> list[dict]:
        ctx_len, vocab = self.bundle.params["CTX"], self.bundle.params["VOCAB"]
        cases = [(1, ctx_len), (1, ctx_len + 7), (vocab, 0), (vocab + 100, 3)]
        records = self.bundle.simulate(
            [f"ILLEGAL {i} {t} {p}" for i, (t, p) in enumerate(cases)], coverage=coverage, name="illegal"
        )
        out = [
            {"case": "input", "token": t, "pos": p, "error": bool(r["error"]), "cycles": r["cycles"]}
            for (t, p), r in zip(cases, records, strict=False)
        ]
        # A program word with an undefined opcode (a corrupted program ROM) must end the step with `error`.
        bits = self.bundle.isa.WORD_BITS
        (self.bundle.rtl / "prog_illegal.hex").write_text(f"{0xFF:0{-(-bits // 4)}x}\n")
        records = self.bundle.simulate(
            [simlib.run_line(9, 1, [1])], coverage=coverage, name="illegal_op", prog="prog_illegal.hex"
        )
        out += [{"case": "opcode", "opcode": 255, "error": bool(r["error"]), "cycles": r["cycles"]} for r in records]
        return out

    def directed_illegal_input(self, item: dict) -> tuple:
        cases = self.illegal_inputs()
        ok = len(cases) == 5 and all(c["error"] for c in cases)
        return ("passed" if ok else "failed"), {"illegal_cases": len(cases)}, {"reference": "chip spec", "cases": cases}

    def directed_start_while_busy(self, item: dict) -> tuple:
        # The testbench pulses `start` with a different token and position inside every
        # step; a sequence equal to L2 shows the pulses were ignored.
        key = f"p{self.prompts[-1]}"
        results, _ = self.compare_runs([key], "busy")
        status, metrics, details = self.summarize(results, [key], "start_while_busy")
        return (
            status,
            metrics,
            details | {"stimulus": "a START frame with token^1 and pos+1, 1000 cycles into every step"},
        )

    def directed_debug_readback(self, item: dict) -> tuple:
        key = f"p{self.prompts[0]}"
        tokens = simlib.tokens_in(self.trace, key)
        upto = min(int(item.get("params", {}).get("positions", 4)), len(tokens)) - 1
        result = simlib.compare_debug(self.bundle, tokens, upto)
        if result["mismatches"]:
            self.divergence = self.divergence or {"sequence": key, "operation": result["first"]}
        return (
            ("passed" if result["mismatches"] == 0 else "failed"),
            {"debug_operations": result["operations"], "debug_mismatches": result["mismatches"]},
            {"reference": "isa", **result},
        )

    def random_tokens(self, item: dict) -> tuple:
        keys = [f"r{i}" for i in self.randoms]
        if not keys:
            return "failed", {"random_sequences": 0}, {"error": "the L2 trace has no random sequences"}
        results, _ = self.compare_runs(keys, "random")
        return self.summarize(results, keys, "random")

    def alu(self, items: list[dict]) -> dict:
        """Runs the selected ALU unit tests in one cocotb session on Icarus Verilog."""
        from cocotb_tools.runner import get_runner

        work = Path(self.ctx.work_dir) / "alu"
        work.mkdir(parents=True, exist_ok=True)
        results_file = work / "alu-results.json"
        h1_frac = next(
            f[4]
            for op, f in (
                self.bundle.isa.decode(int(w)) for w in np.load(self.bundle.root / "isa-image.npz")["program"]
            )
            if op == "SILU"
        )
        # The runner finds the simulator on the process's own PATH.
        os.environ["PATH"] = simlib.ENV["PATH"]
        os.environ["LD_LIBRARY_PATH"] = simlib.ENV["LD_LIBRARY_PATH"]
        runner = get_runner("icarus")
        runner.build(
            sources=[self.bundle.rtl / "llmchip_alu.v"],
            includes=[self.bundle.rtl],
            hdl_toplevel="llmchip_alu",
            build_dir=work / "build",
            always=True,
        )
        env = {
            "ALU_TESTS": ",".join(i["id"] for i in items),
            "ISA_PATH": str(self.bundle.root / "isa.py"),
            "H1_FRAC": str(h1_frac),
            "DW": str(self.bundle.params["DW"]),
            "RSH_POSITIVE": str(self.bundle.params.get("RSH_POSITIVE", 0)),
            "SILU_FRAC_GE4": str(self.bundle.params.get("SILU_FRAC_GE4", 0)),
            "RESULTS": str(results_file),
            "PYTHONPATH": os.pathsep.join([str(HERE), *sys.path]),
            "PATH": simlib.ENV["PATH"],
            "LD_LIBRARY_PATH": simlib.ENV["LD_LIBRARY_PATH"],
        }
        runner.test(
            test_module="cocotb_alu",
            hdl_toplevel="llmchip_alu",
            build_dir=work / "build",
            test_dir=work,
            extra_env=env,
            results_xml="results.xml",
        )
        return json.loads(results_file.read_text()) if results_file.exists() else {}

    def coverage_line(self, item: dict) -> tuple:
        threshold = float(item.get("params", {}).get("threshold", 90))
        keys = [f"p{i}" for i in self.prompts[:2]]
        lines = [
            simlib.run_line(i, self.steps, [int(t) for t in self.trace[f"{k}.prompt"]]) for i, k in enumerate(keys)
        ]
        reset_at = max((self.step_cycles or 2000) // 2, 20)
        lines.append(
            simlib.run_line(len(lines), 3, [int(t) for t in self.trace[f"{keys[0]}.prompt"]], reset_cycles=reset_at)
        )
        files: list[str] = []
        self.bundle.simulate(lines, coverage=True, name="coverage", jobs=JOBS)
        files += self.bundle.coverage_files
        self.bundle.simulate([simlib.run_line(0, 2, [1, 5])], debug=True, coverage=True, name="coverage_debug")
        files += self.bundle.coverage_files
        self.illegal_inputs(coverage=True)
        files += ["illegal.dat", "illegal_op.dat"]
        summary = simlib.coverage_summary(self.bundle, files)
        ok = summary["pct"] >= threshold
        return (
            ("passed" if ok else "failed"),
            {"coverage_line_pct": summary["pct"], "coverage_threshold": threshold},
            {"reference": "threshold", **summary},
        )

    def execute(self) -> list[dict]:
        handlers = {
            "regression.evalset": self.regression_evalset,
            "directed.reset_mid_inference": self.directed_reset_mid_inference,
            "directed.single_token": self.directed_single_token,
            "directed.max_context": self.directed_max_context,
            "directed.illegal_input": self.directed_illegal_input,
            "directed.start_while_busy": self.directed_start_while_busy,
            "directed.debug_readback": self.directed_debug_readback,
            "random.tokens": self.random_tokens,
            "coverage.line": self.coverage_line,
        }
        alu_ids = {"boundary.alu", "exhaustive.silu_index", "exhaustive.exp_index", "exhaustive.rsqrt", "random.alu"}
        items = self.plan["items"]
        # The regression runs first: later items reuse its cycles per step.
        ordered = sorted(items, key=lambda i: i["id"] != "regression.evalset")
        alu_results: dict | None = None
        out = {}
        for n, item in enumerate(ordered):
            self.ctx.check_cancelled()
            self.ctx.progress(n / len(ordered), item["id"])
            entry = {"id": item["id"], "category": item.get("category"), "evidence": "simulation"}
            try:
                if item["id"] in alu_ids:
                    if alu_results is None:
                        alu_results = self.alu([i for i in items if i["id"] in alu_ids])
                    r = alu_results.get(item["id"])
                    if r is None:
                        entry |= {"status": "failed", "details": {"error": "the unit test did not report"}}
                    else:
                        entry |= {
                            "status": "passed" if r["failures"] == 0 else "failed",
                            "simulator": "icarus+cocotb",
                            "reference": "isa.py functions",
                            "metrics": {"vectors": r["vectors"], "failures": r["failures"]},
                            "details": r,
                        }
                elif item["id"] in handlers:
                    status, metrics, details = handlers[item["id"]](item)
                    entry |= {
                        "status": status,
                        "simulator": "verilator",
                        "reference": details.get("reference", "L2"),
                        "metrics": metrics,
                        "details": details,
                    }
                else:
                    entry |= {"status": "not_executed", "details": {"error": "no implementation for this item"}}
            except Exception as error:
                entry |= {"status": "failed", "details": {"error": f"{type(error).__name__}: {str(error)[-2000:]}"}}
            out[item["id"]] = entry
        return [out[i["id"]] for i in items]


def load_trace(path: str) -> dict:
    with np.load(path) as z:
        return {k: z[k] for k in z.files}


def verify(ctx: InvokeContext) -> Result:
    spec = ctx.input("spec")
    bundle = simlib.Bundle(ctx.input("rtl").path, Path(ctx.work_dir))
    trace = load_trace(ctx.input("trace").path)
    plan = json.loads(Path(ctx.input("plan").path).read_text())
    if bundle.design.get("source", {}).get("numericSpec") != spec.hash:
        raise ValueError("the RTL bundle was generated from a different numeric spec")
    if str(trace.get("meta.spec", "")) != spec.hash:
        raise ValueError("the L2 trace was computed from a different numeric spec")
    v = Verification(ctx, bundle, trace, plan)
    items = v.execute()
    counts = {s: sum(1 for i in items if i["status"] == s) for s in ("passed", "failed", "not_executed")}
    metrics: dict = {
        "items_total": len(items),
        "items_executed": counts["passed"] + counts["failed"],
        "items_passed": counts["passed"],
        "items_failed": counts["failed"],
        "items_not_executed": counts["not_executed"],
    }
    for item in items:
        # Unit-test items share metric names, so theirs are prefixed with the item id.
        prefix = f"{item['id']}." if item.get("simulator") == "icarus+cocotb" else ""
        for k, value in (item.get("metrics") or {}).items():
            metrics[prefix + k] = value
    outputs = [Output("report", "verification-report.json", "verification.report", "application/json")]
    if v.divergence:
        op = v.divergence.get("operation") or {}
        obs = v.divergence.get("observation") or {}
        metrics |= {
            "divergence_sequence": v.divergence["sequence"],
            "divergence_pos": op.get("pos", obs.get("pos")),
            "divergence_point": obs.get("point", "operation"),
            "divergence_layer": obs.get("layer", -1),
        }
        if op.get("op") is not None:
            metrics |= {
                "divergence_op": op["op"],
                "divergence_operation": op["operation"],
                "divergence_element": op["element"],
                "divergence_cycle": op["cycle"],
            }
        if (Path(ctx.out_dir) / "divergence.vcd").exists():
            outputs.append(Output("waveform", "divergence.vcd", "waveform.vcd", "text/plain"))
    metrics = {k: v_ for k, v_ in metrics.items() if v_ is not None}
    report = {
        "format": "llmchip-verification-report/1",
        "design": {"top": bundle.design["top"], "params": bundle.params, "source": bundle.design.get("source", {})},
        "tools": tool_versions(),
        # FR-VER-07: evidence types are stated separately; this backend produces test evidence only.
        "evidence": {
            "simulation": [i["id"] for i in items if i["status"] != "not_executed"],
            "formal": [],
            "statement": "All results are simulation tests with the coverage stated; no formal proof was made.",
        },
        "summary": metrics,
        "divergence": v.divergence,
        "items": items,
    }
    Path(ctx.out_dir, "verification-report.json").write_text(
        json.dumps(report, indent=2, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    )
    return Result(outputs=outputs, metrics=metrics, parse={"status": "complete", "parser": "sim-verilator/verify"})


# -- gate-level simulation (L4) ---------------------------------------------------------


def gatesim(ctx: InvokeContext) -> Result:
    """Zero-delay functional simulation of the routed netlist against L2 (FR-SGN-03, L4), on Icarus Verilog."""
    import tarfile

    bundle = simlib.Bundle(ctx.input("rtl").path, Path(ctx.work_dir))
    layout = Path(ctx.work_dir) / "layout"
    with tarfile.open(ctx.input("layout").path) as tar:
        tar.extractall(layout)
    trace = load_trace(ctx.input("trace").path)
    spec = ctx.input("spec")
    if str(trace.get("meta.spec", "")) != spec.hash or bundle.design.get("source", {}).get("numericSpec") != spec.hash:
        raise ValueError("the RTL bundle or the L2 trace belongs to a different numeric spec")
    prompts = int(ctx.params.get("prompts", 2))
    steps = int(ctx.params.get("steps", 16))
    keys = sorted(
        (k.split(".")[0] for k in trace if k.startswith("p") and k.endswith(".tokens")), key=lambda k: int(k[1:])
    )[:prompts]
    layers = bundle.design["model"]["n_layers"]
    ctx.progress(0.1, "building the gate-level model")
    bundle.build_gate(layout)
    ctx.progress(0.3, "simulating")
    runs = [
        (i, min(steps, len(trace[f"{k}.tokens"])), [int(t) for t in trace[f"{k}.prompt"]]) for i, k in enumerate(keys)
    ]
    records = bundle.simulate_gate(layout, runs, jobs=JOBS)
    results = []
    for i, k in enumerate(keys):
        r = simlib.compare_with_l2(records, {**trace, f"{k}.tokens": trace[f"{k}.tokens"][:steps]}, i, k, layers)
        results.append({"key": k, **r})
    steps_done = sum(r["steps"] for r in results)
    metrics = {
        "gl_prompts": len(keys),
        "gl_tokens_per_prompt": min((r["steps"] for r in results), default=0),
        "gl_steps": steps_done,
        "gl_values": sum(r["values"] for r in results),
        "gl_mismatches": sum(r["mismatches"] for r in results),
        "gl_cycles_per_token": int(np.median([r["cycles"] for r in records])) if records else 0,
    }
    report = {
        "level": "L4",
        "evidence": "zero-delay gate-level simulation of the routed netlist (Icarus Verilog, PDK cell models)",
        "netlist": "views/chip_top.nl.v",
        "sequences": results,
        "summary": metrics,
    }
    Path(ctx.out_dir, "gatesim.json").write_text(
        json.dumps(report, indent=2, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    )
    return Result(
        outputs=[Output("report", "gatesim.json", "gatesim.report", "application/json")],
        metrics=metrics,
        parse={"status": "complete", "parser": "sim-verilator/gatesim"},
    )


# -- SDF timing simulation (supplementary evidence, FR-VER-02/03) ------------------------

TIMING_FREE = ("fill", "tap", "endcap", "antenna", "filltie", "decap", "tiel", "tieh")


def sdfsim(ctx: InvokeContext) -> Result:
    """Gate-level simulation with SDF back-annotation (Icarus Verilog). The result states how
    complete the annotation was; incomplete or ignored annotation is never a pass (AC-12)."""
    import re
    import tarfile

    work = Path(ctx.work_dir)
    layout = work / "layout"
    with tarfile.open(ctx.input("layout").path) as tar:
        tar.extractall(layout)
    models = json.loads((layout / "sim" / "models.json").read_text())
    netlist = layout / "views" / f"{models['top']}.nl.v"
    sdf = work / "design.sdf"
    sdf.write_bytes(Path(ctx.input("sdf").path).read_bytes())
    period = float(ctx.params.get("periodNs", 125))
    # Instances with timing arcs in the netlist, and the instances the SDF annotates.
    text = netlist.read_text()
    cells = re.findall(r"^\s*(gf180mcu_fd_sc_\w+)\s+(\S+)\s*\(", text, re.M)
    timed = {inst.lstrip("\\") for cell, inst in cells if not any(t in cell.split("__")[-1] for t in TIMING_FREE)}
    sdf_text = sdf.read_text(errors="replace")
    annotated = {i.replace("\\", "") for i in re.findall(r"\(INSTANCE\s+([^)\s]+)\s*\)", sdf_text)}
    missing = sorted(timed - annotated)
    cmd = [
        f"{simlib.TOOLS}/iverilog",
        "-g2012",
        "-gspecify",
        "-ginterconnect",
        "-o",
        "sdf.vvp",
        f"-DPERIOD={period}",
        f'-DSDF_FILE="{sdf}"',
        str(HERE / "sdf_tb.v"),
        str(netlist),
        str(layout / models["pads"]),
        *models["cells"],
    ]
    simlib.run(cmd, work, timeout=7200)
    proc = subprocess.run(
        [f"{simlib.TOOLS}/vvp", "-n", "sdf.vvp"], cwd=work, capture_output=True, text=True, timeout=7200, env=simlib.ENV
    )
    log = proc.stdout + proc.stderr
    (Path(ctx.out_dir) / "sdfsim.log").write_text(log[-200000:])
    # Every annotation problem the simulator reports (unknown cells, nets, arcs) is "ignored".
    ignored = len(re.findall(r"^(?:SDF |)(?:WARNING|ERROR|Warning|Error)\b.*", log, re.M))
    m = re.search(r"SDFTB errors (\d+)", log)
    metrics = {
        "sdf_instances_netlist": len(timed),
        "sdf_instances_annotated": len(timed & annotated),
        "sdf_missing": len(missing),
        "sdf_ignored": ignored,
        "sdf_annotation_errors_conditional_arcs": len(re.findall(r"Unable to match ModPath", log)),
        "sdf_annotation_complete": not missing and ignored == 0,
        "sim_completed": m is not None,
        "sim_errors": int(m.group(1)) if m else -1,
    }
    Path(ctx.out_dir, "sdfsim.json").write_text(
        json.dumps(
            {"metrics": metrics, "missing": missing[:200], "evidence": "SDF timing simulation (supplementary)"},
            indent=2,
        )
    )
    return Result(
        outputs=[Output("report", "sdfsim.json", "sdfsim.report", "application/json")],
        metrics=metrics,
        parse={"status": "complete" if m else "partial", "parser": "sim-verilator/sdfsim"},
    )


# -- playground -----------------------------------------------------------------------


def generate(ctx: InvokeContext) -> Result:
    """L3: generations of the RTL for the prompts of an evaluation set (Chip Playground)."""
    bundle = simlib.Bundle(ctx.input("rtl").path, Path(ctx.work_dir))
    vocab = bundle.params["VOCAB"]
    tokenizer = simlib.Tokenizer(Path(ctx.input("tokenizer").path).read_bytes(), vocab)
    evalset = json.loads(Path(ctx.input("evalset").path).read_text())
    steps = min(int(evalset.get("steps", 64)), bundle.params["CTX"])
    prompts = evalset["prompts"]
    ids = [tokenizer.encode(p) for p in prompts]
    records = bundle.simulate([simlib.run_line(i, steps, t) for i, t in enumerate(ids)], name="playground")
    generations = []
    for i, (prompt, prompt_ids) in enumerate(zip(prompts, ids, strict=False)):
        steps_ = [r for r in records if r["run"] == i]
        tokens = list(prompt_ids)
        for r in steps_:
            if r["pos"] >= len(prompt_ids) - 1 and r["next"] != 1:
                tokens.append(r["next"])
        generations.append(
            {
                "prompt": prompt,
                "promptTokens": prompt_ids,
                "tokens": tokens,
                "text": tokenizer.decode(tokens),
                "cyclesPerToken": [r["cycles"] for r in steps_],
            }
        )
    Path(ctx.out_dir, "l3.json").write_text(json.dumps({"layer": "L3", "generations": generations}, indent=2))
    cycles = [c for g in generations for c in g["cyclesPerToken"]]
    return Result(
        outputs=[Output("outputs", "l3.json", "l3.outputs", "application/json")],
        metrics={"prompts": len(generations), "cycles_per_token": int(np.median(cycles)) if cycles else 0},
        parse={"status": "complete", "parser": "sim-verilator/generate"},
    )


if __name__ == "__main__":
    run_plugin(
        "sim-verilator",
        "0.1.0",
        {
            "lint": lint,
            "qualify": qualify,
            "verify": verify,
            "gatesim": gatesim,
            "sdfsim": sdfsim,
            "generate": generate,
        },
    )
