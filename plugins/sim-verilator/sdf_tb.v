// SDF-annotated gate-level testbench (Icarus Verilog): reset, a STATUS read, and
// a START with an illegal position that must set the error bit. Supplementary
// timing evidence (FR-VER-02/03): only counts with complete back-annotation.
`timescale 1ns/1ps
module sdf_tb;
  reg clk = 1'b0, rst_n = 1'b0, sclk = 1'b0, cs_n = 1'b1, mosi = 1'b0;
  wire miso, busy, kv_sclk, kv_cs_n, kv_mosi;
  wire kv_miso = 1'b0;
  integer errors = 0, k;
  reg [39:0] status;
  always #(`PERIOD / 2.0) clk = ~clk;
  chip_top u_chip (.clk(clk), .rst_n(rst_n), .host_sclk(sclk), .host_cs_n(cs_n), .host_mosi(mosi), .host_miso(miso),
                   .busy(busy), .kv_sclk(kv_sclk), .kv_cs_n(kv_cs_n), .kv_mosi(kv_mosi), .kv_miso(kv_miso));
  initial $sdf_annotate(`SDF_FILE, u_chip);
  task frame(input integer n, input [47:0] bits);
    begin
      cs_n = 1'b0;
      repeat (4) @(posedge clk);
      for (k = n - 1; k >= 0; k = k - 1) begin
        sclk = 1'b0; mosi = bits[k];
        repeat (4) @(posedge clk);
        status = {status[38:0], miso};
        sclk = 1'b1;
        repeat (4) @(posedge clk);
      end
      sclk = 1'b0;
      repeat (4) @(posedge clk);
      cs_n = 1'b1;
      repeat (8) @(posedge clk);
    end
  endtask
  initial begin
    repeat (8) @(posedge clk);
    rst_n = 1'b1;
    repeat (8) @(posedge clk);
    frame(40, {8'h20, 32'd0});
    if (status[31] !== 1'b0) begin errors = errors + 1; $display("SDFTB busy after reset"); end
    frame(48, {8'h10, 16'd1, 16'd60000, 8'd1});  // position beyond the context
    repeat (64) @(posedge clk);
    frame(40, {8'h20, 32'd0});
    if (status[30] !== 1'b1) begin errors = errors + 1; $display("SDFTB error bit not set"); end
    $display("SDFTB errors %0d", errors);
    $finish;
  end
endmodule
