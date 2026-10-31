// Pins of the generated tile chip under the virtual-chip harness: the host side of `tile_chip`
// is parallel (start, token, length in; busy, done, next_token out), so the testbench drives
// those pins directly. One STEP = one token; cycles are counted on the busy pin.
`default_nettype none

module tb_top (
  input  wire        clk,
  input  wire        rst_n,
  input  wire        start,
  input  wire [23:0] token,
  input  wire [15:0] length,
  output wire        busy,
  output wire        done,
  output wire [23:0] next_token
);

  tile_chip dut (
    .clk(clk), .rst_n(rst_n), .start(start), .token(token), .length(length),
    .busy(busy), .done(done), .next_token(next_token),
    .obs_addr(16'd0), .obs_word(), .dbg_sig(), .dbg_sum(), .dbg_valid()
  );

endmodule
