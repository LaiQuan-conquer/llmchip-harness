// Testbench top: the chip (RTL `llmchip_chip`, or with GATE the gate-level
// netlist of the padded chip top) connected to an SPI SRAM model. The host
// side is driven by tb.cpp.
`timescale 1ns/1ps
`include "llmchip_params.vh"
module tb_top (
  input  wire        clk,
  input  wire        rst_n,
  input  wire        host_sclk,
  input  wire        host_cs_n,
  input  wire        host_mosi,
  output wire        host_miso,
  output wire        busy,
  input  wire [31:0] bd_addr,
  output wire [31:0] bd_data,
  output wire [31:0] pc,
  output wire        trace_start
);
`ifdef MEM_SQI
  // Quad SPI: the bus value of each line is the chip's output while it drives it, else the
  // SRAM's while it drives it, else the board's pull-up.
  wire kv_sclk, kv_cs_n, kv_oe, sram_oe;
  wire [3:0] c_o, c_oe, s_o;
  wire [3:0] sio = (c_o & c_oe) | (s_o & ~c_oe & {4{sram_oe}}) | (~c_oe & ~{4{sram_oe}});
  llmchip_chip u_chip (
    .clk(clk), .rst_n(rst_n), .host_sclk(host_sclk), .host_cs_n(host_cs_n), .host_mosi(host_mosi),
    .host_miso(host_miso), .busy(busy), .kv_sclk(kv_sclk), .kv_cs_n(kv_cs_n),
    .kv_sio0_o(c_o[0]), .kv_sio0_oe(c_oe[0]), .kv_sio0_i(sio[0]),
    .kv_sio1_o(c_o[1]), .kv_sio1_oe(c_oe[1]), .kv_sio1_i(sio[1]),
    .kv_sio2_o(c_o[2]), .kv_sio2_oe(c_oe[2]), .kv_sio2_i(sio[2]),
    .kv_sio3_o(c_o[3]), .kv_sio3_oe(c_oe[3]), .kv_sio3_i(sio[3]),
    .kv_oe(kv_oe));
  assign pc = u_chip.pc;
  assign trace_start = u_chip.trace_start;
  sqi_sram_model #(.BYTES(`SRAM_BYTES)) u_sram (
    .sclk(kv_sclk), .cs_n(kv_cs_n), .sio_i(sio), .sio_o(s_o), .sio_oe(sram_oe), .bd_addr(bd_addr), .bd_data(bd_data));
`else
  wire kv_sclk, kv_cs_n, kv_mosi, kv_miso;
`ifdef GATE
  // Pad pins of the gate-level chip; observation of pc / trace_start is not
  // available at this level.
  `GATE_TOP u_chip (
    .clk(clk), .rst_n(rst_n), .host_sclk(host_sclk), .host_cs_n(host_cs_n), .host_mosi(host_mosi),
    .host_miso(host_miso), .busy(busy), .kv_sclk(kv_sclk), .kv_cs_n(kv_cs_n), .kv_mosi(kv_mosi), .kv_miso(kv_miso));
  assign pc = 32'd0;
  assign trace_start = 1'b0;
`else
  wire kv_oe;
  llmchip_chip u_chip (
    .clk(clk), .rst_n(rst_n), .host_sclk(host_sclk), .host_cs_n(host_cs_n), .host_mosi(host_mosi),
    .host_miso(host_miso), .busy(busy), .kv_sclk(kv_sclk), .kv_cs_n(kv_cs_n), .kv_mosi(kv_mosi),
    .kv_miso(kv_miso), .kv_oe(kv_oe));
  assign pc = u_chip.pc;
  assign trace_start = u_chip.trace_start;
`endif
  spi_sram_model #(.BYTES(`SRAM_BYTES)) u_sram (
    .sclk(kv_sclk), .cs_n(kv_cs_n), .mosi(kv_mosi), .miso(kv_miso), .bd_addr(bd_addr), .bd_data(bd_data));
`endif
endmodule
