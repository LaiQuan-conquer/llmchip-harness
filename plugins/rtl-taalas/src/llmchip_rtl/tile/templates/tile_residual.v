// The residual add of taalas-tile/1: x + o in the residual stream's int16 format, saturated.
`default_nettype none

module tile_residual #(
  parameter integer DIM = 32,
  parameter integer WIDTH = 16
) (
  input  wire signed [DIM*WIDTH-1:0] a,
  input  wire signed [DIM*WIDTH-1:0] b,
  output wire signed [DIM*WIDTH-1:0] y
);

  genvar lane;
  generate
    for (lane = 0; lane < DIM; lane = lane + 1) begin : g_lane
      wire signed [WIDTH:0] sum = $signed(a[lane*WIDTH +: WIDTH]) + $signed(b[lane*WIDTH +: WIDTH]);
      assign y[lane*WIDTH +: WIDTH] = (sum > (1 << (WIDTH - 1)) - 1) ? (1 << (WIDTH - 1)) - 1 :
                                      (sum < -(1 << (WIDTH - 1))) ? -(1 << (WIDTH - 1)) : sum[WIDTH-1:0];
    end
  endgenerate

endmodule
