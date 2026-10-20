"""Cycle-exact performance model of the llmchip vector engine (`rtl/llmchip_core.v`).

The core's cycle count does not depend on the data, only on the program, the
position (attention length) and the step mode. This module walks the core's
state machine state by state, without computing values, and adds the cycles of
the subroutines: the shared multiplier (BW = 18 cycles), the divider (DW cycles)
and the external memory port (`rtl/llmchip_spimem.v`), whose latency depends on
what the port did before (a read that continues an open sequential read, a
fresh frame, the closing states after a write).

`step_cycles` gives the cycles the core is busy for one step (start to done),
split into memory, multiply, divide and control, per instruction. The test bench
counts the same busy cycles in the RTL simulation; tests/arch-perf.spec.ts checks
that both are equal.

Like `isa.py`, this file is self-contained (Python 3.10, standard library) and is
shipped inside every RTL bundle.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BW = 18  # width of the multiplier's second operand: S_MUL takes BW cycles
FIELDS = 16
OP_NAMES = {
    0: "END",
    1: "EMB",
    2: "MATVEC",
    3: "RMS",
    4: "ROPE",
    5: "ATTN",
    6: "SILU",
    7: "ADD",
    8: "TRACE",
    9: "ARGMAX",
}
FLAG_DEBUG = 4
FLAG_VB = 4  # MATVEC: the input is already in the vector buffer (operand buffers only)
MODE_POINTS = 1
MODE_DEBUG = 2


def decode(word: int) -> tuple[str, list[int]]:
    fields = []
    for i in range(FIELDS):
        f = (word >> (8 + 32 * i)) & 0xFFFFFFFF
        fields.append(f - (1 << 32) if f >= 1 << 31 else f)
    return OP_NAMES[word & 0xFF], fields


class SpiPort:
    """Latency of one 32-bit access through the SPI master (SCK = clk / 2, 64-bit frames).

    Times are clock cycles of the core. A request made by the core in cycle `c` (the
    state that issues LOAD/STORE) is seen by the port at the end of cycle c + 1; the
    core waits in S_LOAD/S_STORE until the port acknowledges.
    """

    FRESH_READ = 128  # 64 bits x 2 cycles, from the edge that accepts the request to the acknowledge
    FRESH_WRITE = 128
    CONTINUE = 64  # 32 data bits of a continued sequential read

    def __init__(self) -> None:
        self.mode = "idle"  # idle | open (sequential read open) | end (closing after a write)
        self.last = -1
        self.ready = 0  # first edge at which an idle port accepts a request

    def access(self, c: int, addr: int, write: bool) -> int:
        """Cycles the core spends waiting for an access requested in cycle c."""
        fresh = self.FRESH_WRITE if write else self.FRESH_READ
        if self.mode == "open" and not write and addr == self.last + 1:
            start, bits = c + 1, self.CONTINUE
        elif self.mode == "open":
            start, bits = c + 4, fresh  # OPEN -> END (SCK low) -> IDLE -> accept
        else:
            start, bits = max(c + 1, self.ready), fresh
        ack = start + bits
        self.last = addr
        if write:
            self.mode, self.ready = "end", ack + 3  # SCK low, CS high, then idle
        else:
            self.mode = "open"
        return ack - c


class SqiPort(SpiPort):
    """The quad SPI (SQI) master: 4 data lines, so a command (8 bits), an address (24 bits)
    and a word (32 bits) take 2, 6 and 8 SCK cycles; a read waits 2 dummy SCK cycles
    (23LC1024 in SQI mode). SCK = clk / 2, as in SPI."""

    FRESH_READ = 2 * (2 + 6 + 2 + 8)
    FRESH_WRITE = 2 * (2 + 6 + 8)
    CONTINUE = 2 * 8


def port_for(params: dict) -> SpiPort:
    return SqiPort() if params.get("MEM_SQI") else SpiPort()


@dataclass
class OpCycles:
    index: int
    op: str
    memory: int = 0
    multiply: int = 0
    divide: int = 0
    control: int = 0
    accesses: int = 0

    @property
    def total(self) -> int:
        return self.memory + self.multiply + self.divide + self.control


@dataclass
class StepCycles:
    pos: int
    total: int = 0
    ops: list[OpCycles] = field(default_factory=list)

    def by(self, kind: str) -> int:
        return sum(getattr(o, kind) for o in self.ops)


class Core:
    """The memory port and the counters of one step. `port` gives the latency of one data access
    (the SPI master today; other memory interfaces implement the same `access`)."""

    def __init__(self, dw: int, ctx: int, lanes: int, port=None) -> None:
        self.dw, self.ctx, self.lanes = dw, ctx, lanes
        self.port = port or SpiPort()
        self.cyc = 0
        self.cur: OpCycles | None = None

    # One plain state: one cycle of control.
    def state(self, n: int = 1) -> None:
        self.cyc += n
        self.cur.control += n

    def load(self, addr: int) -> None:
        self.mem(addr, False)

    def store(self, addr: int) -> None:
        self.mem(addr, True)

    def mem(self, addr: int, write: bool) -> None:
        c = self.cyc
        self.state()  # the state that issues the access
        wait = self.port.access(c, addr, write)
        self.cyc += wait
        self.cur.memory += wait
        self.cur.accesses += 1

    def mul(self, n: int = 1) -> None:
        self.state(n)
        self.cyc += n * BW
        self.cur.multiply += n * BW

    def more_reads(self, n: int, sequential: bool, last: int) -> None:
        """n further reads right after a read, with no other access in between: each continues
        the open sequential transfer (`sequential`), or each closes it and starts a fresh frame
        (alternating, non-consecutive addresses). `last` is the address of the final read.
        The same cycles as n calls of `load`, in O(1)."""
        if n <= 0:
            return
        p = self.port
        wait = p.CONTINUE + 1 if sequential else p.FRESH_READ + 4
        self.state(n)
        self.cyc += n * wait
        self.cur.memory += n * wait
        self.cur.accesses += n
        p.mode, p.last = "open", last

    def div(self) -> None:
        self.state()
        self.cyc += self.dw
        self.cur.divide += self.dw


def step_cycles(
    program: list[int], params: dict, pos: int, mode: int = MODE_POINTS, port=None, fast: bool = True
) -> StepCycles:
    """Busy cycles of one step at position `pos` (start to done), per instruction.

    `params` are the design parameters of the bundle (LANES, DW, CTX); `mode` is the step
    mode (bit 0: observation-point traces, bit 1: debug traces). `fast` counts the long
    regular loops (rows of a MATVEC, rows of K and V) in closed form; `fast=False` walks
    every state (the reference the tests compare it with)."""
    L, ctx = int(params["LANES"]), int(params["CTX"])
    k = Core(int(params["DW"]), ctx, L, port or port_for(params))
    # Operand buffer parts (design parameters of the bundle).
    k.buf_vec = bool(params.get("OPBUF_VEC"))  # MATVEC input and RMS vector
    k.buf_q = bool(params.get("OPBUF_Q"))  # attention: q of the head
    k.buf_acc = bool(params.get("OPBUF_ACC"))  # attention: output accumulators
    k.fast = fast
    out = StepCycles(pos)
    for index, word in enumerate(program):
        op, f = decode(int(word))
        k.cur = OpCycles(index, op)
        out.ops.append(k.cur)
        if op == "END":
            k.state()  # S_FETCH clears busy at the end of its cycle
            break
        _op(k, op, f, pos, mode)
        k.state()  # S_NEXT
    out.total = k.cyc
    return out


def _op(k: Core, op: str, f: list[int], pos: int, mode: int) -> None:
    L = k.lanes
    if op == "EMB":
        dst, dim = f[0], f[1]
        k.mul()  # S_FETCH: token row base
        k.mul()  # S_EMB_0: scale row base
        k.state()  # S_EMB_1
        for i in range(dim):
            k.store(dst + i)  # S_EMB
            k.state()  # S_EMB_N
    elif op == "MATVEC":
        x, dst, inp, out, group, flags, stride = f[0], f[1], f[2], f[3], f[6], f[9], f[10]
        k.mul()  # S_FETCH: position offset
        k.state()  # S_MV_0
        base = dst + (pos * stride if flags & 2 else 0)
        obase = 0
        while True:
            k.state()  # S_MV_BLK
            i = 0
            # With operand buffers, the input is read from memory only by the first block
            # (and not at all when it is already in the vector buffer).
            buffered = k.buf_vec and (obase > 0 or flags & FLAG_VB)
            if k.fast:
                # The block in closed form: inp reads (from the buffer, or one transfer then
                # sequential continuations) and an S_MV_ACC each; per group, L scalings.
                if buffered:
                    k.state(inp)
                else:
                    k.load(x)
                    k.more_reads(inp - 1, True, x + inp - 1)
                k.state(inp)
                groups = -(-inp // group)
                k.mul(groups * L)
                k.state(groups * L)
            while not k.fast:
                gi = 0
                while True:
                    if buffered:
                        k.state()  # S_MV_X: from the vector buffer
                    else:
                        k.load(x + i)  # S_MV_X
                    k.state()  # S_MV_ACC
                    if gi + 1 == group or i + 1 == inp:
                        break
                    i, gi = i + 1, gi + 1
                for _ in range(L):
                    k.mul()  # S_MV_SC
                    k.state()  # S_MV_SC2
                if i + 1 == inp:
                    break
                i += 1
            for lane in range(L + 1):  # S_MV_W
                if lane < L and obase + lane < out:
                    k.store(base + obase + lane)
                else:
                    k.state()
            if obase + L >= out:
                break
            obase += L
    elif op == "RMS":
        x, dst, n = f[0], f[1], f[2]
        k.state()  # S_FETCH
        for i in range(n):
            k.load(x + i)  # S_RMS_SUM
            k.mul()  # S_RMS_SQ
            if i + 1 == n:
                k.div()  # S_RMS_ACC
            else:
                k.state()
        k.state()  # S_RMS_NORM
        for i in range(n):
            if k.buf_vec:
                k.state()  # S_RMS_X: from the vector buffer
            else:
                k.load(x + i)  # S_RMS_X
            k.mul()  # S_RMS_M1
            k.mul()  # S_RMS_M2
            k.store(dst + i)  # S_RMS_W
    elif op == "ROPE":
        addr, n, flags, stride = f[0], f[1], f[5], f[6]
        k.mul()  # S_FETCH: position offset
        k.mul()  # S_RO_0: table row
        k.state()  # S_RO_1
        base = addr + (pos * stride if flags & 2 else 0)
        i = 0
        while True:
            k.load(base + i)  # S_RO_A
            k.load(base + i + 1)  # S_RO_B
            k.mul()  # S_RO_C
            k.mul()  # S_RO_S
            k.store(base + i)  # S_RO_W1
            k.mul()  # S_RO_M3
            k.mul()  # S_RO_M4
            k.store(base + i + 1)  # S_RO_W2
            k.state()  # S_RO_N
            if i + 2 >= n:
                break
            i += 2
    elif op == "ATTN":
        q, kbase, vbase, dst, heads, kv_mul, hs, scratch, kv_dim = (f[i] for i in (0, 1, 2, 3, 4, 5, 6, 10, 11))
        ctx = k.ctx
        k.state()  # S_FETCH
        hoff = kvh = hc = 0
        for _ in range(heads):
            k.state()  # S_AT_HEAD
            kaddr = kbase + kvh
            if k.buf_q:
                if k.fast:
                    k.load(q + hoff)  # S_AT_QL, then the rest of q in sequence
                    k.more_reads(hs - 1, True, q + hoff + hs - 1)
                    k.state(hs)  # S_AT_QW
                else:
                    for j in range(hs):
                        k.load(q + hoff + j)  # S_AT_QL: q of the head into the vector buffer
                        k.state()  # S_AT_QW
            for t in range(pos + 1):
                if k.fast:
                    # The row of K (and q, without buffers): hs (2 hs) reads, hs products,
                    # hs - 1 S_AT_DOT states and the scaling product.
                    if k.buf_q:
                        k.load(kaddr)
                        k.more_reads(hs - 1, True, kaddr + hs - 1)
                    else:
                        k.load(q + hoff)
                        k.more_reads(2 * hs - 1, False, kaddr + hs - 1)
                    k.mul(hs + 1)
                    k.state(hs - 1)
                    k.store(scratch + t)  # S_AT_SC
                    k.state()  # S_AT_SN
                    kaddr += kv_dim
                    continue
                for j in range(hs):
                    if not k.buf_q:
                        k.load(q + hoff + j)  # S_AT_Q
                    k.load(kaddr + j)  # S_AT_K
                    k.mul()  # S_AT_QK
                    if j + 1 == hs:
                        k.mul()  # S_AT_DOT: scale
                    else:
                        k.state()
                k.store(scratch + t)  # S_AT_SC
                k.state()  # S_AT_SN
                kaddr += kv_dim
            for t in range(pos + 1):
                k.load(scratch + t)  # S_AT_EL0
                k.store(scratch + ctx + t)  # S_AT_EL
                k.state()  # S_AT_EN
            for t in range(pos + 1):
                k.load(scratch + ctx + t)  # S_AT_PL
                k.div()  # S_AT_PD
                k.store(scratch + ctx + t)  # S_AT_PW
                k.state()  # S_AT_PN
            vrow = vbase + kvh
            if k.buf_acc:
                # Position outer, column inner: one probability per position, the V row in
                # sequence, one accumulator per column; then the head's outputs are stored.
                kaddr = vrow
                for t in range(pos + 1):
                    k.load(scratch + ctx + t)  # S_AT_OP
                    if k.fast:
                        k.load(kaddr)
                        k.more_reads(hs - 1, True, kaddr + hs - 1)
                        k.mul(hs)
                        k.state(hs)
                    else:
                        for j in range(hs):
                            k.load(kaddr + j)  # S_AT_OV
                            k.mul()  # S_AT_OM
                            k.state()  # S_AT_OA
                    kaddr += kv_dim
                for j in range(hs):
                    k.store(dst + hoff + j)  # S_AT_OS
                    k.state()  # S_AT_ON
            for j in range(hs if not k.buf_acc else 0):
                kaddr = vrow + j
                if k.fast:
                    # p[t] and V[t][j] alternate: every read after the first starts a fresh frame.
                    k.load(scratch + ctx)
                    k.more_reads(2 * (pos + 1) - 1, False, kaddr + pos * kv_dim)
                    k.mul(pos + 1)
                    k.state(pos)  # S_AT_OA for t < pos
                    k.store(dst + hoff + j)  # S_AT_OA at t = pos
                    k.state()  # S_AT_ON
                    continue
                for t in range(pos + 1):
                    k.load(scratch + ctx + t)  # S_AT_OP
                    k.load(kaddr)  # S_AT_OV
                    k.mul()  # S_AT_OM
                    if t == pos:
                        k.store(dst + hoff + j)  # S_AT_OA
                    else:
                        k.state()
                        kaddr += kv_dim
                k.state()  # S_AT_ON
            hoff += hs
            if hc + 1 == kv_mul:
                hc, kvh = 0, kvh + hs
            else:
                hc += 1
        k.state()  # S_AT_HEAD: all heads done
    elif op == "SILU":
        h1, h3, dst, n = f[0], f[1], f[2], f[3]
        k.state()  # S_FETCH
        for i in range(n):
            k.load(h1 + i)  # S_SI_A
            k.load(h3 + i)  # S_SI_B
            k.mul()  # S_SI_C
            k.mul()  # S_SI_D
            k.store(dst + i)  # S_SI_W
        return
    elif op == "ADD":
        x, y, n = f[0], f[1], f[2]
        k.state()  # S_FETCH
        for i in range(n):
            k.load(x + i)  # S_ADD_A
            k.load(y + i)  # S_ADD_B
            k.store(x + i)  # S_ADD_W
        return
    elif op == "TRACE":
        addr, n, flags, stride, buffer = f[0], f[1], f[4], f[5], f[6]
        if not mode & (MODE_DEBUG if flags & FLAG_DEBUG else MODE_POINTS):
            k.state()  # S_FETCH
            return
        k.mul()  # S_FETCH: position offset
        k.state()  # S_TR_0
        base = addr + (pos * stride if flags & 2 else 0)
        for i in range(n):
            k.load(base + i)  # S_TR_L
            k.store(buffer + i)  # S_TR_W
        return
    elif op == "ARGMAX":
        addr, n = f[0], f[1]
        k.state()  # S_FETCH
        for i in range(n):
            k.load(addr + i)  # S_AM_L
            k.state()  # S_AM_C
        return
    else:
        raise ValueError(f"unknown op {op}")
