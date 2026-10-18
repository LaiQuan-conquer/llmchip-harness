// Combinational wrapper of the arithmetic functions, for unit tests
// (boundary, exhaustive and random tests against isa.py). The functions are
// compiled with the design's datapath width, as in the core.
`timescale 1ns/1ps
`include "llmchip_params.vh"
module llmchip_alu (
  input  wire signed [`DW-1:0] v,
  input  wire signed [31:0]    s,
  input  wire signed [`DW-1:0] clamp,
  input  wire signed [`DW-1:0] offset,
  output wire signed [`DW-1:0] rsh,
  output wire signed [`DW-1:0] sat16,
  output wire signed [`DW-1:0] sat32,
  output wire [15:0]           silu_index,
  output wire [15:0]           exp_index,
  output wire [6:0]            rsqrt_e2,
  output wire [`DW-1:0]        rsqrt_t
);
  `include "llmchip_functions.vh"
  assign rsh = f_rsh(v, s[7:0]);
  assign sat16 = f_sat16(v);
  assign sat32 = f_sat32(v);
  // Each function is exercised on its domain: h1 int16, frac 8-bit, cap < 2^16, shift < 32.
  assign silu_index = f_silu_index(v[15:0], s[7:0], clamp[23:0], offset[15:0]);
  assign exp_index = f_exp_index(v, clamp[15:0], s[4:0]);
  assign rsqrt_e2 = f_rsqrt_e2(v);
  assign rsqrt_t = f_rsqrt_t(v);
endmodule
