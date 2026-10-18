// Combinational wrapper of the arithmetic functions, for unit tests
// (boundary, exhaustive and random tests against isa.py).
`timescale 1ns/1ps
module llmchip_alu (
  input  wire signed [63:0] v,
  input  wire signed [31:0] s,
  input  wire signed [63:0] clamp,
  input  wire signed [63:0] offset,
  output wire signed [63:0] rsh,
  output wire signed [63:0] sat16,
  output wire signed [63:0] sat32,
  output wire signed [63:0] silu_index,
  output wire signed [63:0] exp_index,
  output wire [6:0] rsqrt_e2,
  output wire [63:0] rsqrt_t
);
  `include "llmchip_functions.vh"
  assign rsh = f_rsh(v, s);
  assign sat16 = f_sat16(v);
  assign sat32 = f_sat32(v);
  assign silu_index = f_silu_index(v, s, clamp, offset);
  assign exp_index = f_exp_index(v, clamp, s);
  assign rsqrt_e2 = f_rsqrt_e2(v);
  assign rsqrt_t = f_rsqrt_t(v);
endmodule
