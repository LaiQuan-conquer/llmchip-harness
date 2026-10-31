// SiLU gate of taalas-tile/1 (clocked, enabled): s = rshift(h1 * sigmoid_lut[clipped index], 15);
// g = rshift(s * h3, F1 + F3 - FG), saturated to int16. The lanes compute only while `en` is high.
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
  input  wire                        clk,
  input  wire                        rst_n,
  input  wire                        en,
  input  wire signed [DIM*16-1:0]    h1,
  input  wire signed [DIM*16-1:0]    h3,
  output wire signed [DIM*16-1:0]    g
);

  localparam integer SHIFT = F1 + F3 - FG;
  localparam signed [31:0] LIMIT = (SIG_RANGE << F1) - 1;
  localparam integer ENTRIES = 2 * SIG_RANGE * SIG_STEPS;

  reg [15:0] sigmoid [0:ENTRIES-1];
  initial begin
    $readmemh(INIT, sigmoid);
  end

  function automatic signed [31:0] silu_z(input signed [15:0] a);
    silu_z = ($signed(a) > LIMIT) ? LIMIT :
             ($signed(a) < -LIMIT - 1) ? (-LIMIT - 1) : $signed(a);
  endfunction

  function automatic [7:0] silu_index(input signed [15:0] a);
    silu_index = ((silu_z(a) >>> (F1 - 4)) + SIG_RANGE * SIG_STEPS < 0) ? 8'd0 :
                 ((silu_z(a) >>> (F1 - 4)) + SIG_RANGE * SIG_STEPS > 255) ? 8'd255 :
                 (silu_z(a) >>> (F1 - 4)) + SIG_RANGE * SIG_STEPS;
  endfunction

  function automatic signed [31:0] silu_s(input signed [15:0] a, input [15:0] sig);
    silu_s = (($signed({{16{a[15]}}, a}) * $signed({16'd0, sig})) + 32'sd16384) >>> 15;
  endfunction

  function automatic signed [63:0] silu_prod(input signed [15:0] a, input signed [15:0] b,
                                             input [15:0] sig);
    silu_prod = $signed(silu_s(a, sig)) * $signed({{48{b[15]}}, b});
  endfunction

  function automatic signed [63:0] silu_round(input signed [15:0] a, input signed [15:0] b,
                                              input [15:0] sig);
    silu_round = (silu_prod(a, b, sig) + (64'sd1 <<< (SHIFT - 1))) >>> SHIFT;
  endfunction

  function automatic signed [15:0] silu_lane(input signed [15:0] a, input signed [15:0] b,
                                             input [15:0] sig);
    silu_lane = (silu_round(a, b, sig) > 64'sd32767) ? 16'sh7fff :
                (silu_round(a, b, sig) < -64'sd32768) ? 16'sh8000 : silu_round(a, b, sig);
  endfunction

  reg signed [DIM*16-1:0] g_r;
  integer lane;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      g_r <= {DIM*16{1'b0}};
    end else if (en) begin
      for (lane = 0; lane < DIM; lane = lane + 1) begin
        g_r[lane*16 +: 16] <= silu_lane(h1[lane*16 +: 16], h3[lane*16 +: 16],
                                        sigmoid[silu_index(h1[lane*16 +: 16])]);
      end
    end
  end

  assign g = g_r;

endmodule
