// Unit-test wrapper of the bit-serial cells of a spatial bundle (sp_lib.v): a serial adder
// (a + b, or a - b with SUB) feeding a row's output stage (rounding shift S, W result
// bits, the bundle's rounding and overflow modes), sequenced like a matrix module: `start`,
// then L stream bits (k = 0 .. L-1), the result in y after the last bit.
`timescale 1ns/1ps
module spatial_unit #(parameter S = 16, W = 16, L = 40, ROUND = 0, WRAP = 0, SUB = 0) (
  input  wire         clk,
  input  wire         rst_n,
  input  wire         start,
  input  wire         a,
  input  wire         b,
  output wire [W-1:0] y
);
  reg [7:0] k;
  reg run;
  wire first = run && k == 0;
  wire last = run && k == L - 1;
  wire clr = start || !rst_n;
  always @(posedge clk) begin
    if (!rst_n) begin run <= 1'b0; k <= 8'd0; end
    else if (start) begin run <= 1'b1; k <= 8'd0; end
    else if (run) begin if (k == L - 1) run <= 1'b0; else k <= k + 8'd1; end
  end
  wire s;
  sp_sadd #(.SUB(SUB)) u_add (.clk(clk), .clr(clr), .a(a), .b(b), .s(s));
  sp_out #(.W(W), .ROUND(ROUND), .WRAP(WRAP)) u_out (
    .clk(clk), .rst_n(rst_n), .first(first), .lo(run && k + 1 < S), .hb(run && k + 1 == S), .rb(run && k == S),
    .col(run && k >= S && k < S + W), .hi(run && k >= S + W), .last(last), .a(s), .y(y));
endmodule
