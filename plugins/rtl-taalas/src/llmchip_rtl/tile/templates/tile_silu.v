// SiLU gate of taalas-tile/1: s = rshift(h1 * sigmoid_lut[clipped index], 15); g = rshift(s * h3,
// F1 + F3 - FG), saturated to int16. The sigmoid table is the numeric spec's, 16 steps per unit
// over [-SIG_RANGE, SIG_RANGE).
`default_nettype none

module tile_silu #(
  parameter integer DIM = 64,
  parameter integer F1 = 15,
  parameter integer F3 = 15,
  parameter integer FG = 19,
  parameter integer SIG_RANGE = 8,
  parameter integer SIG_STEPS = 16,
  parameter         INIT = "sigmoid.hex"
) (
  input  wire signed [DIM*16-1:0] h1,
  input  wire signed [DIM*16-1:0] h3,
  output wire signed [DIM*16-1:0] g
);

  localparam integer SHIFT = F1 + F3 - FG;
  localparam signed [31:0] LIMIT = (SIG_RANGE << F1) - 1;
  localparam integer ENTRIES = 2 * SIG_RANGE * SIG_STEPS;

  reg [15:0] sigmoid [0:ENTRIES-1];
  initial begin
    $readmemh(INIT, sigmoid);
  end

  genvar lane;
  generate
    for (lane = 0; lane < DIM; lane = lane + 1) begin : g_lane
      wire signed [15:0] a = h1[lane*16 +: 16];
      wire signed [15:0] b = h3[lane*16 +: 16];
      wire signed [31:0] a32 = {{16{a[15]}}, a};
      wire signed [31:0] z32 = (a32 > LIMIT) ? LIMIT :
                              (a32 < -LIMIT - 1) ? (-LIMIT - 1) : a32;
      wire signed [31:0] idx32 = (z32 >>> (F1 - 4)) + SIG_RANGE * SIG_STEPS;
      wire        [7:0]  idx8 = (idx32 < 32'sd0) ? 8'd0 : (idx32 > 32'sd255) ? 8'd255 : idx32[7:0];
      wire        [15:0] sig = sigmoid[idx8];
      wire signed [31:0] s_prod = a * $signed({1'b0, sig});
      wire signed [31:0] s = (s_prod + (1 << 14)) >>> 15;
      wire signed [63:0] s64 = {{32{s[31]}}, s};
      wire signed [63:0] b64 = {{48{b[15]}}, b};
      wire signed [63:0] prod = s64 * b64;
      wire signed [63:0] rounded = (prod + (64'sd1 <<< (SHIFT - 1))) >>> SHIFT;
      assign g[lane*16 +: 16] = (rounded > 64'sd32767) ? 16'sh7fff :
                                (rounded < -64'sd32768) ? 16'sh8000 : rounded[15:0];
    end
  endgenerate

endmodule
