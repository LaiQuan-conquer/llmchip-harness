// Testbench for tile_rmsnorm: x, the norm weights and the expected y are written by
// tests/test_tile_normalization.py from GraphL2.rmsnorm.
`timescale 1ns/1ps
`default_nettype none

module tile_rmsnorm_tb;
  parameter integer DIM = 32;
  parameter integer LOG2DIM = 5;
  parameter integer FN = 18;
  parameter integer FO = 12;
  parameter integer EPS_Q = 69;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  reg start = 1'b0;
  reg signed [DIM*16-1:0] x = {DIM*16{1'b0}};
  reg signed [DIM*16-1:0] weight = {DIM*16{1'b0}};
  wire signed [DIM*16-1:0] y;
  wire done;

  reg [DIM*16-1:0] xmem [0:0];
  reg [DIM*16-1:0] wmem [0:0];
  reg [15:0] expected [0:DIM-1];
  integer i;
  integer errors;
  integer cycles;

  always #5 clk = ~clk;

  tile_rmsnorm #(.DIM(DIM), .LOG2DIM(LOG2DIM), .FN(FN), .FO(FO), .EPS_Q(EPS_Q), .INIT("rsqrt.hex")) dut (
    .clk(clk), .rst_n(rst_n), .start(start), .x(x), .weight(weight), .y(y), .done(done)
  );

  initial begin
    errors = 0;
    cycles = 0;
    $readmemh("x.hex", xmem);
    $readmemh("weight.hex", wmem);
    $readmemh("expected.hex", expected);
    x = xmem[0];
    weight = wmem[0];
    repeat (3) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk);
    start = 1'b1;
    @(posedge clk);
    start = 1'b0;
    while (!done && cycles < 100000) begin
      @(posedge clk);
      cycles = cycles + 1;
    end
    if (!done) begin
      $display("FAIL timeout");
      errors = errors + 1;
    end else begin
      for (i = 0; i < DIM; i = i + 1) begin
        if ($signed(y[i*16 +: 16]) !== $signed(expected[i])) begin
          $display("FAIL y[%0d] = %0d, expected %0d", i, $signed(y[i*16 +: 16]), $signed(expected[i]));
          errors = errors + 1;
        end
      end
    end
    if (errors == 0) $display("PASS after %0d cycles", cycles);
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
