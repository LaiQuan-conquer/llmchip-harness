// Integer RMSNorm of taalas-tile/1 (requirements FR-RTL-01, the arithmetic of the numeric spec):
// v = (sum x_i^2 / DIM) + eps, clamped at 1; the even exponent e2 = floor(log2 v) rounded down to
// even; t = v scaled to a 6-bit mantissa; rsqrt from the Q15 table; y_i = narrow(rshift(x_i * w_i
// * r, fn + 15 + e2/2 - fo)). One multiplier and one accumulator are reused over DIM cycles.
`default_nettype none

module tile_rmsnorm #(
  parameter integer DIM = 8,
  parameter integer LOG2DIM = 3,
  parameter signed [7:0]  FN = 8'sd14,
  parameter signed [7:0]  FO = 8'sd8,
  parameter signed [63:0] EPS_Q = 64'sd1,
  parameter         INIT = "rsqrt.hex"
) (
  input  wire                     clk,
  input  wire                     rst_n,
  input  wire                     start,
  input  wire signed [DIM*16-1:0] x,
  input  wire signed [DIM*16-1:0] weight,
  output reg  signed [DIM*16-1:0] y,
  output reg                      done
);

  localparam [2:0] IDLE = 3'd0, SUM = 3'd1, PREP = 3'd2, PREP2 = 3'd3, LUT = 3'd4, SCALE0 = 3'd5, SCALE = 3'd6, DONE = 3'd7;
  reg [2:0]               state;
  reg [31:0]              i;
  reg signed [63:0]       sumsq;
  reg signed [63:0]       v;
  reg signed [7:0]        e2;
  reg signed [7:0]        shift;
  reg signed [15:0]       r;
  reg [7:0]               index;
  reg                     lut_en;
  reg signed [DIM*16-1:0] x_q;
  reg signed [DIM*16-1:0] w_q;

  wire signed [15:0] lut_value;

  tile_lut #(.WIDTH(16), .DEPTH(192), .ADDR(8), .INIT(INIT)) rsqrt (
    .clk(clk), .rst_n(rst_n), .en(lut_en), .index(index), .value(lut_value)
  );

  // floor(log2 v) of the registered v: the highest set bit.
  reg [6:0] msb;
  integer b;
  always @(*) begin
    msb = 7'd0;
    for (b = 63; b >= 0; b = b - 1) begin
      if (v[b]) msb = b[6:0];
    end
  end

  wire signed [15:0] x_i = x_q[i*16 +: 16];
  wire signed [15:0] w_i = w_q[i*16 +: 16];
  wire signed [31:0] sq = x_i * x_i;
  wire signed [63:0] sq_ext = {{32{sq[31]}}, sq};
  wire signed [63:0] product = x_i * w_i * r;
  wire signed [7:0]  half = {e2[7], e2[7:1]};  // e2 / 2, e2 is even
  wire signed [63:0] rounded = (shift > 8'sd0) ? ((product + (64'sd1 <<< (shift - 8'sd1))) >>> shift) : (product <<< (-shift));
  wire signed [15:0] clamped = (rounded > 64'sd32767) ? 16'sh7fff : ((rounded < -64'sd32768) ? 16'sh8000 : rounded[15:0]);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state  <= IDLE;
      i      <= 32'd0;
      sumsq  <= 64'sd0;
      v      <= 64'sd1;
      e2     <= 8'sd0;
      shift  <= 8'sd0;
      r      <= 16'sd0;
      index  <= 8'd0;
      lut_en <= 1'b0;
      x_q    <= {DIM*16{1'b0}};
      w_q    <= {DIM*16{1'b0}};
      y      <= {DIM*16{1'b0}};
      done   <= 1'b0;
    end else begin
      lut_en <= 1'b0;
      case (state)
        IDLE: begin
          done <= 1'b0;
          if (start) begin
            x_q   <= x;
            w_q   <= weight;
            i     <= 32'd0;
            sumsq <= 64'sd0;
            state <= SUM;
          end
        end
        SUM: begin
          sumsq <= sumsq + sq_ext;
          if (i == DIM - 1) begin
            state <= PREP;
          end else begin
            i <= i + 32'd1;
          end
        end
        PREP: begin
          v     <= ((sumsq >>> LOG2DIM) + EPS_Q < 64'sd1) ? 64'sd1 : (sumsq >>> LOG2DIM) + EPS_Q;
          state <= PREP2;
        end
        PREP2: begin
          e2    <= {1'b0, msb} - {7'd0, msb[0]};
          state <= LUT;
        end
        LUT: begin
          index  <= ((e2 >= 8'sd6) ? v[(e2[5:0] - 6'd6) +: 8] : (v[7:0] << (6'd6 - {3'd0, e2[2:0]}))) - 8'd64;
          lut_en <= 1'b1;
          state  <= SCALE0;
        end
        SCALE0: begin
          r     <= lut_value;
          shift <= FN + 8'sd15 + half - FO;
          i     <= 32'd0;
          state <= SCALE;
        end
        SCALE: begin
          y[i*16 +: 16] <= clamped;
          if (i == DIM - 1) begin
            state <= DONE;
          end else begin
            i <= i + 32'd1;
          end
        end
        DONE: begin
          done  <= 1'b1;
          state <= IDLE;
        end
        default: state <= IDLE;
      endcase
    end
  end

endmodule
