`timescale 1ns/1ps
module qual_tb;
  reg clk = 1'b0;
  reg rst = 1'b1;
  reg signed [15:0] a = 16'sd0, b = 16'sd0;
  wire signed [31:0] acc, y;
  integer i, ta, tb;
  qual dut (.clk(clk), .rst(rst), .a(a), .b(b), .acc(acc), .y(y));
  initial begin
    #1 clk = 1'b1;
    #1 clk = 1'b0;
    rst = 1'b0;
    for (i = 0; i < 96; i = i + 1) begin
      ta = (i * 977 + 13) % 65536 - 32768;
      tb = (i * 611 + 7) % 65536 - 32768;
      a = ta[15:0];
      b = tb[15:0];
      #1 clk = 1'b1;
      #1 clk = 1'b0;
      $display("Q %0d %0d %0d", i, acc, y);
    end
    $finish;
  end
endmodule
