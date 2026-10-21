"""cocotb tests of the table-index units of a spatial bundle (sp_units.v) against the numeric
spec's rules (floors): the rsqrt index and exponent of rmsnorm, the exp index of softmax and
the sigmoid index of SiLU.

Selected by the environment: UNIT_TESTS (comma-separated test ids), UNIT (the module under
test), the design's constants DIM, EPS_Q, SQ_BITS, EXP_CAP, EXP_SHIFT, F_H1, SIG_LIMIT,
SIG_OFFSET, and RESULTS (JSON file for counts).
"""

import json
import os
import random

import cocotb
from cocotb.triggers import Timer

SELECTED = set(os.environ.get("UNIT_TESTS", "").split(","))
UNIT = os.environ["UNIT"]
P = {
    k: int(os.environ[k])
    for k in ("DIM", "EPS_Q", "SQ_BITS", "EXP_CAP", "EXP_SHIFT", "F_H1", "SIG_LIMIT", "SIG_OFFSET")
}


def record(name: str, checked: int, failures: list) -> None:
    path = os.environ.get("RESULTS")
    if not path:
        return
    existing = {}
    if os.path.exists(path):
        with open(path) as f:
            existing = json.load(f)
    existing[name] = {"vectors": checked, "failures": len(failures), "examples": failures[:5]}
    with open(path, "w") as f:
        json.dump(existing, f, indent=2)


def value(signal) -> int:
    v = signal.value
    return v.to_unsigned() if hasattr(v, "to_unsigned") else v.integer


def rsqrt_index(sumsq: int) -> tuple[int, int]:
    v = max(sumsq // P["DIM"] + P["EPS_Q"], 1)
    k = v.bit_length() - 1
    e2 = k - (k % 2)
    t = v >> (e2 - 6) if e2 >= 6 else v << (6 - e2)
    return t - 64, e2


@cocotb.test(skip="exhaustive.rsqrt" not in SELECTED or UNIT != "sp_rms_index")
async def exhaustive_rsqrt(dut):
    width = P["SQ_BITS"]
    values = list(range(0, 2**16 * P["DIM"] + 1, max(P["DIM"] // 4, 1)))
    for j in range(1, width - 1):
        values += [2**j - 1, 2**j, 2**j + 1]
    values += [2 ** (width - 1) - 1]
    failures = []
    for s in values:
        dut.sumsq.value = s
        await Timer(1, "ns")
        got = (value(dut.a), value(dut.e))
        want = rsqrt_index(s)
        if got != want:
            failures.append({"sumsq": s, "rtl": got, "expected": want})
    record("exhaustive.rsqrt", len(values), failures)


@cocotb.test(skip="exhaustive.exp_index" not in SELECTED or UNIT != "sp_exp_index")
async def exhaustive_exp_index(dut):
    rng = random.Random(5)
    cap, shift = P["EXP_CAP"], P["EXP_SHIFT"]
    diffs = [*range(0, cap + 64), cap * 4, 2**20, 2**31 - 1, 2**32 - 1]
    failures = []
    for d in diffs:
        mx = rng.randrange(-(2**31) + d, 2**31) if d < 2**31 else 2**31 - 1
        sc = mx - d
        if not -(2**31) <= sc < 2**31:
            mx, sc = 2**31 - 1, 2**31 - 1 - d if d < 2**32 else -(2**31)
        dut.mx.value = mx
        dut.sc.value = sc
        await Timer(1, "ns")
        want = min(mx - sc, cap) >> shift
        got = value(dut.a)
        if got != want:
            failures.append({"mx": mx, "sc": sc, "rtl": got, "expected": want})
    record("exhaustive.exp_index", len(diffs), failures)


@cocotb.test(skip="exhaustive.silu_index" not in SELECTED or UNIT != "sp_silu_index")
async def exhaustive_silu_index(dut):
    fh, lim = P["F_H1"], P["SIG_LIMIT"]
    failures = []
    for h1 in range(-(2**15), 2**15):
        dut.h1.value = h1
        await Timer(1, "ns")
        z = min(max(h1, -lim), lim - 1)
        want = (z >> (fh - 4) if fh >= 4 else z << (4 - fh)) + P["SIG_OFFSET"]
        got = value(dut.a)
        if got != want:
            failures.append({"h1": h1, "rtl": got, "expected": want})
    record("exhaustive.silu_index", 2**16, failures)
