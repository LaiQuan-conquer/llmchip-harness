// Chip function of the spatial engine without the pad ring: the host interface and the
// engine. There is no external memory: weights are circuits, the KV cache is on chip.
// The process-specific top (pads) wraps this module in S6.
`timescale 1ns/1ps
module llmchip_chip (
  input  wire clk,
  input  wire rst_n,
  input  wire host_sclk,
  input  wire host_cs_n,
  input  wire host_mosi,
  output wire host_miso,
  output wire busy
);
  // rst_n is asynchronous to clk like the host inputs: two-flop synchronizer, then a
  // synchronous reset (the clock runs during reset; no flop has an asynchronous reset).
  reg [1:0] rst_sync;
  always @(posedge clk) rst_sync <= {rst_sync[0], rst_n};  // X until the first two clocks of reset
  wire rst_ok = rst_sync[1];

  wire start, done, error;
  wire [15:0] token, pos, token_out;
  wire [10:0] obs_sel;
  wire [31:0] obs_word;
  sp_host u_host (
    .clk(clk), .rst_n(rst_ok), .sclk_i(host_sclk), .cs_n_i(host_cs_n), .mosi_i(host_mosi), .miso(host_miso),
    .start(start), .token(token), .pos(pos), .busy(busy), .error(error), .done(done), .token_out(token_out),
    .obs_sel(obs_sel), .obs_word(obs_word));
  sp_core u_core (
    .clk(clk), .rst_n(rst_ok), .start(start), .token_in(token), .pos_in(pos), .obs_sel(obs_sel),
    .obs_word(obs_word), .busy(busy), .done(done), .error(error), .token_out(token_out));
endmodule
