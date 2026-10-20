// Arithmetic functions of the llmchip vector engine. Shared by llmchip_core
// and llmchip_alu (the unit-test wrapper), so the unit tests exercise exactly
// the code the core uses. Reference: the scalar functions of isa.py, which are
// checked against the numeric spec's L2 model.
//
// FW is the function width: the datapath width DW of the design (generated in
// llmchip_params.vh; the generator proves that no intermediate value needs more).
`ifndef FW
`define FW `DW
`endif

// Sign-extends the low `bits` bits of v.
function automatic signed [`FW-1:0] f_sx(input [`FW-1:0] v, input integer bits);
  reg [`FW-1:0] t;
  begin
    t = v << (`FW - bits);
    f_sx = $signed(t) >>> (`FW - bits);
  end
endfunction

// Result shift with the numeric spec's rounding mode; a negative s shifts left.
// t = v >>> (s-1) keeps the half bit in t[0], so every mode needs one small adder:
//   half-up (ROUND_MODE 0):    (t + 1) >>> 1
//   half-even (ROUND_HALF_EVEN): (t >>> 1) + (t[0] & (sticky | t[1])), sticky = any bit below the half
//   truncate (ROUND_TRUNCATE): v >>> s
// Designs whose program only shifts right (RSH_POSITIVE, proven by the generator)
// omit the other cases.
`ifdef ROUND_TRUNCATE
function automatic signed [`FW-1:0] f_rsh_pos(input signed [`FW-1:0] v, input signed [7:0] s);
  f_rsh_pos = v >>> s;
endfunction
`elsif ROUND_HALF_EVEN
function automatic signed [`FW-1:0] f_rsh_pos(input signed [`FW-1:0] v, input signed [7:0] s);
  reg signed [`FW-1:0] t;
  reg sticky;
  begin
    t = v >>> (s - 8'sd1);
    sticky = |(v & ((({{(`FW-1){1'b0}}, 1'b1}) << (s - 8'sd1)) - 1));
    // $signed: an unsigned operand would make the whole sum unsigned and t >>> 1 a logical shift.
    f_rsh_pos = (t >>> 1) + $signed({1'b0, t[0] & (sticky | t[1])});
  end
endfunction
`else
function automatic signed [`FW-1:0] f_rsh_pos(input signed [`FW-1:0] v, input signed [7:0] s);
  reg signed [`FW-1:0] t;
  begin
    t = v >>> (s - 8'sd1);
    f_rsh_pos = (t + 1) >>> 1;
  end
endfunction
`endif

function automatic signed [`FW-1:0] f_rsh(input signed [`FW-1:0] v, input signed [7:0] s);
  begin
`ifdef RSH_POSITIVE
    f_rsh = f_rsh_pos(v, s);
`else
    if (s > 0) f_rsh = f_rsh_pos(v, s);
    else if (s < 0) f_rsh = v <<< (-s);
    else f_rsh = v;
`endif
  end
endfunction

// Narrowing of a result with the numeric spec's overflow mode: clamped (saturate)
// or its low bits sign-extended (OVERFLOW_WRAP, two's complement).
function automatic signed [`FW-1:0] f_sat16(input signed [`FW-1:0] v);
  begin
`ifdef OVERFLOW_WRAP
    f_sat16 = f_sx(v, 16);
`else
    if (v > 32767) f_sat16 = 32767;
    else if (v < -32768) f_sat16 = -32768;
    else f_sat16 = v;
`endif
  end
endfunction

function automatic signed [`FW-1:0] f_sat32(input signed [`FW-1:0] v);
  begin
`ifdef OVERFLOW_WRAP
    f_sat32 = f_sx(v, 32);
`else
    if (v > 64'sd2147483647) f_sat32 = 64'sd2147483647;
    else if (v < -64'sd2147483648) f_sat32 = -64'sd2147483648;
    else f_sat32 = v;
`endif
  end
endfunction

// The MATVEC total after a group addition: narrowed to ACCB bits when the numeric
// spec's accumulator is narrower than the proven bound (ACC_NARROW), else unchanged.
`ifndef ACC_NARROW
function automatic signed [`FW-1:0] f_acc(input signed [`FW-1:0] v);
  f_acc = v;
endfunction
`elsif OVERFLOW_WRAP
function automatic signed [`FW-1:0] f_acc(input signed [`FW-1:0] v);
  f_acc = f_sx(v, `ACCB);
endfunction
`else
function automatic signed [`FW-1:0] f_acc(input signed [`FW-1:0] v);
  reg signed [`FW-1:0] hi;
  begin
    hi = ({{(`FW-1){1'b0}}, 1'b1} <<< (`ACCB - 1)) - 1;
    if (v > hi) f_acc = hi;
    else if (v < -hi - 1) f_acc = -hi - 1;
    else f_acc = v;
  end
endfunction
`endif

// Index of the most significant set bit (0 for v = 0).
function automatic [6:0] f_msb(input [`FW-1:0] v);
  integer k;
  begin
    f_msb = 7'd0;
    for (k = 0; k < `FW; k = k + 1)
      if (v[k]) f_msb = k[6:0];
  end
endfunction

// rmsnorm normalization of v (v >= 1): e2 = even exponent, t = v / 2^(e2-6) in [64, 256).
function automatic [6:0] f_rsqrt_e2(input [`FW-1:0] v);
  /* verilator lint_off UNUSEDSIGNAL */
  reg [6:0] k;  // bit 0 is dropped
  /* verilator lint_on UNUSEDSIGNAL */
  begin
    k = f_msb(v);
    f_rsqrt_e2 = {k[6:1], 1'b0};
  end
endfunction

function automatic [`FW-1:0] f_rsqrt_t(input [`FW-1:0] v);
  reg [6:0] e2;
  begin
    e2 = f_rsqrt_e2(v);
    if (e2 >= 7'd6) f_rsqrt_t = v >> (e2 - 7'd6);
    else f_rsqrt_t = v << (7'd6 - e2);
  end
endfunction

// Sigmoid table index of h1 (int16; fraction bits frac <= 15, clamp = 8 * 2^frac,
// offset = 128): always in [0, 256). SILU_FRAC_GE4: every program has frac >= 4.
function automatic [15:0] f_silu_index(input signed [15:0] h1, input signed [7:0] frac, input signed [23:0] clamp, input signed [15:0] offset);
  reg signed [23:0] z;
  begin
    z = h1;
    if (z < -clamp) z = -clamp;
    if (z > clamp - 1) z = clamp - 1;
`ifdef SILU_FRAC_GE4
    f_silu_index = (z >>> (frac - 8'sd4)) + offset;
`else
    if (frac >= 4) f_silu_index = (z >>> (frac - 4)) + offset;
    else f_silu_index = (z <<< (4 - frac)) + offset;
`endif
  end
endfunction

// Exp table index of d = max - s >= 0 (cap < 2^16).
function automatic [15:0] f_exp_index(input [`FW-1:0] d, input [15:0] cap, input [4:0] shift);
  begin
    f_exp_index = ((d > cap) ? cap : d[15:0]) >> shift;
  end
endfunction
