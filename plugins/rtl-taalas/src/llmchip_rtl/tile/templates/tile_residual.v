// The residual add of taalas-tile/1: a + b, saturated to the residual stream's width.
// The numeric spec widens the residual stream (16 or 24 bits) while the operator outputs that
// feed it stay 16 bits, so the two operands and the result carry their own widths.
`default_nettype none

module tile_residual #(
  parameter integer DIM = 32,
  parameter integer WIDTH_A = 16,
  parameter integer WIDTH_B = 16,
  parameter integer WIDTH = 16
) (
  input  wire signed [DIM*WIDTH_A-1:0] a,
  input  wire signed [DIM*WIDTH_B-1:0] b,
  output wire signed [DIM*WIDTH-1:0] y
);

  localparam signed [31:0] LIMIT_HI = (1 << (WIDTH - 1)) - 1;
  localparam signed [31:0] LIMIT_LO = -(1 << (WIDTH - 1));

  genvar lane;
  generate
    for (lane = 0; lane < DIM; lane = lane + 1) begin : g_lane
      wire signed [31:0] a32 = {{(32-WIDTH_A){a[lane*WIDTH_A + WIDTH_A - 1]}},
                                a[lane*WIDTH_A +: WIDTH_A]};
      wire signed [31:0] b32 = {{(32-WIDTH_B){b[lane*WIDTH_B + WIDTH_B - 1]}},
                                b[lane*WIDTH_B +: WIDTH_B]};
      wire signed [31:0] sum = a32 + b32;
      wire signed [31:0] hi = LIMIT_HI;
      wire signed [31:0] lo = LIMIT_LO;
      assign y[lane*WIDTH +: WIDTH] = (sum > hi) ? hi[WIDTH-1:0] :
                                      (sum < lo) ? lo[WIDTH-1:0] : sum[WIDTH-1:0];
    end
  endgenerate

endmodule
