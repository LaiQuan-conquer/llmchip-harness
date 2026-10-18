"""cocotb tests of the arithmetic functions (llmchip_alu) against isa.py.

Selected by the environment: ALU_TESTS (comma-separated test ids), ISA_PATH,
H1_FRAC (fraction bits of h1 in the design), RESULTS (JSON file for counts).
"""

import importlib.util
import json
import os
import random

import cocotb
from cocotb.triggers import Timer

spec = importlib.util.spec_from_file_location("isa", os.environ["ISA_PATH"])
isa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(isa)

SELECTED = set(os.environ.get("ALU_TESTS", "").split(","))
COUNTS: dict = {}


def record(name: str, checked: int, failures: list) -> None:
    COUNTS[name] = {"vectors": checked, "failures": len(failures), "examples": failures[:5]}
    path = os.environ.get("RESULTS")
    if path:
        existing = {}
        if os.path.exists(path):
            with open(path) as f:
                existing = json.load(f)
        existing.update(COUNTS)
        with open(path, "w") as f:
            json.dump(existing, f, indent=2)


async def apply(dut, v=0, s=0, clamp=0, offset=0):
    dut.v.value = v
    dut.s.value = s
    dut.clamp.value = clamp
    dut.offset.value = offset
    await Timer(1, "ns")


async def check_rsh_sat(dut, cases):
    failures = []
    for v, s in cases:
        await apply(dut, v, s)
        want = (isa.rsh(v, s), isa.sat(v, 16), isa.sat(v, 32))
        got = (dut.rsh.value.to_signed(), dut.sat16.value.to_signed(), dut.sat32.value.to_signed())
        if got != want:
            failures.append({"v": v, "s": s, "got": got, "want": want})
    return failures


async def check_silu(dut, values, frac):
    failures = []
    clamp = 8 * 2**frac
    for h1 in values:
        await apply(dut, h1, frac, clamp, 128)
        want = isa.silu_index(h1, frac, clamp, 128)
        got = dut.silu_index.value.to_signed()
        if got != want:
            failures.append({"h1": h1, "frac": frac, "got": got, "want": want})
    return failures


async def check_exp(dut, values, cap=16383, shift=5):
    failures = []
    for d in values:
        await apply(dut, d, shift, cap)
        want = isa.exp_index(d, cap, shift)
        got = dut.exp_index.value.to_signed()
        if got != want:
            failures.append({"d": d, "got": got, "want": want})
    return failures


async def check_rsqrt(dut, values):
    failures = []
    for v in values:
        await apply(dut, v)
        want = isa.rsqrt_index(v)
        got = (int(dut.rsqrt_e2.value), int(dut.rsqrt_t.value))
        if got != want:
            failures.append({"v": v, "got": got, "want": want})
    return failures


@cocotb.test(skip="boundary.alu" not in SELECTED)
async def boundary_alu(dut):
    cases = []
    for s in range(1, 24):
        half = 1 << (s - 1)
        for k in range(-3, 4):
            cases += [
                (k * (1 << s) + half, s),
                (k * (1 << s) - half, s),
                (k * (1 << s) + half - 1, s),
                (k * (1 << s) + half + 1, s),
            ]
    for v in (32767, 32768, -32768, -32769, 2**31 - 1, 2**31, -(2**31), -(2**31) - 1, 2**40, -(2**40), 0, -1):
        cases += [(v, 0), (v, 3), (v, -2)]
    failures = await check_rsh_sat(dut, cases)
    silu_values = []
    for frac in range(0, 21):
        clamp = 8 * 2**frac
        failures += await check_silu(dut, [clamp, clamp - 1, -clamp, -clamp - 1, 0, -1, 1, 2**20, -(2**20)], frac)
        silu_values.append(frac)
    failures += await check_exp(dut, [0, 1, 31, 32, 16382, 16383, 16384, 2**40])
    rs = [v for k in range(0, 62) for v in (2**k - 1, 2**k, 2**k + 1) if v >= 1]
    failures += await check_rsqrt(dut, rs)
    record("boundary.alu", len(cases) + 9 * 21 + 8 + len(rs), failures)
    assert not failures, failures[:3]


@cocotb.test(skip="exhaustive.silu_index" not in SELECTED)
async def exhaustive_silu_index(dut):
    frac = int(os.environ.get("H1_FRAC", "11"))
    values = list(range(-32768, 32768))
    failures = await check_silu(dut, values, frac)
    failures += await check_silu(dut, values[::64], 2)  # left-shift path
    record("exhaustive.silu_index", len(values) + len(values[::64]), failures)
    assert not failures, failures[:3]


@cocotb.test(skip="exhaustive.exp_index" not in SELECTED)
async def exhaustive_exp_index(dut):
    values = list(range(0, 16383 + 65))
    failures = await check_exp(dut, values)
    record("exhaustive.exp_index", len(values), failures)
    assert not failures, failures[:3]


@cocotb.test(skip="exhaustive.rsqrt" not in SELECTED)
async def exhaustive_rsqrt(dut):
    values = list(range(1, 65537))
    failures = await check_rsqrt(dut, values)
    record("exhaustive.rsqrt", len(values), failures)
    assert not failures, failures[:3]


@cocotb.test(skip="random.alu" not in SELECTED)
async def random_alu(dut):
    rng = random.Random(20261018)
    cases = [(rng.randint(-(2**47), 2**47), rng.randint(-8, 40)) for _ in range(20000)]
    failures = await check_rsh_sat(dut, cases)
    rs = [rng.randint(1, 2**60) for _ in range(5000)]
    failures += await check_rsqrt(dut, rs)
    record("random.alu", len(cases) + len(rs), failures)
    assert not failures, failures[:3]
