// RoPE of taalas-tile/1 (requirements FR-RTL-01, the numeric spec's Q14 tables): for one token at
// position p, each head's pair (a, b) becomes (a*cos - b*sin, b*cos + a*sin) >> 14 with half-up
// rounding and saturation. STYLE 0 rotates element j with j + HD/2 (Hugging Face), STYLE 1 the
// interleaved pairs (llama2.c). One pair per two cycles: the table read, then the products.
`default_nettype none

module tile_rope #(
  parameter integer HEADS = 2,
  parameter integer HD = 8,
  parameter integer STYLE = 0,
  parameter integer LOG2HD = 3,
  parameter integer LOG2HEADS = 1,
  parameter integer ADDR = 16,
  parameter         COS_INIT = "rope_cos.hex",
  parameter         SIN_INIT = "rope_sin.hex"
) (
  input  wire                      clk,
  input  wire                      rst_n,
  input  wire                      start,
  input  wire [15:0]               position,
  input  wire signed [HEADS*HD*16-1:0] x,
  output reg  signed [HEADS*HD*16-1:0] y,
  output reg                       done
);

  localparam [15:0] HALF = 16'd1 << (LOG2HD - 1);
  localparam [15:0] HEADS_M1 = (16'd1 << LOG2HEADS) - 16'd1;

  localparam [2:0] IDLE = 3'd0, REQ = 3'd1, WAIT = 3'd2, APPLY = 3'd3, DONE = 3'd4;

  reg [2:0]               state;
  reg [15:0]              head;
  reg [15:0]              pair;
  reg signed [HEADS*HD*16-1:0] x_q;
  reg [ADDR-1:0]          index;
  reg                     cos_en;
  reg                     sin_en;

  wire signed [15:0] cos_value;
  wire signed [15:0] sin_value;

  tile_lut #(.WIDTH(16), .DEPTH(1 << ADDR), .ADDR(ADDR), .INIT(COS_INIT)) cos_table (
    .clk(clk), .rst_n(rst_n), .en(cos_en), .index(index), .value(cos_value)
  );

  tile_lut #(.WIDTH(16), .DEPTH(1 << ADDR), .ADDR(ADDR), .INIT(SIN_INIT)) sin_table (
    .clk(clk), .rst_n(rst_n), .en(sin_en), .index(index), .value(sin_value)
  );

  wire [15:0] base = head << LOG2HD;
  wire [15:0] lane_a = (STYLE == 0) ? (base + pair) : (base + (pair << 1));
  wire [15:0] lane_b = (STYLE == 0) ? (lane_a + HALF) : (lane_a + 16'd1);

  wire signed [15:0] a = x_q[lane_a*16 +: 16];
  wire signed [15:0] b = x_q[lane_b*16 +: 16];
  wire signed [31:0] ac = a * cos_value;
  wire signed [31:0] bs = b * sin_value;
  wire signed [31:0] bc = b * cos_value;
  wire signed [31:0] as = a * sin_value;
  wire signed [32:0] diff = {ac[31], ac} - {bs[31], bs};
  wire signed [32:0] sum = {bc[31], bc} + {as[31], as};
  wire signed [32:0] out_a = (diff + 33'sd8192) >>> 14;
  wire signed [32:0] out_b = (sum + 33'sd8192) >>> 14;
  wire signed [15:0] sat_a = (out_a > 33'sd32767) ? 16'sh7fff : ((out_a < -33'sd32768) ? 16'sh8000 : out_a[15:0]);
  wire signed [15:0] sat_b = (out_b > 33'sd32767) ? 16'sh7fff : ((out_b < -33'sd32768) ? 16'sh8000 : out_b[15:0]);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state  <= IDLE;
      head   <= 16'd0;
      pair   <= 16'd0;
      x_q    <= {HEADS*HD{1'b0}};
      index  <= {ADDR{1'b0}};
      cos_en <= 1'b0;
      sin_en <= 1'b0;
      y      <= {HEADS*HD{1'b0}};
      done   <= 1'b0;
    end else begin
      cos_en <= 1'b0;
      sin_en <= 1'b0;
      case (state)
        IDLE: begin
          done <= 1'b0;
          if (start) begin
            x_q   <= x;
            head  <= 16'd0;
            pair  <= 16'd0;
            state <= REQ;
          end
        end
        REQ: begin
          index  <= position * HALF + pair;
          cos_en <= 1'b1;
          sin_en <= 1'b1;
          state  <= WAIT;
        end
        WAIT: begin
          state <= APPLY;
        end
        APPLY: begin
          y[lane_a*16 +: 16] <= sat_a;
          y[lane_b*16 +: 16] <= sat_b;
          if (pair == HALF - 16'd1) begin
            pair <= 16'd0;
            if (head == HEADS_M1) begin
              state <= DONE;
            end else begin
              head  <= head + 16'd1;
              state <= REQ;
            end
          end else begin
            pair  <= pair + 16'd1;
            state <= REQ;
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
