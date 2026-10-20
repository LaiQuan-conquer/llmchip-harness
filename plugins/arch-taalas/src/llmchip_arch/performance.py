"""Performance of a design: cycles per token from the microcode the chip will run.

The template compiles the numeric spec into the engine's program (rtl-taalas
`Compiled`), then counts the cycles of every step with the cycle-exact model
`llmchip_rtl.perf`, which equals the RTL simulation (tests/arch-perf.spec.ts).
Nothing here is a fitted constant: the microarchitecture options change the
program and the memory port, and the model counts what they do.
"""

from __future__ import annotations

from llmchip_rtl import perf
from llmchip_rtl.compiler import Compiled

SRAM_SCK_MAX_MHZ = 20.0  # 23LC1024 at 4.5-5.5 V; the port's SCK is clk / 2
KINDS = ("memory", "multiply", "divide", "control")


def options_of(params: dict) -> dict:
    """The microarchitecture options of design parameters, with the operand buffers as a choice
    name (none, attention, all; a boolean means none or all)."""
    buffers = params.get("operandBuffers", "none")
    if buffers is None:
        buffers = "none"
    if isinstance(buffers, bool):
        buffers = "all" if buffers else "none"
    return {"operandBuffers": buffers, "memoryInterface": params.get("memoryInterface", "spi")}


def generation(program: list[int], dparams: dict, context: int, mode: int) -> list[int]:
    """Busy cycles of every position of a generation over the whole context. The cycles grow
    linearly with the position (attention reads one more row); when the first three and the
    last position say so, the rest follow exactly from the line, otherwise each is counted."""
    probe = {p: perf.step_cycles(program, dparams, p, mode).total for p in (0, 1, 2, context - 1) if p < context}
    if context > 3:
        step = probe[1] - probe[0]
        if probe[2] - probe[1] == step and probe[context - 1] == probe[0] + (context - 1) * step:
            return [probe[0] + p * step for p in range(context)]
    return [probe.get(p) or perf.step_cycles(program, dparams, p, mode).total for p in range(context)]


def performance(compiled: Compiled, clock_mhz: float, traces: bool = True) -> dict:
    """Cycles and speed of a compiled design at a clock (no observation traces: production mode),
    with the cycles of the same steps with the observation-point traces the verification uses
    (`traces`; not needed to compare designs)."""
    dparams = compiled.params
    ctx = compiled.context
    plain = generation(compiled.program, dparams, ctx, 0)
    traced = generation(compiled.program, dparams, ctx, perf.MODE_POINTS) if traces else None
    mid = perf.step_cycles(compiled.program, dparams, ctx // 2, 0)
    by_op: dict[str, dict] = {}
    for o in mid.ops:
        if o.total == 0:
            continue
        row = by_op.setdefault(o.op, {"op": o.op, "count": 0, "accesses": 0, **dict.fromkeys(KINDS, 0)})
        row["count"] += 1
        row["accesses"] += o.accesses
        for k in KINDS:
            row[k] += getattr(o, k)
    mean = sum(plain) / ctx
    breakdown = {k: mid.by(k) for k in KINDS}
    memory_share = breakdown["memory"] / max(mid.total, 1)
    return {
        "model": "cycle-exact model of the engine's microcode (llmchip_rtl.perf), equal to the RTL simulation",
        "clockMHz": clock_mhz,
        "cyclesPerToken": mean,
        "cyclesFirst": plain[0],
        "cyclesLast": plain[-1],
        "cyclesPerPosition": plain,
        **({"cyclesPerTokenWithTraces": sum(traced) / ctx, "cyclesWithTracesPerPosition": traced} if traced else {}),
        "breakdownPosition": ctx // 2,
        "breakdown": breakdown,
        "byOp": sorted(by_op.values(), key=lambda r: -sum(r[k] for k in KINDS)),
        "memoryAccessesPerToken": sum(o.accesses for o in mid.ops),
        "msPerToken": mean / (clock_mhz * 1e3),
        "computeMsPerToken": mean / (clock_mhz * 1e3),
        "tokensPerSecond": clock_mhz * 1e6 / mean,
        "tokensPerSecondLast": clock_mhz * 1e6 / plain[-1],
        "bottleneck": "external memory"
        if memory_share > 0.5
        else "multiplier"
        if breakdown["multiply"] > breakdown["memory"]
        else "control",
        "memoryShare": memory_share,
        "clockLimit": {
            "sramSckMHz": clock_mhz / 2,
            "sramSckMaxMHz": SRAM_SCK_MAX_MHZ,
            "maxClockBySramMHz": 2 * SRAM_SCK_MAX_MHZ,
            "logic": "the clock the logic closes at is measured by static timing analysis after layout (S7)",
        },
    }


def buffer_bits(compiled: Compiled) -> int:
    """Flip-flops of the operand buffers: the int16 vector buffer and the attention accumulators."""
    w = compiled.widths
    q = w.get("HSN", 0) * 16 if w.get("OPBUF_Q") else 0
    return w.get("VBN", 0) * 16 + q + w.get("HSN", 0) * w.get("OACCW", 0)
