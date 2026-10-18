// Qualification design of the rtl-functional-sim capability: a saturating
// multiply-accumulate with a round-half-up output shift, the arithmetic that
// the generated engines rely on. The expected outputs are computed in Python.
`timescale 1ns/1ps
module qual (
  input  wire               clk,
  input  wire               rst,
  input  wire signed [15:0] a,
  input  wire signed [15:0] b,
  output reg  signed [31:0] acc,
  output wire signed [31:0] y
);
  wire signed [63:0] product = a * b;
  wire signed [63:0] sum = {{32{acc[31]}}, acc} + product;
  always @(posedge clk)
    if (rst) acc <= 32'sd0;
    else if (sum > 64'sd2147483647) acc <= 32'sd2147483647;
    else if (sum < -64'sd2147483648) acc <= -32'sd2147483648;
    else acc <= sum[31:0];
  wire signed [32:0] rounded = {acc[31], acc} + 33'sd4;
  wire signed [32:0] shifted = rounded >>> 3;
  assign y = shifted[31:0];
endmodule
