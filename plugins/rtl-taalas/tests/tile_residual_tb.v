// Testbench for tile_residual.
`timescale 1ns/1ps
`default_nettype none

module tile_residual_tb;
  parameter integer DIM = 32;
  parameter integer WIDTH = 16;

  reg signed [DIM*WIDTH-1:0] a = {DIM*WIDTH{1'b0}};
  reg signed [DIM*WIDTH-1:0] b = {DIM*WIDTH{1'b0}};
  wire signed [DIM*WIDTH-1:0] y;

  reg [DIM*WIDTH-1:0] amem [0:0];
  reg [DIM*WIDTH-1:0] bmem [0:0];
  reg [15:0] expected [0:DIM-1];
  integer i;
  integer errors;

  tile_residual #(.DIM(DIM), .WIDTH(WIDTH)) dut (.a(a), .b(b), .y(y));

  initial begin
    errors = 0;
    $readmemh("a.hex", amem);
    $readmemh("b.hex", bmem);
    $readmemh("expected.hex", expected);
    a = amem[0];
    b = bmem[0];
    #1;
    for (i = 0; i < DIM; i = i + 1) begin
      if ($signed(y[i*16 +: 16]) !== $signed(expected[i])) begin
        $display("FAIL y[%0d] = %0d, expected %0d", i, $signed(y[i*16 +: 16]), $signed(expected[i]));
        errors = errors + 1;
      end
    end
    if (errors == 0) $display("PASS");
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
