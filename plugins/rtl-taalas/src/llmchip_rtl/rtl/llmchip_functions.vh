// Arithmetic functions of the llmchip vector engine. Shared by llmchip_core
// and llmchip_alu (the unit-test wrapper), so the unit tests exercise exactly
// the code the core uses. Reference: the scalar functions of isa.py, which are
// checked against the numeric spec's L2 model.

// Round-half-up shift: (v + 2^(s-1)) >>> s; a negative s shifts left.
function automatic signed [63:0] f_rsh(input signed [63:0] v, input signed [31:0] s);
  begin
    if (s > 0) f_rsh = (v + (64'sd1 <<< (s - 1))) >>> s;
    else if (s < 0) f_rsh = v <<< (-s);
    else f_rsh = v;
  end
endfunction

function automatic signed [63:0] f_sat16(input signed [63:0] v);
  begin
    if (v > 64'sd32767) f_sat16 = 64'sd32767;
    else if (v < -64'sd32768) f_sat16 = -64'sd32768;
    else f_sat16 = v;
  end
endfunction

function automatic signed [63:0] f_sat32(input signed [63:0] v);
  begin
    if (v > 64'sd2147483647) f_sat32 = 64'sd2147483647;
    else if (v < -64'sd2147483648) f_sat32 = -64'sd2147483648;
    else f_sat32 = v;
  end
endfunction

// Sign-extends the low `bits` bits of v.
function automatic signed [63:0] f_sx(input [63:0] v, input integer bits);
  reg [63:0] t;
  begin
    t = v << (64 - bits);
    f_sx = $signed(t) >>> (64 - bits);
  end
endfunction

// Index of the most significant set bit (0 for v = 0).
function automatic [6:0] f_msb(input [63:0] v);
  integer k;
  begin
    f_msb = 7'd0;
    for (k = 0; k < 64; k = k + 1)
      if (v[k]) f_msb = k[6:0];
  end
endfunction

// rmsnorm normalization of v (v >= 1): e2 = even exponent, t = v / 2^(e2-6) in [64, 256).
function automatic [6:0] f_rsqrt_e2(input [63:0] v);
  reg [6:0] k;
  begin
    k = f_msb(v);
    f_rsqrt_e2 = {k[6:1], 1'b0};
  end
endfunction

function automatic [63:0] f_rsqrt_t(input [63:0] v);
  reg [6:0] e2;
  begin
    e2 = f_rsqrt_e2(v);
    if (e2 >= 7'd6) f_rsqrt_t = v >> (e2 - 7'd6);
    else f_rsqrt_t = v << (7'd6 - e2);
  end
endfunction

// Sigmoid table index of h1 (fraction bits frac, clamp = 8 * 2^frac, offset = 128).
function automatic signed [63:0] f_silu_index(input signed [63:0] h1, input signed [31:0] frac, input signed [63:0] clamp, input signed [63:0] offset);
  reg signed [63:0] z;
  begin
    z = h1;
    if (z < -clamp) z = -clamp;
    if (z > clamp - 1) z = clamp - 1;
    if (frac >= 4) f_silu_index = (z >>> (frac - 4)) + offset;
    else f_silu_index = (z <<< (4 - frac)) + offset;
  end
endfunction

// Exp table index of d = max - s >= 0.
function automatic signed [63:0] f_exp_index(input signed [63:0] d, input signed [63:0] cap, input signed [31:0] shift);
  begin
    f_exp_index = ((d > cap) ? cap : d) >>> shift;
  end
endfunction
