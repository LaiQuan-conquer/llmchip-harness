"""Generator of the spatial engine (architecture `taalas-spatial/1`).

The model is not run by a program: it is built as circuits. For every matrix of
the frozen numeric spec, each output row becomes a bit-serial adder tree whose
wiring is the row's ternary weights (+1: add the input, -1: subtract it, 0: no
wire) and whose group scale mantissa is a fixed sum of shifted copies (its CSD
digits). Every layer has its own trees; a token flows through them in order.
Attention and the KV cache are runtime data: a shared attention unit with 16
multipliers, exact dividers and on-chip KV rings that rotate one row per clock.

The arithmetic is that of the numeric spec (L2) exactly: every sum is an exact
integer sum (order does not matter), every division exact, every rounding shift
and narrowing the spec's (`llmchip_numeric.arith`). The cycle count does not depend
on the data or the position (`Spatial.schedule`, checked against the RTL).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from llmchip_numeric import arith
from llmchip_numeric.spec import EXP_RANGE, SCORE_FRAC, SIG_RANGE, SIG_STEPS

FORMAT = "llmchip-rtl-bundle/3"
ARCHITECTURE = "taalas-spatial/1"
ROUND = {"half-up": 0, "half-even": 1, "truncate": 2}


def csd(m: int) -> list[tuple[int, int]]:
    """Canonical signed digits of a positive integer: [(shift, +1|-1)], fewest nonzero digits."""
    out = []
    j = 0
    while m:
        if m & 1:
            d = 2 - (m & 3)  # +1 when m = ...01, -1 when m = ...11
            out.append((j, d))
            m -= d
        m >>= 1
        j += 1
    return out


@dataclass
class Matrix:
    """One matrix as circuits: its rows, group size, scale, rounding shift and output width."""

    name: str
    q: np.ndarray  # (out, in) ternary levels
    m: np.ndarray  # (out, groups) mantissas
    group: int
    shift: int  # rounding shift of the result (f_in + e - f_out)
    width: int  # 16 or 32
    bound: int = 0  # largest |total| of a row
    length: int = 0  # bits the serial stream must run
    adders: int = 0
    delays: int = 0

    def __post_init__(self) -> None:
        out, _ = self.q.shape
        worst = 0
        for o in range(out):
            total = 0
            for g in range(self.m.shape[1]):
                mag = int(np.abs(self.q[o, g * self.group : (g + 1) * self.group]).sum())
                total += int(self.m[o, g]) * mag * 2**15
            worst = max(worst, total)
        self.bound = worst
        total_bits = max(worst, 1).bit_length() + 1
        # The stream must show the rounded W bits and then the true sign beyond them.
        self.length = max(total_bits, self.shift + self.width) + 2


@dataclass
class Spatial:
    spec: dict
    arrays: dict
    cfg: dict = field(init=False)
    matrices: dict = field(init=False)

    def __post_init__(self) -> None:
        s = self.spec
        if s["accumulator"].get("canOverflow"):
            raise ValueError("the spatial engine sums exactly: a narrow accumulator is not supported")
        c = s["model"]["config"]
        self.cfg = c
        self.dim, self.hidden, self.layers = c["dim"], c["hidden_dim"], c["n_layers"]
        self.heads, self.kv_heads, self.vocab = c["n_heads"], c["n_kv_heads"], c["vocab_size"]
        self.hs = self.dim // self.heads
        self.kv_dim = self.kv_heads * self.hs
        self.ctx = s["context"]["maxTokens"]
        if self.dim & (self.dim - 1) or self.kv_heads != 1 or self.heads != 2 or self.ctx != 64:
            raise ValueError("taalas-spatial/1 implements dim a power of two, 2 heads, 1 KV head and a context of 64")
        self.group = s["weights"]["groupSize"]
        self.f = s["activations"]["frac"]
        self.fn = s["normWeights"]["frac"]
        self.fl = s["logits"]["frac"]
        self.exp = s["weights"]["exponents"]
        self.rounding, self.overflow = arith.modes_of(s)
        self.score_frac = s["scores"]["frac"]
        self.att_scale = round(2**15 / math.sqrt(self.hs))
        self.eps_q = round(c["norm_eps"] * 2.0 ** (2 * self.f["x"]))
        f = self.f
        a = self.arrays
        mats: dict[str, Matrix] = {}
        for L in range(self.layers):
            for name, fi, fo in (
                ("wq", "xn", "q"),
                ("wk", "xn", "k"),
                ("wv", "xn", "v"),
                ("wo", "v", "x"),
                ("w1", "xn", "h1"),
                ("w3", "xn", "h3"),
                ("w2", "g", "x"),
            ):
                key = f"{L}.{name}"
                mats[key] = Matrix(key, a[f"{key}.q"], a[f"{key}.m"], self.group, f[fi] + self.exp[key] - f[fo], 16)
        cls = "emb" if c["shared_classifier"] else "cls"
        mats["cls"] = Matrix("cls", a[f"{cls}.q"], a[f"{cls}.m"], self.group, f["xn"] + self.exp[cls] - self.fl, 32)
        for mtx in mats.values():
            if mtx.shift < 1:
                raise ValueError(f"{mtx.name}: a rounding shift below 1 is not supported")
        self.matrices = mats
        self.input_delays: dict[str, int] = {}  # per matvec module: delay bits of its input taps
        if self.hs < 4:
            raise ValueError("taalas-spatial/1 needs a head size of at least 4")
        if 2 * self.layers * self.dim + self.vocab + 1 > 2048:
            raise ValueError("the observation words exceed the 11-bit READ address of the host interface")
        if self.vocab > 2**16 or self.hidden > 255 or self.dim > 255:
            raise ValueError("taalas-spatial/1 sequences at most 255 elements per stage and 2^16 tokens")
        # Width of the sum of squares of the rmsnorm: dim values of at most 2^30 each.
        self.sq_bits = max(38, (self.dim * 2**30).bit_length() + 1)
        self.dividers = 16
        self.div_rounds = -(-self.heads * self.ctx // self.dividers)
        if self.div_rounds * self.dividers != self.heads * self.ctx or self.div_rounds & (self.div_rounds - 1):
            raise ValueError("the dividers must split the probabilities into a power-of-two number of rounds")
        # The embedding table, computed from the frozen integer weights exactly as L2 does.
        q, m = a["emb.q"].astype(np.int64), a["emb.m"].astype(np.int64)
        idx = np.arange(self.dim) // self.group
        x = arith.rshift(q * m[:, idx], self.exp["emb"] - f["x"], self.rounding)
        self.emb_table = arith.overflow(x, 16, self.overflow)

    # -- cycles ------------------------------------------------------------------------

    def schedule(self) -> list[tuple[str, int]]:
        """(stage, cycles) of one step, in order; a fixed count per token (the KV passes always
        visit all rows of the ring). The state machine of `sp_core.v` is generated from this list,
        one state per stage, so the RTL takes exactly these cycles (checked by simulation)."""
        mv = lambda *names: max(self.matrices[n].length for n in names) + 2  # noqa: E731  start, stream, result
        rms = 2 * self.dim + 1
        C = self.ctx
        out = [("embedding", 1)]
        for L in range(self.layers):
            out += [
                (f"layer {L} rmsnorm (attention)", rms),
                (f"layer {L} q, k, v", mv(f"{L}.wq", f"{L}.wk", f"{L}.wv")),
                (f"layer {L} rope", self.dim + self.kv_dim),
                (f"layer {L} attention: scores and maximum", C),
                (f"layer {L} attention: exp and sum", C),
                (f"layer {L} attention: divisions", self.div_rounds * 17),
                (f"layer {L} attention: weighted sum of v", C + 1),
                (f"layer {L} wo", mv(f"{L}.wo")),
                (f"layer {L} residual (attention)", 1),
                (f"layer {L} rmsnorm (ffn)", rms),
                (f"layer {L} w1, w3", mv(f"{L}.w1", f"{L}.w3")),
                (f"layer {L} silu gate", self.hidden),
                (f"layer {L} w2", mv(f"{L}.w2")),
                (f"layer {L} residual (ffn)", 1),
            ]
        out += [("final rmsnorm", rms), ("classifier", mv("cls")), ("argmax", self.vocab + 1)]
        return out

    def cycles_per_token(self) -> int:
        """Busy cycles of a step: the stages, plus the cycle that ends the step."""
        return sum(c for _, c in self.schedule()) + 1

    # -- circuits ----------------------------------------------------------------------

    def matvec_verilog(self, module: str, names: list[str], inputs: int) -> str:
        """One module per input vector: the trees of every matrix that reads it (one PISO bank)."""
        mats = [self.matrices[n] for n in names]
        length = max(m.length for m in mats)
        kw = max(length.bit_length(), 2)
        rnd = ROUND[self.rounding]
        wrap = int(self.overflow == "wrap")
        lines = [
            "// Generated by rtl-taalas (spatial) from the numeric weights; do not edit.",
            f"// {', '.join(names)}: each output row is a bit-serial adder tree wired from its integer",
            "// weights (+1 adds an input, -1 subtracts it, 0 is no wire; larger levels add shifted",
            "// copies); each group's scale mantissa is a fixed sum of shifted copies (CSD digits).",
            f"module {module} (",
            "  input  wire clk,",
            "  input  wire rst_n,",
            "  input  wire start,",
            f"  input  wire [{inputs - 1}:0] xs,  // the input vector, bit-serial (bit k at k), from the core",
            *[f"  output wire [{m.q.shape[0] * m.width - 1}:0] y_{m.name.split('.')[-1]}," for m in mats],
            "  output reg  done",
            ");",
            f"  reg [{kw - 1}:0] k;",
            "  reg run;",
            "  wire first = run && k == 0;",
            "  wire clr = start || !rst_n;  // carries and delay lines start a run empty",
            f"  wire last = run && k == {length - 1};",
            "  always @(posedge clk) begin",
            "    if (!rst_n) begin run <= 1'b0; k <= 0; done <= 1'b0; end",
            "    else begin",
            "      done <= 1'b0;",
            "      if (start) begin run <= 1'b1; k <= 0; end",
            f"      else if (run) begin if (k == {length - 1}) begin run <= 1'b0; done <= 1'b1; end "
            f"else k <= k + 1'b1; end",
            "    end",
            "  end",
        ]
        # Integer weights beyond +-1 are sums of shifted inputs (the CSD digits of |q|); the shifted
        # copies of an input come from one tapped delay line per input, shared by every row.
        taps_of = [0] * inputs
        for mtx in mats:
            for i in range(mtx.q.shape[1]):
                for v in np.unique(np.abs(mtx.q[:, i])):
                    if v:
                        taps_of[i] = max(taps_of[i], max(j for j, _ in csd(int(v))))
        for i, n in enumerate(taps_of):
            if n:
                lines.append(f"  wire [{n - 1}:0] xt{i};")
                lines.append(f"  sp_taps #(.N({n})) t{i} (.clk(clk), .clr(clr), .a(xs[{i}]), .t(xt{i}));")

        self.input_delays[module] = sum(taps_of)

        def weight_terms(q: int, i: int) -> list[tuple[str, int]]:
            """(stream, sign) whose sum is q * x_i."""
            return [(f"xs[{i}]" if j == 0 else f"xt{i}[{j - 1}]", d if q > 0 else -d) for j, d in csd(abs(int(q)))]

        counter = {"n": 0, "adders": 0}

        def wire() -> str:
            counter["n"] += 1
            return f"w{counter['n']}"

        def add(a: str, b: str, sub: bool = False) -> str:
            counter["adders"] += 1
            w = wire()
            lines.append(f"  wire {w};")
            lines.append(f"  sp_sadd #(.SUB({int(sub)})) a{w} (.clk(clk), .clr(clr), .a({a}), .b({b}), .s({w}));")
            return w

        def tree(terms: list[str]) -> str | None:
            if not terms:
                return None
            while len(terms) > 1:
                terms = [
                    add(terms[i], terms[i + 1]) if i + 1 < len(terms) else terms[i] for i in range(0, len(terms), 2)
                ]
            return terms[0]

        def signed(pos: list[str], neg: list[str]) -> str:
            p, n = tree(pos), tree(neg)
            if p is None and n is None:
                return "1'b0"
            if n is None:
                return p
            return add(p if p is not None else "1'b0", n, sub=True)

        for mtx in mats:
            short = mtx.name.split(".")[-1]
            out, inp = mtx.q.shape
            counter["adders"] = 0
            mtx.delays = 0
            # The phases of the output stream, shared by the rows: below the half bit (sticky), the
            # half bit, the rounding carry, the W result bits, and the bits above them (overflow).
            ph = f"p_{short}"
            S, W = mtx.shift, mtx.width
            lines += [
                f"  wire {ph}_lo = run && k + 1 < {S};",
                f"  wire {ph}_hb = run && k + 1 == {S};",
                f"  wire {ph}_rb = run && k == {S};",
                f"  wire {ph}_col = run && k >= {S} && k < {S + W};",
                f"  wire {ph}_hi = run && k >= {S + W};",
            ]
            lines.append(f"  // ---- {mtx.name}: {out} rows, rounding shift {mtx.shift}, {mtx.width}-bit results")
            for o in range(out):
                pos_terms, neg_terms = [], []
                for g in range(mtx.m.shape[1]):
                    cols = range(g * self.group, min((g + 1) * self.group, inp))
                    terms = [t for i in cols if mtx.q[o, i] for t in weight_terms(mtx.q[o, i], i)]
                    gp = [t for t, d in terms if d > 0]
                    gn = [t for t, d in terms if d < 0]
                    if not gp and not gn:
                        continue
                    gsum = signed(gp, gn)
                    # The scale: the group sum shifted by each CSD digit of the mantissa (a shift is a
                    # delay: one tapped delay line per group).
                    digits = csd(int(mtx.m[o, g]))
                    top = max(j for j, _ in digits)
                    if top:
                        taps = wire()
                        lines.append(f"  wire [{top - 1}:0] {taps};")
                        lines.append(f"  sp_taps #(.N({top})) d{taps} (.clk(clk), .clr(clr), .a({gsum}), .t({taps}));")
                        mtx.delays += top
                    for j, d in digits:
                        term = gsum if j == 0 else f"{taps}[{j - 1}]"
                        (pos_terms if d > 0 else neg_terms).append(term)
                total = signed(pos_terms, neg_terms)
                lines.append(
                    f"  sp_out #(.W({mtx.width}), .ROUND({rnd}), .WRAP({wrap})) o_{short}_{o} "
                    f"(.clk(clk), .rst_n(rst_n), .first(first), .lo({ph}_lo), .hb({ph}_hb), "
                    f".rb({ph}_rb), .col({ph}_col), "
                    f".hi({ph}_hi), .last(last), .a({total}), .y(y_{short}[{o * mtx.width} +: {mtx.width}]));"
                )
            mtx.adders = counter["adders"]
        lines.append("endmodule")
        return "\n".join(lines) + "\n"

    # -- the core ----------------------------------------------------------------------

    def modules(self) -> list[tuple[str, list[str], str, int]]:
        """(module, matrices, input vector, inputs): one matvec module per input vector and layer."""
        out = []
        for li in range(self.layers):
            out += [
                (f"sp_mv_l{li}_qkv", [f"{li}.wq", f"{li}.wk", f"{li}.wv"], "xn", self.dim),
                (f"sp_mv_l{li}_o", [f"{li}.wo"], "att", self.dim),
                (f"sp_mv_l{li}_w13", [f"{li}.w1", f"{li}.w3"], "xn", self.dim),
                (f"sp_mv_l{li}_w2", [f"{li}.w2"], "gg", self.hidden),
            ]
        out.append(("sp_mv_cls", ["cls"], "xn", self.dim))
        return out

    @staticmethod
    def rom(name: str, values, width: int) -> str:
        """A constant table as its own module (a case statement: logic, not storage)."""
        values = [int(v) for v in values]
        aw = max((len(values) - 1).bit_length(), 1)
        lines = [
            f"module sp_rom_{name} (",
            f"  input  wire [{aw - 1}:0] a,",
            f"  output reg  [{width - 1}:0] d",
            ");",
            "  always @* begin",
            "    case (a)",
        ]
        for i, v in enumerate(values):
            lines.append(f"      {aw}'d{i}: d = {width}'h{v % 2**width:x};")
        lines += [f"      default: d = {width}'h0;", "    endcase", "  end", "endmodule"]
        return "\n".join(lines) + "\n"

    def roms_verilog(self) -> str:
        """The tables of the numeric spec and the embedding (one row of D values per token)."""
        a = self.arrays
        D, L = self.dim, self.layers
        norms = [f"{li}.{n}" for li in range(L) for n in ("rms_att", "rms_ffn")] + ["rms_final"]
        emb_rows = [sum((int(v) % 2**16) << (16 * i) for i, v in enumerate(row)) for row in self.emb_table]
        return "\n".join(
            [
                "// Generated by rtl-taalas (spatial) from the numeric spec; do not edit.",
                "// The tables of the numeric spec as logic; the embedding gives a whole row per token.",
                self.rom("emb", emb_rows, 16 * D),
                self.rom("rsqrt", a["lut.rsqrt"], 17),
                self.rom("exp", a["lut.exp"], 17),
                self.rom("sig", a["lut.sigmoid"], 17),
                self.rom("cos", a["lut.rope_cos"].ravel(), 16),
                self.rom("sin", a["lut.rope_sin"].ravel(), 16),
                self.rom("norm", [v for k in norms for v in a[k]], 16),
            ]
        )

    @staticmethod
    def _rs(expr: str, s: int) -> str:
        """The spec's rounding shift by a constant (a negative constant shifts left exactly)."""
        if s >= 1:
            return f"rsh({expr}, 7'd{s})"
        if s == 0:
            return f"({expr})"
        return f"(({expr}) <<< {-s})"

    def core_verilog(self) -> str:
        D, H, L, HS, KD, V, C = self.dim, self.hidden, self.layers, self.hs, self.kv_dim, self.vocab, self.ctx
        f = self.f
        if f["xn"] >= self.fn + 15:
            raise ValueError("rmsnorm: the rounding shift of the output must be at least 1")
        stages = self.schedule()
        names = [n for n, _ in stages]
        states = ["IDLE", "INIT", *[f"S{i}" for i in range(len(stages))], "END"]
        sw = (len(states) - 1).bit_length()
        cw = max(max(max(c for _, c in stages), C).bit_length(), 1)

        def st(name: str) -> str:
            return f"{sw}'d{states.index('S' + str(names.index(name)))}"

        def any_state(fmt: str) -> str:
            return "(" + " || ".join(f"state == {st(fmt.format(li))}" for li in range(L)) + ")"

        ssh = f["q"] + f["k"] + 15 - SCORE_FRAC
        fh = f["h1"]
        nobs = L * 2 * D
        wrap = int(self.overflow == "wrap")
        mods = self.modules()
        lg = lambda n: max((n - 1).bit_length(), 1)  # noqa: E731
        o = []
        w = o.append
        w("// Generated by rtl-taalas (spatial) from the numeric spec; do not edit.")
        w("// The spatial engine: the matrices of every layer are circuits (sp_mv_*); this core")
        w("// sequences them with the elementwise units, the shared attention unit and the KV rings.")
        w("// One state per stage of the schedule: the cycles per token are fixed.")
        w("`timescale 1ns/1ps")
        w("module sp_core (")
        w("  input  wire        clk,")
        w("  input  wire        rst_n,")
        w("  input  wire        start,")
        w("  input  wire [15:0] token_in,")
        w("  input  wire [15:0] pos_in,")
        w("  input  wire [10:0] obs_sel,")
        w("  output reg  [31:0] obs_word,")
        w("  output reg         busy,")
        w("  output reg         done,")
        w("  output reg         error,")
        w("  output reg  [15:0] token_out")
        w(");")
        w(f"  // Rounding shift of the numeric spec ({self.rounding}), s >= 1.")
        w("  function automatic signed [63:0] rsh(input signed [63:0] v, input [6:0] s);")
        if self.rounding == "truncate":
            w("    rsh = v >>> s;")
        elif self.rounding == "half-even":
            w("    rsh = (v + ((64'sd1 <<< (s - 7'd1)) - 64'sd1) + ((v >>> s) & 64'sd1)) >>> s;")
        else:
            w("    rsh = (v + (64'sd1 <<< (s - 7'd1))) >>> s;")
        w("  endfunction")
        w(f"  // Narrowing of the numeric spec ({self.overflow}).")
        for bits in (16, 32):
            hi, lo = 2 ** (bits - 1) - 1, 2 ** (bits - 1)
            w(f"  function automatic signed [{bits - 1}:0] n{bits}(input signed [63:0] v);")
            if wrap:
                w(f"    n{bits} = v[{bits - 1}:0];")
            else:
                w(f"    n{bits} = v > 64'sd{hi} ? {bits}'sd{hi} : v < -64'sd{lo} ? -{bits}'sd{lo} : v[{bits - 1}:0];")
            w("  endfunction")
        w("")
        w(f"  reg [{sw - 1}:0] state;")
        w(f"  reg [{cw - 1}:0] cnt;")
        w(f"  reg [{lg(C) - 1}:0] hc;  // the KV row at the head of every ring")
        w("  reg [7:0] ly;  // the layer being computed")
        w("  wire [15:0] pos = pos_in;")
        for name, n in (("x", D), ("xn", D), ("qv", D), ("kn", KD), ("vn", KD), ("att", D), ("gg", H), ("obs", nobs)):
            w(f"  reg signed [15:0] {name} [0:{n - 1}];")
        qw = self.sq_bits
        w(f"  reg [{qw - 1}:0] sumsq;")
        w("  reg [16:0] rr;")
        w("  reg [6:0] e2;")
        w("  reg signed [31:0] mx [0:1];")
        w("  reg [22:0] ssum [0:1];")
        w(f"  reg [16:0] ee [0:{2 * C - 1}];  // exp of the scores, then the probabilities")
        nd = self.dividers
        w(f"  reg [22:0] rem [0:{nd - 1}];")
        w(f"  reg [15:0] qt [0:{nd - 1}];")
        w("  reg [4:0] dsb;  // bit of the divisions (16: load the next round)")
        w("  reg have;  // a valid row was seen in this pass")
        w("  reg [7:0] drd;  // round of the divisions")
        w(f"  reg signed [39:0] acc [0:{D - 1}];")
        w("  reg signed [31:0] best;")
        w("  reg [15:0] besti;")
        w("  integer i;")
        w("")
        w("  // ---- the matrices: one circuit per input vector and layer")
        w("  // The input vectors leave their registers bit-serially, LSB first (an arithmetic shift")
        w("  // per clock while a matrix reads them), into the circuits of the matrices.")
        for src, n in (("xn", D), ("att", D), ("gg", H)):
            w(f"  wire [{n - 1}:0] xs_{src};")
            w(f"  genvar g_{src};")
            w(f"  generate for (g_{src} = 0; g_{src} < {n}; g_{src} = g_{src} + 1) begin : v_{src}")
            w(f"    assign xs_{src}[g_{src}] = {src}[g_{src}][0];")
            w("  end endgenerate")
        for mod, mats, src, _n in mods:
            ports = []
            for mname in mats:
                mtx = self.matrices[mname]
                short = mname.split(".")[-1]
                w(f"  wire [{mtx.q.shape[0] * mtx.width - 1}:0] {mod}_{short};")
                ports.append(f".y_{short}({mod}_{short})")
            w(f"  reg go_{mod};")
            w(f"  wire done_{mod};")
            w(
                f"  {mod} u_{mod} (.clk(clk), .rst_n(rst_n), .start(go_{mod}), .xs(xs_{src}), "
                f"{', '.join(ports)}, .done(done_{mod}));"
            )

        def by_layer(width: int, name: str, fmt: str) -> None:
            expr = " : ".join([f"ly == 8'd{li} ? {fmt.format(li)}" for li in range(L - 1)] + [fmt.format(L - 1)])
            w(f"  wire [{width - 1}:0] {name} = {expr};")

        by_layer(16 * D, "q_out", "sp_mv_l{}_qkv_wq")
        by_layer(16 * KD, "k_out", "sp_mv_l{}_qkv_wk")
        by_layer(16 * KD, "v_out", "sp_mv_l{}_qkv_wv")
        by_layer(16 * D, "o_out", "sp_mv_l{}_o_wo")
        by_layer(16 * H, "h1_out", "sp_mv_l{}_w13_w1")
        by_layer(16 * H, "h3_out", "sp_mv_l{}_w13_w3")
        by_layer(16 * D, "f_out", "sp_mv_l{}_w2_w2")
        w("")
        w("  // ---- KV cache: per layer a K ring and a V ring; every ring rotates one row per clock,")
        w("  // the row at the head is row hc. The new row is written at the tail when hc == pos.")
        w(f"  wire [{16 * KD - 1}:0] new_k, new_v;")
        w("  genvar g_kv;")
        w(f"  generate for (g_kv = 0; g_kv < {KD}; g_kv = g_kv + 1) begin : v_kv")
        w("    assign new_k[16 * g_kv +: 16] = kn[g_kv];")
        w("    assign new_v[16 * g_kv +: 16] = vn[g_kv];")
        w("  end endgenerate")
        w(f"  wire at_pos = hc == pos[{lg(C) - 1}:0];")
        w("  // The rings have no per-flop reset: state INIT after reset fills them with zeros (one row")
        w(f"  // per clock, {C} clocks), so every flop holds a defined value before the first step.")
        for li in range(L):
            for kind in ("k", "v"):
                r = f"ring_{kind}{li}"
                w(f"  reg [{16 * KD - 1}:0] {r} [0:{C - 1}];")
                w(f"  wire ins_{kind}{li} = state == {st(f'layer {li} attention: scores and maximum')} && at_pos;")
                w("  always @(posedge clk) begin")
                w(f"    for (i = 0; i < {C - 1}; i = i + 1) {r}[i] <= {r}[i + 1];")
                w(f"    {r}[{C - 1}] <= state == {sw}'d1 ? {16 * KD}'d0 : ins_{kind}{li} ? new_{kind} : {r}[0];")
                w("  end")
        by_layer(16 * KD, "krow_ring", "ring_k{}[0]")
        by_layer(16 * KD, "vrow", "ring_v{}[0]")
        p1 = any_state("layer {} attention: scores and maximum")
        pv = any_state("layer {} attention: weighted sum of v")
        w(f"  wire in_p1 = {p1};")
        w(f"  wire in_pv = {pv};")
        w("  // The new K row is used at its own position before it reaches the head of the ring.")
        w(f"  wire [{16 * KD - 1}:0] krow = in_p1 && at_pos ? new_k : krow_ring;")
        w(f"  wire valid_t = {{{16 - lg(C)}'d0, hc}} <= pos;")
        w("")
        w(f"  // ---- attention unit: {D} multipliers. Score passes: both heads against the same K row")
        w("  // (one KV head); the weighted sum: probabilities times the V row.")
        w("  wire [16:0] p_h0 = ee[{1'b0, hc}];")
        w("  wire [16:0] p_h1 = ee[{1'b1, hc}];")
        w(f"  wire [{16 * KD - 1}:0] mrow = in_pv ? vrow : krow;")
        w(f"  wire signed [39:0] prod [0:{D - 1}];")
        w("  genvar g_m;")
        w(f"  generate for (g_m = 0; g_m < {D}; g_m = g_m + 1) begin : v_m")
        w(
            f"    wire signed [17:0] ma = in_pv ? $signed({{1'b0, g_m < {HS} ? p_h0 : p_h1}}) "
            f": $signed({{{{2{{qv[g_m][15]}}}}, qv[g_m]}});"
        )
        w(
            f"    wire signed [17:0] mb = $signed({{{{2{{mrow[16 * (g_m % {HS}) + 15]}}}}"
            f", mrow[16 * (g_m % {HS}) +: 16]}});"
        )
        w("    wire signed [35:0] mp;")
        w("    sp_mul #(.AW(18), .BW(18)) u_mul (.a(ma), .b(mb), .p(mp));")
        w("    assign prod[g_m] = {{4{mp[35]}}, mp};")
        w("  end endgenerate")
        for h in range(2):
            terms = " + ".join(f"prod[{h * HS + j}]" for j in range(HS))
            w(f"  wire signed [39:0] dot{h} = {terms};")
            prod_s = f"$signed({{{{24{{dot{h}[39]}}}}, dot{h}}}) * 64'sd{self.att_scale}"
            w(f"  wire signed [31:0] sc{h} = n32({self._rs(prod_s, ssh)});")
            w(f"  wire [{lg(EXP_RANGE * 32) - 1}:0] ea{h};")
            w(f"  sp_exp_index u_exp_index{h} (.mx(mx[{h}]), .sc(sc{h}), .a(ea{h}));")
            w(f"  wire [16:0] ex{h};")
            w(f"  sp_rom_exp u_exp{h} (.a(ea{h}), .d(ex{h}));")
        w("  wire [7:0] dnext = cnt == 0 ? 8'd0 : drd + 8'd1;  // the round a load starts")
        w(f"  wire [22:0] dden [0:{nd - 1}];")
        w(f"  wire [{nd - 1}:0] dbit;")
        w("  genvar g_d;")
        w(f"  generate for (g_d = 0; g_d < {nd}; g_d = g_d + 1) begin : v_d")
        w(f"    assign dden[g_d] = ssum[drd[{lg(self.div_rounds) - 1}]];  // the head of probability drd * {nd} + g_d")
        w("    assign dbit[g_d] = rem[g_d] >= dden[g_d];")
        w("  end endgenerate")
        w("")
        w("  // ---- rmsnorm: sum of squares, then the rsqrt table index (floor), as in L2")
        w(f"  wire signed [15:0] x_c = x[cnt[{lg(D) - 1}:0]];")
        w("  wire signed [31:0] sq;")
        w("  sp_mul #(.AW(16), .BW(16)) u_sq (.a(x_c), .b(x_c), .p(sq));")
        w("  wire [7:0] rms_a;")
        w("  wire [6:0] rms_e;")
        w("  sp_rms_index u_rms_index (.sumsq(sumsq), .a(rms_a), .e(rms_e));")
        w(f"  wire [{cw - 1}:0] nrow_c = cnt - {cw}'d{D + 1};")
        w(f"  wire [{lg(D) - 1}:0] nrow = nrow_c[{lg(D) - 1}:0];")
        naw = lg((2 * L + 1) * D)
        w(f"  reg [{naw - 1}:0] nbase;")
        w("  wire signed [15:0] x_n = x[nrow];")
        w("  wire signed [15:0] nw;")
        w(f"  sp_rom_norm u_norm (.a(nbase + {{{naw - lg(D)}'d0, nrow}}), .d(nw));")
        w("  wire signed [16:0] rsq;")
        w("  sp_rom_rsqrt u_rsqrt (.a(rms_a), .d(rsq));")
        w("  wire signed [31:0] nxw;")
        w("  sp_mul #(.AW(16), .BW(16)) u_nxw (.a(x_n), .b(nw), .p(nxw));")
        w("  wire signed [49:0] nxr;")
        w("  sp_mul #(.AW(32), .BW(18)) u_nxr (.a(nxw), .b($signed({1'b0, rr})), .p(nxr));")
        w("  wire signed [63:0] nprod = $signed({{14{nxr[49]}}, nxr});")
        w(f"  wire signed [15:0] xn_new = n16(rsh(nprod, 7'd{self.fn + 15 - f['xn']} + {{1'b0, e2[6:1]}}));")
        w("")
        w("  // ---- rope: one output per clock (q, then k); position tables")
        w(f"  wire rk = cnt >= {D};")
        w(f"  wire [7:0] rix = rk ? cnt - {D} : cnt;")
        w("  wire [7:0] rpair = {rix[7:1], 1'b0};")
        w("  wire signed [15:0] ra = rk ? k_out[16 * rpair +: 16] : q_out[16 * rpair +: 16];")
        w("  wire signed [15:0] rb = rk ? k_out[16 * (rpair + 1) +: 16] : q_out[16 * (rpair + 1) +: 16];")
        w(f"  wire [15:0] rtab = pos * 16'd{HS // 2} + {{{16 - (lg(HS) - 1)}'d0, rix[{lg(HS) - 1}:1]}};")
        w("  wire signed [15:0] rc, rs;")
        w(f"  sp_rom_cos u_cos (.a(rtab[{lg(C * HS // 2) - 1}:0]), .d(rc));")
        w(f"  sp_rom_sin u_sin (.a(rtab[{lg(C * HS // 2) - 1}:0]), .d(rs));")
        w("  wire signed [15:0] rt1 = rix[0] ? rs : rc;")
        w("  wire signed [15:0] rt2 = rix[0] ? rc : rs;")
        w("  wire signed [31:0] rm1, rm2;")
        w("  sp_mul #(.AW(16), .BW(16)) u_rm1 (.a(ra), .b(rt1), .p(rm1));")
        w("  sp_mul #(.AW(16), .BW(16)) u_rm2 (.a(rb), .b(rt2), .p(rm2));")
        w("  wire signed [63:0] rm1x = $signed({{32{rm1[31]}}, rm1});")
        w("  wire signed [63:0] rm2x = $signed({{32{rm2[31]}}, rm2});")
        w("  wire signed [15:0] rope_new = n16(rsh(rix[0] ? rm1x + rm2x : rm1x - rm2x, 7'd14));")
        w("")
        w("  // ---- silu gate: one output per clock, sigmoid table")
        w("  wire signed [15:0] s_h1 = h1_out[16 * cnt +: 16];")
        w("  wire signed [15:0] s_h3 = h3_out[16 * cnt +: 16];")
        w(f"  wire [{lg(2 * SIG_RANGE * SIG_STEPS) - 1}:0] s_a;")
        w("  sp_silu_index u_silu_index (.h1(s_h1), .a(s_a));")
        w("  wire signed [16:0] sg;")
        w("  sp_rom_sig u_sig (.a(s_a), .d(sg));")
        w("  wire signed [32:0] s_hs;")
        w("  sp_mul #(.AW(16), .BW(17)) u_hs (.a(s_h1), .b(sg), .p(s_hs));")
        w(f"  wire signed [63:0] s_s = {self._rs('$signed({{31{s_hs[32]}}, s_hs})', 15)};")
        w("  // |s| <= 2^15: 18 bits hold it exactly")
        w("  wire signed [33:0] s_g;")
        w("  sp_mul #(.AW(18), .BW(16)) u_sg (.a(s_s[17:0]), .b(s_h3), .p(s_g));")
        w(f"  wire signed [15:0] g_new = n16({self._rs('$signed({{30{s_g[33]}}, s_g})', fh + f['h3'] - f['g'])});")
        w("")
        w(f"  wire [{16 * D - 1}:0] emb_row;")
        w(f"  sp_rom_emb u_emb (.a(token_in[{lg(V) - 1}:0]), .d(emb_row));")
        w("")
        w("  // ---- the residual adds")
        w(f"  wire signed [15:0] res_o [0:{D - 1}];")
        w(f"  wire signed [15:0] res_f [0:{D - 1}];")
        w("  genvar g_r;")
        w(f"  generate for (g_r = 0; g_r < {D}; g_r = g_r + 1) begin : v_r")
        w("    wire [15:0] ow = o_out[16 * g_r +: 16];")
        w("    wire [15:0] fw = f_out[16 * g_r +: 16];")
        w("    wire signed [63:0] xr = $signed({{48{x[g_r][15]}}, x[g_r]});")
        w("    assign res_o[g_r] = n16(xr + $signed({{48{ow[15]}}, ow}));")
        w("    assign res_f[g_r] = n16(xr + $signed({{48{fw[15]}}, fw}));")
        w("  end endgenerate")
        w("")
        w("  // ---- the classifier's result of the current index")
        w("  wire signed [31:0] logit = sp_mv_cls_cls[32 * cnt +: 32];")
        w("")
        w("  always @(posedge clk) begin")
        w("    if (!rst_n) begin")
        w(f"      state <= {sw}'d1; cnt <= 0; hc <= 0; ly <= 8'd0; busy <= 1'b1; done <= 1'b0; error <= 1'b0;")
        w("      token_out <= 16'd0; sumsq <= 0; rr <= 17'd0; e2 <= 7'd0; nbase <= 0; have <= 1'b0;")
        w("      best <= 32'sd0; besti <= 16'd0; dsb <= 5'd0; drd <= 8'd0;")
        w("      mx[0] <= 32'sd0; mx[1] <= 32'sd0; ssum[0] <= 23'd0; ssum[1] <= 23'd0;")
        w(
            f"      for (i = 0; i < {D}; i = i + 1) begin x[i] <= 16'sd0; xn[i] <= 16'sd0; qv[i] <= 16'sd0; "
            f"att[i] <= 16'sd0; acc[i] <= 40'sd0; end"
        )
        w(f"      for (i = 0; i < {KD}; i = i + 1) begin kn[i] <= 16'sd0; vn[i] <= 16'sd0; end")
        w(f"      for (i = 0; i < {H}; i = i + 1) gg[i] <= 16'sd0;")
        w(f"      for (i = 0; i < {nobs}; i = i + 1) obs[i] <= 16'sd0;")
        w(f"      for (i = 0; i < {2 * C}; i = i + 1) ee[i] <= 17'd0;")
        w(f"      for (i = 0; i < {nd}; i = i + 1) begin rem[i] <= 23'd0; qt[i] <= 16'd0; end")
        for mod, *_ in mods:
            w(f"      go_{mod} <= 1'b0;")
        w("    end else begin")
        w("      hc <= hc + 1'b1;")
        w("      done <= 1'b0;")
        w("      cnt <= cnt + 1'b1;")
        for mod, *_ in mods:
            w(f"      go_{mod} <= 1'b0;")
        w("      case (state)")
        w(f"        {sw}'d0: begin  // idle")
        w("          cnt <= 0;")
        w("          if (start) begin")
        w(f"            if (pos_in >= 16'd{C} || token_in >= 16'd{V}) begin error <= 1'b1; done <= 1'b1; end")
        w(f"            else begin error <= 1'b0; busy <= 1'b1; ly <= 8'd0; state <= {st('embedding')}; end")
        w("          end")
        w("        end")
        w(
            f"        {sw}'d1: if (cnt == {C - 1}) begin state <= {sw}'d0; cnt <= 0; "
            f"busy <= 1'b0; end  // init: clear the rings"
        )

        def stage(i: int, name: str, n: int, body: list[str]) -> None:
            nxt = f"{sw}'d{states.index('S' + str(i + 1)) if i + 1 < len(stages) else states.index('END')}"
            w(f"        {sw}'d{states.index('S' + str(i))}: begin  // {name}: {n} cycles")
            for b in body:
                w("          " + b)
            w(f"          if (cnt == {n - 1}) begin state <= {nxt}; cnt <= 0; end")
            w("        end")

        def rms(base: int) -> list[str]:
            return [
                f"if (cnt == 0) nbase <= {naw}'d{base};",
                f"if (cnt < {D}) sumsq <= (cnt == 0 ? {qw}'d0 : sumsq) + {{{qw - 32}'d0, sq[31:0]}};",
                f"if (cnt == {D}) begin rr <= rsq; e2 <= rms_e; end",
                f"if (cnt > {D}) xn[nrow] <= xn_new;",
            ]

        for i, (name, n) in enumerate(stages):
            body: list[str]
            parts = name.split(" ")
            li = int(parts[1]) if parts[0] == "layer" else None
            if name == "embedding":
                body = [f"for (i = 0; i < {D}; i = i + 1) x[i] <= emb_row[16 * i +: 16];"]
            elif name.endswith("rmsnorm (attention)"):
                body = rms(li * 2 * D)
            elif name.endswith("rmsnorm (ffn)"):
                body = rms(li * 2 * D + D)
            elif name == "final rmsnorm":
                body = rms(L * 2 * D)
            elif name.endswith("q, k, v"):
                body = [
                    f"if (cnt == 0) go_sp_mv_l{li}_qkv <= 1'b1;",
                    f"if (cnt >= 2) for (i = 0; i < {D}; i = i + 1) xn[i] <= xn[i] >>> 1;",
                ]
            elif name.endswith("rope"):
                body = [
                    f"if (cnt == 0) for (i = 0; i < {KD}; i = i + 1) vn[i] <= v_out[16 * i +: 16];",
                    f"if (rk) kn[rix[{lg(KD) - 1}:0]] <= rope_new; else qv[rix[{lg(D) - 1}:0]] <= rope_new;",
                ]
            elif name.endswith("scores and maximum"):
                body = [
                    "// the maximum over the valid rows (t <= pos), visited from the row at the head",
                    "if (cnt == 0) have <= 1'b0;",
                    "if (valid_t) begin",
                    "  if (cnt == 0 || !have || sc0 > mx[0]) mx[0] <= sc0;",
                    "  if (cnt == 0 || !have || sc1 > mx[1]) mx[1] <= sc1;",
                    "  have <= 1'b1;",
                    "end",
                ]
            elif name.endswith("exp and sum"):
                body = [
                    "ee[{1'b0, hc}] <= valid_t ? ex0 : 17'd0;",
                    "ee[{1'b1, hc}] <= valid_t ? ex1 : 17'd0;",
                    "ssum[0] <= (cnt == 0 ? 23'd0 : ssum[0]) + (valid_t ? {6'd0, ex0} : 23'd0);",
                    "ssum[1] <= (cnt == 0 ? 23'd0 : ssum[1]) + (valid_t ? {6'd0, ex1} : 23'd0);",
                ]
            elif name.endswith("divisions"):
                body = [
                    "// p = floor(e * 2^15 / sum): restoring division, one quotient bit per clock;",
                    f"// {nd} dividers, {self.div_rounds} rounds of 17 clocks (load, then 16 bits).",
                    "if (cnt == 0) begin dsb <= 5'd0; drd <= 8'd0; end",
                    "else if (dsb == 5'd16) begin dsb <= 5'd0; drd <= drd + 8'd1; end",
                    "else dsb <= dsb + 5'd1;",
                    f"for (i = 0; i < {nd}; i = i + 1) begin",
                    "  if (cnt == 0 || dsb == 5'd16) begin",
                    f"    rem[i] <= {{6'd0, ee[{{dnext[{lg(self.div_rounds) - 1}:0], i[{lg(nd) - 1}:0]}}]}};",
                    "    qt[i] <= 16'd0;",
                    "  end else begin",
                    "    rem[i] <= (dbit[i] ? rem[i] - dden[i] : rem[i]) << 1;",
                    "    qt[i] <= {qt[i][14:0], dbit[i]};",
                    f"    if (dsb == 5'd15) ee[{{drd[{lg(self.div_rounds) - 1}:0], i[{lg(nd) - 1}:0]}}] "
                    "<= {1'b0, qt[i][14:0], dbit[i]};",
                    "  end",
                    "end",
                ]
            elif name.endswith("weighted sum of v"):
                body = [
                    f"if (cnt < {C}) for (i = 0; i < {D}; i = i + 1) acc[i] <= (cnt == 0 ? 40'sd0 : acc[i]) "
                    f"+ (valid_t ? prod[i] : 40'sd0);",
                    f"if (cnt == {C}) for (i = 0; i < {D}; i = i + 1) "
                    "att[i] <= n16(rsh($signed({{24{acc[i][39]}}, acc[i]}), 7'd15));",
                ]
            elif name.endswith(" wo"):
                body = [
                    f"if (cnt == 0) go_sp_mv_l{li}_o <= 1'b1;",
                    f"if (cnt >= 2) for (i = 0; i < {D}; i = i + 1) att[i] <= att[i] >>> 1;",
                ]
            elif name.endswith("residual (attention)"):
                body = [
                    f"for (i = 0; i < {D}; i = i + 1) begin",
                    "  x[i] <= res_o[i];",
                    f"  obs[{2 * li * D} + i] <= res_o[i];",
                    "end",
                ]
            elif name.endswith("w1, w3"):
                body = [
                    f"if (cnt == 0) go_sp_mv_l{li}_w13 <= 1'b1;",
                    f"if (cnt >= 2) for (i = 0; i < {D}; i = i + 1) xn[i] <= xn[i] >>> 1;",
                ]
            elif name.endswith("silu gate"):
                body = [f"gg[cnt[{lg(H) - 1}:0]] <= g_new;"]
            elif name.endswith(" w2"):
                body = [
                    f"if (cnt == 0) go_sp_mv_l{li}_w2 <= 1'b1;",
                    f"if (cnt >= 2) for (i = 0; i < {H}; i = i + 1) gg[i] <= gg[i] >>> 1;",
                ]
            elif name.endswith("residual (ffn)"):
                body = [
                    f"for (i = 0; i < {D}; i = i + 1) begin",
                    "  x[i] <= res_f[i];",
                    f"  obs[{(2 * li + 1) * D} + i] <= res_f[i];",
                    "end",
                    "ly <= ly + 8'd1;",
                ]
            elif name == "classifier":
                body = [
                    "if (cnt == 0) go_sp_mv_cls <= 1'b1;",
                    f"if (cnt >= 2) for (i = 0; i < {D}; i = i + 1) xn[i] <= xn[i] >>> 1;",
                ]
            elif name == "argmax":
                body = [
                    "// the lowest index wins a tie",
                    f"if (cnt < {V} && (cnt == 0 || logit > best)) begin best <= logit; "
                    f"besti <= {{{16 - cw}'d0, cnt}}; end",
                    f"if (cnt == {V}) token_out <= besti;",
                ]
            else:
                raise AssertionError(name)
            stage(i, name, n, body)
        w(
            f"        {sw}'d{states.index('END')}: begin busy <= 1'b0; done <= 1'b1; ly <= 8'd0; "
            f"state <= {sw}'d0; cnt <= 0; end"
        )
        w(f"        default: state <= {sw}'d0;")
        w("      endcase")
        w("    end")
        w("  end")
        w("")
        w("  // ---- observation points: the residual stream after attention and after the FFN of")
        w("  // every layer, the logits and the token of the last step")
        w("  always @* begin")
        ob = f"obs[obs_sel[{lg(nobs) - 1}:0]]"
        w(f"    if (obs_sel < 11'd{nobs}) obs_word = {{{{16{{{ob}[15]}}}}, {ob}}};")
        w(f"    else if (obs_sel < 11'd{nobs + V}) obs_word = sp_mv_cls_cls[32 * (obs_sel - 11'd{nobs}) +: 32];")
        w("    else obs_word = {16'd0, token_out};")
        w("  end")
        w("endmodule")
        return "\n".join(o) + "\n"

    def units_verilog(self) -> str:
        """The table-index units of the core as modules of their own (the core instantiates them,
        sim-verilator tests them exhaustively): rmsnorm's rsqrt index, softmax's exp index, the
        sigmoid index of SiLU. Floors, as the numeric spec says for table indices."""
        D, f = self.dim, self.f
        lg = lambda n: max((n - 1).bit_length(), 1)  # noqa: E731
        qw = self.sq_bits
        cap = EXP_RANGE * 2**SCORE_FRAC - 1
        fh = f["h1"]
        lim = SIG_RANGE * 2**fh
        shift = f"(z >>> {fh - 4})" if fh >= 4 else f"(z <<< {4 - fh})"
        ew = lg(EXP_RANGE * 32)
        sw = lg(2 * SIG_RANGE * SIG_STEPS)
        return "\n".join(
            [
                "// Generated by rtl-taalas (spatial) from the numeric spec; do not edit.",
                "// Table-index units of the spatial core (floors, as the numeric spec's table indices).",
                "`timescale 1ns/1ps",
                "// rmsnorm: v = sum(x^2) / dim + eps (at least 1); e2 = the even exponent of v;",
                "// t = v scaled to [64, 256); a = t - 64 (rsqrt table address).",
                "module sp_rms_index (",
                f"  input  wire [{qw - 1}:0] sumsq,",
                "  output wire [7:0] a,",
                "  output reg  [6:0] e",
                ");",
                f"  reg [{qw - 1}:0] v, t;",
                "  reg [6:0] k;",
                "  integer i;",
                "  always @* begin",
                f"    v = (sumsq >> {lg(D) if D > 1 else 0}) + {qw}'d{self.eps_q};",
                f"    if (v == {qw}'d0) v = {qw}'d1;",
                "    k = 7'd0;",
                f"    for (i = 0; i < {qw}; i = i + 1) if (v[i]) k = i[6:0];",
                "    e = {k[6:1], 1'b0};",
                "    t = e >= 7'd6 ? v >> (e - 7'd6) : v << (7'd6 - e);",
                "  end",
                "  assign a = t[7:0] - 8'd64;",
                "endmodule",
                "",
                "// softmax: d = max - score, capped; a = d >> (score fraction bits - 5) (exp table address).",
                "module sp_exp_index (",
                "  input  wire signed [31:0] mx,",
                "  input  wire signed [31:0] sc,",
                f"  output wire [{ew - 1}:0] a",
                ");",
                "  wire signed [32:0] d = $signed({mx[31], mx}) - $signed({sc[31], sc});",
                f"  wire [32:0] c = d > 33'sd{cap} ? 33'd{cap} : d;",
                f"  assign a = c[{ew + SCORE_FRAC - 5 - 1}:{SCORE_FRAC - 5}];",
                "endmodule",
                "",
                "// SiLU: z = h1 clamped to the sigmoid range; a = (z >> (frac_h1 - 4)) + offset",
                "// (sigmoid table address).",
                "module sp_silu_index (",
                "  input  wire signed [15:0] h1,",
                f"  output wire [{sw - 1}:0] a",
                ");",
                "  wire signed [31:0] hx = $signed({{16{h1[15]}}, h1});",
                f"  wire signed [31:0] z = hx < -32'sd{lim} ? -32'sd{lim} : hx > 32'sd{lim - 1} ? 32'sd{lim - 1} : hx;",
                f"  wire signed [31:0] ix = {shift} + 32'sd{SIG_RANGE * SIG_STEPS};",
                f"  assign a = ix[{sw - 1}:0];",
                "endmodule",
                "",
            ]
        )

    def files(self) -> dict[str, str]:
        """The Verilog of the design: the core and one matvec circuit per input vector and layer."""
        out = {"sp_core.v": self.core_verilog(), "sp_roms.v": self.roms_verilog(), "sp_units.v": self.units_verilog()}
        for mod, mats, _src, n in self.modules():
            out[f"{mod}.v"] = self.matvec_verilog(mod, mats, n)
        return out


def read_back(text: str, inputs: int) -> dict[str, np.ndarray]:
    """The linear map wired by a generated matvec module (the round trip of FR-RTL-02): for every
    output row the integer coefficient of every input, read from the netlist alone. Equals
    q * m (the mantissa of the input's group) when the circuit implements the weights."""
    import re

    lin: dict[str, np.ndarray] = {}
    for i in range(inputs):
        e = np.zeros(inputs, dtype=object)
        e[i] = 1
        lin[f"xs[{i}]"] = e
    zero = np.zeros(inputs, dtype=object)
    lin["1'b0"] = zero

    def val(name: str) -> np.ndarray:
        return lin[name.strip()]

    rows: dict[str, dict[int, np.ndarray]] = {}
    width: dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if m := re.match(r"sp_taps #\(\.N\((\d+)\)\) \S+ \(.*\.a\(([^)]*\]?)\), \.t\((\w+)\)\);", line):
            n, src, dst = int(m.group(1)), m.group(2), m.group(3)
            for j in range(1, n + 1):
                lin[f"{dst}[{j - 1}]"] = val(src) * 2**j
        elif m := re.match(
            r"sp_sadd #\(\.SUB\((\d)\)\) \S+ \(.*\.a\(([^)]*\]?)\), \.b\(([^)]*\]?)\), \.s\((\w+)\)\);", line
        ):
            sub, a_, b_, dst = int(m.group(1)), m.group(2), m.group(3), m.group(4)
            lin[dst] = val(a_) - val(b_) if sub else val(a_) + val(b_)
        elif m := re.match(r"sp_out #\(\.W\((\d+)\).*\) o_(\w+)_(\d+) \(.*\.a\(([^)]*\]?)\), \.y\(.*\)\);", line):
            w_, name, row, src = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
            rows.setdefault(name, {})[row] = val(src)
            width[name] = w_
    return {name: np.array([r[k] for k in sorted(r)], dtype=object) for name, r in rows.items()}


def check_round_trip(sp: Spatial, files: dict[str, str]) -> None:
    """Reads every matvec circuit back and compares its linear map with the numeric weights."""
    for mod, mats, _src, n in sp.modules():
        back = read_back(files[f"{mod}.v"], n)
        for mname in mats:
            mtx = sp.matrices[mname]
            out, inp = mtx.q.shape
            idx = np.arange(inp) // mtx.group
            want = mtx.q.astype(object) * mtx.m[:, idx].astype(object)
            got = back.get(mname.split(".")[-1])
            if got is None or got.shape != (out, n) or not (got[:, :inp] == want).all() or (got[:, inp:] != 0).any():
                raise ValueError(f"{mname}: the generated circuit does not implement the numeric weights")
