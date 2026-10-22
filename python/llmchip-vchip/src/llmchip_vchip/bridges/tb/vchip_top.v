// Testbench top of the virtual-chip test harness: the chip function of an RTL bundle (its
// `top` module, `llmchip_chip` by default) with nothing between it and the harness's pin-level
// testbench (vchip_tb.cpp). It belongs to the harness, not to the bundle, so the chip under test
// cannot shape what the testbench observes. Ports: the chip's pins only.
`timescale 1ns/1ps
`ifndef CHIP_TOP
`define CHIP_TOP llmchip_chip
`endif
module tb_top (
  input  wire clk,
  input  wire rst_n,
  input  wire host_sclk,
  input  wire host_cs_n,
  input  wire host_mosi,
  output wire host_miso,
  output wire busy
);
  `CHIP_TOP u_chip (
    .clk(clk), .rst_n(rst_n), .host_sclk(host_sclk), .host_cs_n(host_cs_n), .host_mosi(host_mosi),
    .host_miso(host_miso), .busy(busy));
endmodule
