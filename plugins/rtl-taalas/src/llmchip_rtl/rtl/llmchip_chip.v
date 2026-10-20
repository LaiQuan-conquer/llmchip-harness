// Chip function without the pad ring: host interface, engine and external
// memory port. The process-specific top (pads) wraps this module in S6.
//   kv_oe: the chip drives the memory bus only while busy; when idle the host
//   may read the external memory (observation points, debug readback).
//   MEM_SQI: the memory port is quad SPI on four bidirectional lines kv_sio0..3
//   (output, output enable and input of each), driven only while the port sends.
`timescale 1ns/1ps
`include "llmchip_params.vh"
module llmchip_chip (
  input  wire clk,
  input  wire rst_n,
  input  wire host_sclk,
  input  wire host_cs_n,
  input  wire host_mosi,
  output wire host_miso,
  output wire busy,
  output wire kv_sclk,
  output wire kv_cs_n,
`ifdef MEM_SQI
  output wire kv_sio0_o, output wire kv_sio0_oe, input wire kv_sio0_i,
  output wire kv_sio1_o, output wire kv_sio1_oe, input wire kv_sio1_i,
  output wire kv_sio2_o, output wire kv_sio2_oe, input wire kv_sio2_i,
  output wire kv_sio3_o, output wire kv_sio3_oe, input wire kv_sio3_i,
`else
  output wire kv_mosi,
  input  wire kv_miso,
`endif
  output wire kv_oe
);
  // rst_n is asynchronous to clk like the host inputs: two-flop synchronizer, then a
  // synchronous reset (the clock runs during reset; no flop has an asynchronous reset).
  reg [1:0] rst_sync;
  always @(posedge clk) rst_sync <= {rst_sync[0], rst_n};  // X until the first two clocks of reset
  wire rst_ok = rst_sync[1];

  wire start, done, error, mem_req, mem_we, mem_ack;
  // Observed by the testbench only.
  /* verilator lint_off UNUSEDSIGNAL */
  wire trace_start;
  wire [`PCW-1:0] pc;
  /* verilator lint_on UNUSEDSIGNAL */
  wire [15:0] token, pos, token_out;
  wire [1:0] mode;
  wire [`AW-1:0] mem_addr;
  wire [31:0] mem_wdata, mem_rdata;

  llmchip_host u_host (
    .clk(clk), .rst_n(rst_ok), .sclk_i(host_sclk), .cs_n_i(host_cs_n), .mosi_i(host_mosi), .miso(host_miso),
    .start(start), .token(token), .pos(pos), .mode(mode), .busy(busy), .error(error), .done(done), .token_out(token_out));
  llmchip_core u_core (
    .clk(clk), .rst_n(rst_ok), .start(start), .token_in(token), .pos_in(pos), .mode(mode),
    .busy(busy), .done(done), .error(error), .token_out(token_out),
    .mem_req(mem_req), .mem_we(mem_we), .mem_addr(mem_addr), .mem_wdata(mem_wdata), .mem_ack(mem_ack),
    .mem_rdata(mem_rdata), .pc(pc), .trace_start(trace_start));
`ifdef MEM_SQI
  wire [3:0] sio_o, sio_oe;
  llmchip_sqimem u_mem (
    .clk(clk), .rst_n(rst_ok), .hold(busy), .req(mem_req), .we(mem_we), .addr(mem_addr), .wdata(mem_wdata),
    .ack(mem_ack), .rdata(mem_rdata), .sclk(kv_sclk), .cs_n(kv_cs_n), .sio_o(sio_o), .sio_oe(sio_oe),
    .sio_i({kv_sio3_i, kv_sio2_i, kv_sio1_i, kv_sio0_i}));
  assign {kv_sio3_o, kv_sio2_o, kv_sio1_o, kv_sio0_o} = sio_o;
  assign {kv_sio3_oe, kv_sio2_oe, kv_sio1_oe, kv_sio0_oe} = sio_oe;
`else
  llmchip_spimem u_mem (
    .clk(clk), .rst_n(rst_ok), .hold(busy), .req(mem_req), .we(mem_we), .addr(mem_addr), .wdata(mem_wdata),
    .ack(mem_ack), .rdata(mem_rdata), .sclk(kv_sclk), .cs_n(kv_cs_n), .mosi(kv_mosi), .miso(kv_miso));
`endif
  // The port also drives SCK and CS while it brings the SRAM into SQI mode after reset.
  assign kv_oe = busy | !kv_cs_n;
endmodule
