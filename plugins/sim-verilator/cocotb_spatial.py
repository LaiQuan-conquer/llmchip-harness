"""cocotb tests of the bit-serial cells of a spatial bundle (sp_sadd feeding sp_out, through
spatial_unit.v) against the integer rules of the numeric spec: the result of a stream is
narrow(rshift(a +/- b, S), W) with the bundle's rounding and overflow modes.

Selected by the environment: UNIT_TESTS (comma-separated test ids), S, W, L, SUB,
ROUND_MODE (0 half-up, 1 half-even, 2 truncate), OVERFLOW_WRAP, RESULTS (JSON file; counts
add up over the parameter sets of one session).
"""

import json
import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge

SELECTED = set(os.environ.get("UNIT_TESTS", "").split(","))
S, W, L = int(os.environ["S"]), int(os.environ["W"]), int(os.environ["L"])
SUB = int(os.environ.get("SUB", "0"))
MODE = int(os.environ.get("ROUND_MODE", "0"))
WRAP = int(os.environ.get("OVERFLOW_WRAP", "0"))
LIMIT = 2 ** (L - 3)  # sums the L stream bits carry (the generator's length rule guarantees it)


def rsh(v: int, s: int) -> int:
    if MODE == 2:
        return v >> s
    if MODE == 1:
        return (v + (1 << (s - 1)) - 1 + ((v >> s) & 1)) >> s
    return (v + (1 << (s - 1))) >> s


def narrow(v: int) -> int:
    lo, hi = -(1 << (W - 1)), (1 << (W - 1)) - 1
    return ((v - lo) & ((1 << W) - 1)) + lo if WRAP else min(max(v, lo), hi)


def record(name: str, checked: int, failures: list) -> None:
    path = os.environ.get("RESULTS")
    if not path:
        return
    existing = {}
    if os.path.exists(path):
        with open(path) as f:
            existing = json.load(f)
    prev = existing.get(name, {"vectors": 0, "failures": 0, "examples": [], "parameterSets": []})
    existing[name] = {
        "vectors": prev["vectors"] + checked,
        "failures": prev["failures"] + len(failures),
        "examples": (prev["examples"] + failures)[:5],
        "parameterSets": prev["parameterSets"] + [{"S": S, "W": W, "L": L, "SUB": SUB}],
    }
    with open(path, "w") as f:
        json.dump(existing, f, indent=2)


async def run(dut, pairs) -> list:
    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    dut.rst_n.value = 0
    dut.start.value = 0
    dut.a.value = 0
    dut.b.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    failures = []
    for a, b in pairs:
        dut.start.value = 1
        await FallingEdge(dut.clk)
        dut.start.value = 0
        for k in range(L):
            dut.a.value = (a >> k) & 1
            dut.b.value = (b >> k) & 1
            await FallingEdge(dut.clk)
        got = dut.y.value.to_signed() if hasattr(dut.y.value, "to_signed") else dut.y.value.signed_integer
        want = narrow(rsh(a - b if SUB else a + b, S))
        if got != want:
            failures.append({"a": a, "b": b, "sub": SUB, "S": S, "W": W, "rtl": got, "expected": want})
    return failures


def boundary_pairs() -> list:
    half = 1 << (S - 1)
    edges = [0, 1, -1, 2, -2, 3, (1 << (W - 1)) - 2, (1 << (W - 1)) - 1, 1 << (W - 1), -(1 << (W - 1))]
    edges += [-(1 << (W - 1)) - 1, (1 << W) + 5, -(1 << W) - 5]
    deltas = [-half - 1, -half, -half + 1, -1, 0, 1, half - 1, half, half + 1]
    out = []
    for t in edges:
        for d in deltas:
            v = (t << S) + d
            if abs(v) < LIMIT and abs(v // 3) < LIMIT // 2:
                # The same sum from two operands: the adder's carry chain also sees it.
                b = v // 3
                out.append((v - b, b) if not SUB else (v + b, b))
    return out


@cocotb.test(skip="boundary.serial_output" not in SELECTED)
async def boundary_serial_output(dut):
    pairs = boundary_pairs()
    record("boundary.serial_output", len(pairs), await run(dut, pairs))


@cocotb.test(skip="random.serial_output" not in SELECTED)
async def random_serial_output(dut):
    rng = random.Random(S * 1000 + W * 10 + SUB)
    span = min(LIMIT // 2, 1 << (S + W + 1))
    pairs = [(rng.randrange(-span, span), rng.randrange(-span, span)) for _ in range(600)]
    record("random.serial_output", len(pairs), await run(dut, pairs))
