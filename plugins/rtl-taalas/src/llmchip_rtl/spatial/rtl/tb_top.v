// Testbench top of the spatial engine: the chip (RTL `llmchip_chip`, or with GATE the
// gate-level netlist of the padded chip top). tb.cpp drives the host SPI interface and
// reads the observation points with the host READ command (no backdoor). The ports
// bd_addr / bd_data / pc / trace_start keep the testbench interface of the bundle format
// and are unused.
`timescale 1ns/1ps
module tb_top (
  input  wire        clk,
  input  wire        rst_n,
  input  wire        host_sclk,
  input  wire        host_cs_n,
  input  wire        host_mosi,
  output wire        host_miso,
  output wire        busy,
  /* verilator lint_off UNUSEDSIGNAL */
  input  wire [31:0] bd_addr,
  /* verilator lint_on UNUSEDSIGNAL */
  output wire [31:0] bd_data,
  output wire [31:0] pc,
  output wire        trace_start
);
`ifdef GATE
  `GATE_TOP u_chip (
`else
  llmchip_chip u_chip (
`endif
    .clk(clk), .rst_n(rst_n), .host_sclk(host_sclk), .host_cs_n(host_cs_n), .host_mosi(host_mosi),
    .host_miso(host_miso), .busy(busy));
  assign bd_data = 32'd0;
  assign pc = 32'd0;
  assign trace_start = 1'b0;
endmodule
