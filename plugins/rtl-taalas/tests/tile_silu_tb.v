// Testbench for tile_silu: gate/up values and the expected gate_mul output.
`timescale 1ns/1ps
`default_nettype none

module tile_silu_tb;
  parameter integer DIM = 64;
  parameter integer F1 = 15;
  parameter integer F3 = 15;
  parameter integer FG = 19;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  reg en = 1'b1;
  always #5 clk = ~clk;
  reg signed [DIM*16-1:0] h1 = {DIM*16{1'b0}};
  reg signed [DIM*16-1:0] h3 = {DIM*16{1'b0}};
  wire signed [DIM*16-1:0] g;

  reg [DIM*16-1:0] h1mem [0:0];
  reg [DIM*16-1:0] h3mem [0:0];
  reg [15:0] expected [0:DIM-1];
  integer i;
  integer errors;

  tile_silu #(.DIM(DIM), .F1(F1), .F3(F3), .FG(FG), .INIT("sigmoid.hex")) dut (.clk(clk), .rst_n(rst_n), .en(en), .h1(h1), .h3(h3), .g(g));

  initial begin
    errors = 0;
    $readmemh("h1.hex", h1mem);
    $readmemh("h3.hex", h3mem);
    $readmemh("expected.hex", expected);
    h1 = h1mem[0];
    h3 = h3mem[0];
    repeat (2) @(posedge clk);
    rst_n = 1'b1;
    repeat (3) @(posedge clk);
    #1;
    for (i = 0; i < DIM; i = i + 1) begin
      if ($signed(g[i*16 +: 16]) !== $signed(expected[i])) begin
        $display("FAIL g[%0d] = %0d, expected %0d", i, $signed(g[i*16 +: 16]), $signed(expected[i]));
        errors = errors + 1;
      end
    end
    if (errors == 0) $display("PASS");
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
