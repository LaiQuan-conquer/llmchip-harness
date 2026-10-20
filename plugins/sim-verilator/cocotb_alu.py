"""cocotb tests of the arithmetic functions (llmchip_alu) against isa.py.

Selected by the environment: ALU_TESTS (comma-separated test ids), ISA_PATH,
H1_FRAC (fraction bits of h1 in the design), RESULTS (JSON file for counts), and
the design's proven domains: DW (datapath width), RSH_POSITIVE (all shifts are
right shifts), SILU_FRAC_GE4 (fraction bits of h1 >= 4), and the numeric modes of
the design: ROUND_MODE (index into isa.ROUND_MODES), OVERFLOW_WRAP, ACC_NARROW and
ACCB (accumulator width). Each function is tested on the domain the core uses it on.
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
DW = int(os.environ.get("DW", "64"))
RSH_MIN = 1 if os.environ.get("RSH_POSITIVE") == "1" else -8
FRAC_MIN = 4 if os.environ.get("SILU_FRAC_GE4") == "1" else 0
VMAX = 2 ** (DW - 2)  # values of the datapath stay below 2^(DW-1)
ROUNDING = isa.ROUND_MODES[int(os.environ.get("ROUND_MODE", "0"))]
OVERFLOW = "wrap" if os.environ.get("OVERFLOW_WRAP") == "1" else "saturate"
ACCB = int(os.environ["ACCB"]) if os.environ.get("ACC_NARROW") == "1" else None
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
        if not (-VMAX <= v < VMAX and s >= RSH_MIN):
            continue
        await apply(dut, v, s)
        want = (
            isa.rsh(v, s, ROUNDING),
            isa.sat(v, 16, OVERFLOW),
            isa.sat(v, 32, OVERFLOW),
            v if ACCB is None else isa.sat(v, ACCB, OVERFLOW),
        )
        got = (
            dut.rsh.value.to_signed(),
            dut.sat16.value.to_signed(),
            dut.sat32.value.to_signed(),
            dut.acc.value.to_signed(),
        )
        if got != want:
            failures.append({"v": v, "s": s, "got": got, "want": want})
    return failures


async def check_silu(dut, values, frac):
    failures = []
    clamp = 8 * 2**frac
    for h1 in values:
        await apply(dut, h1, frac, clamp, 128)
        want = isa.silu_index(h1, frac, clamp, 128)
        got = dut.silu_index.value.to_unsigned()
        if got != want:
            failures.append({"h1": h1, "frac": frac, "got": got, "want": want})
    return failures


async def check_exp(dut, values, cap=16383, shift=5):
    failures = []
    for d in values:
        await apply(dut, d, shift, cap)
        want = isa.exp_index(d, cap, shift)
        got = dut.exp_index.value.to_unsigned()
        if got != want:
            failures.append({"d": d, "got": got, "want": want})
    return failures


async def check_rsqrt(dut, values):
    failures = []
    for v in values:
        if v >= 2**32:  # the core normalizes 32-bit values
            continue
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
    if ACCB is not None:  # the edges of a narrow accumulator
        cases += [(v, 3) for v in (2 ** (ACCB - 1) - 1, 2 ** (ACCB - 1), -(2 ** (ACCB - 1)), -(2 ** (ACCB - 1)) - 1)]
    failures = await check_rsh_sat(dut, cases)
    silu_values = []
    for frac in range(FRAC_MIN, 16):
        clamp = 8 * 2**frac
        edges = [clamp, clamp - 1, -clamp, -clamp - 1, 0, -1, 1, 32767, -32768]
        failures += await check_silu(dut, [h for h in edges if -32768 <= h <= 32767], frac)  # h1 is int16
        silu_values.append(frac)
    failures += await check_exp(dut, [0, 1, 31, 32, 16382, 16383, 16384, 2**32, 2**33 - 1])
    rs = [v for k in range(0, 33) for v in (2**k - 1, 2**k, 2**k + 1) if 1 <= v < 2**32]
    failures += await check_rsqrt(dut, rs)
    record(
        "boundary.alu",
        sum(1 for v, s in cases if -VMAX <= v < VMAX and s >= RSH_MIN)
        + sum(
            len(
                [
                    h
                    for h in (8 * 2**f, 8 * 2**f - 1, -8 * 2**f, -8 * 2**f - 1, 0, -1, 1, 32767, -32768)
                    if -32768 <= h <= 32767
                ]
            )
            for f in range(FRAC_MIN, 16)
        )
        + 9
        + len(rs),
        failures,
    )
    assert not failures, failures[:3]


@cocotb.test(skip="exhaustive.silu_index" not in SELECTED)
async def exhaustive_silu_index(dut):
    frac = int(os.environ.get("H1_FRAC", "11"))
    values = list(range(-32768, 32768))
    failures = await check_silu(dut, values, frac)
    extra = values[::64] if FRAC_MIN == 0 else []
    failures += await check_silu(dut, extra, 2)  # left-shift path, when the design has one
    record("exhaustive.silu_index", len(values) + len(extra), failures)
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
    cases = [(rng.randint(-VMAX, VMAX - 1), rng.randint(RSH_MIN, min(40, DW - 2))) for _ in range(20000)]
    failures = await check_rsh_sat(dut, cases)
    rs = [rng.randint(1, 2**32 - 1) for _ in range(5000)]
    failures += await check_rsqrt(dut, rs)
    record("random.alu", len(cases) + len(rs), failures)
    assert not failures, failures[:3]
