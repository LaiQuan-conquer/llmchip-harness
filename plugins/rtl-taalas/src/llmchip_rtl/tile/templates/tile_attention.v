// The attention pass of taalas-tile/1 (drafts/tile-180nm-pipeline.md §12.1) for one query token.
//
// One head at a time, with a single 17x17 multiplier shared by every stage:
//   DOT  - HD cycles per cached position: dot = q_head . k_j, then
//          score_j = rshift(dot * ATTN_SCALE, FQ + FK + 15 - SCORE_FRAC);
//   EXP  - the exp table read is registered, so each position takes an issue cycle and a wait
//          cycle; d_j = min(smax - score_j, EXP_RANGE * 2^SCORE_FRAC - 1);
//   DIV  - one restoring step per cycle: p_j = (e_j << 15) / sum(e), the remainder shifted left
//          and compared against the divisor;
//   WSUM - HD accumulators, p_j * v_j[i] reusing the multiplier;
//   EMIT - (sum p * v) >> 15, saturated to int16.
// The KV cache is read with a one-cycle latency: the address is issued, the data arrives in the
// next cycle and is held while the multiplier walks the HD lanes. The model's arithmetic is in
// llmchip_rtl.tile.attention and the comparison is tests/test_tile_attention.py.
`default_nettype none

module tile_attention #(
  parameter integer HEADS = 4,
  parameter integer KVHEADS = 2,
  parameter integer HD = 8,
  parameter integer LOG2CONTEXT = 3,
  parameter integer ADDR = 4,
  parameter integer FQ = 15,
  parameter integer FK = 15,
  parameter integer SCORE_FRAC = 10,
  parameter integer EXP_RANGE = 16,
  parameter integer ATTN_SCALE = 11585,
  parameter integer ACC = 48
) (
  input  wire                          clk,
  input  wire                          rst_n,
  input  wire                          start,
  input  wire [15:0]                   length,
  input  wire signed [HEADS*HD*16-1:0] q,
  output reg  signed [HEADS*HD*16-1:0] out,
  output reg                           out_valid,
  output reg                           done,
  output reg  [ADDR-1:0]               k_addr,
  input  wire signed [HD*16-1:0]       k_data,
  output reg  [ADDR-1:0]               v_addr,
  input  wire signed [HD*16-1:0]       v_data,
  output reg                           exp_en,
  output reg  [13:0]                   exp_index,
  input  wire [15:0]                   exp_value
);

  localparam integer MAXS = 1 << LOG2CONTEXT;
  localparam integer GROUP = HEADS / KVHEADS;
  localparam integer SHIFT = FQ + FK + 15 - SCORE_FRAC;
  localparam integer REMBITS = LOG2CONTEXT + 15;
  localparam signed [31:0] DMAX = EXP_RANGE * (1 << SCORE_FRAC) - 1;

  localparam [3:0] IDLE = 4'd0, HEAD = 4'd1, DOT_ADDR = 4'd2, DOT_WAIT = 4'd3,
                   DOT_ACC = 4'd4, EXP_ADDR = 4'd5, EXP_GET = 4'd6, DIV_INIT = 4'd7,
                   DIV_STEP = 4'd8, WSUM_ADDR = 4'd9, WSUM_WAIT = 4'd10, WSUM_ACC = 4'd11,
                   EMIT = 4'd12, NEXT = 4'd13, EXP_WAIT = 4'd14;

  reg [3:0]  state;
  reg [31:0] head;
  reg [31:0] kv_head;
  reg [31:0] head_in_group;
  reg [31:0] positions;
  reg [31:0] pos;
  reg [31:0] i;

  reg signed [15:0]  q_buf [0:HD-1];
  reg signed [31:0]  score_mem [0:MAXS-1];
  reg        [15:0]  e_mem [0:MAXS-1];
  reg        [15:0]  p_mem [0:MAXS-1];
  reg signed [ACC-1:0] acc [0:HD-1];

  reg signed [31:0]  smax;
  reg signed [ACC-1:0] dot;
  reg        [31:0]  sum_e;

  reg        [REMBITS-1:0] divisor;
  reg        [31:0]  dividend;
  reg        [REMBITS-1:0] rem;
  reg        [15:0]  quo;
  reg        [5:0]   div_count;

  integer k;

  // One multiplier for DOT and WSUM: the operands are selected by the stage. The p operand is
  // zero extended into 17 bits so that p = 32,768 (a single non-cancelled position) is positive.
  wire [31:0] lane = (i < HD) ? i : 32'd0;
  wire signed [16:0] mul_a = (state == WSUM_ACC) ? {1'b0, p_mem[pos]} : {q_buf[lane][15], q_buf[lane]};
  wire signed [16:0] mul_b = (state == WSUM_ACC) ? {v_data[lane*16 + 15], v_data[lane*16 +: 16]}
                                                 : {k_data[lane*16 + 15], k_data[lane*16 +: 16]};
  wire signed [33:0] mul_p = mul_a * mul_b;

  // The score: (dot * att_scale) rounded-shifted by SHIFT and saturated to int32 (graph_l2).
  wire signed [63:0] dot_ext   = {{(64-ACC){dot[ACC-1]}}, dot};
  wire signed [63:0] scaled    = dot_ext * $signed(ATTN_SCALE);
  wire signed [63:0] rounded   = scaled + (64'sd1 <<< (SHIFT - 1));
  wire signed [63:0] shifted   = rounded >>> SHIFT;
  wire signed [31:0] score_sat = (shifted > 64'sd2147483647) ? 32'sh7fffffff :
                                 (shifted < -64'sd2147483648) ? 32'sh80000000 : shifted[31:0];

  // The exp table index: d = smax - score, capped to EXP_RANGE * 2^SCORE_FRAC - 1. SCORE_FRAC is
  // the numeric spec's 10, so index = d >> 5 has 9 bits (0..511).
  wire signed [31:0] d_wide = smax - score_mem[pos];
  wire        [31:0] d_raw  = d_wide[31] ? 32'd0 : d_wide;
  wire        [13:0] d_cap  = (d_raw > DMAX) ? DMAX[13:0] : d_raw[13:0];
  wire        [13:0] d_index = d_cap >> (SCORE_FRAC - 5);

  // Restoring division: shift the remainder left, bring in the next dividend bit, compare.
  // rem < divisor <= sum(e) < MAXS * 2^15, so REMBITS holds both; the shifted remainder is one
  // bit wider, and the low REMBITS bits of the subtraction are exact when it is >= divisor.
  wire [REMBITS:0]   rem_next = {rem, dividend[31]};
  wire [REMBITS-1:0] rem_sub  = rem_next[REMBITS-1:0] - divisor;

  // WSUM and EMIT: p * v in the shared multiplier, then the rounding shift and int16 saturation.
  wire signed [ACC-1:0] acc_sum = acc[i] + {{(ACC-34){mul_p[33]}}, mul_p};
  wire signed [ACC-1:0] emit_rounded = acc[i] + 48'sd16384;
  wire signed [ACC-1:0] emit_shifted = emit_rounded >>> 15;
  wire signed [15:0] emit_sat = (emit_shifted > 48'sd32767) ? 16'sh7fff :
                                (emit_shifted < -48'sd32768) ? 16'sh8000 : emit_shifted[15:0];

  wire [ADDR-1:0] k_index = (kv_head[ADDR-1:0] << LOG2CONTEXT) + pos[ADDR-1:0];
  wire [ADDR-1:0] v_index = (kv_head[ADDR-1:0] << LOG2CONTEXT) + pos[ADDR-1:0];

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state        <= IDLE;
      head         <= 32'd0;
      kv_head      <= 32'd0;
      head_in_group <= 32'd0;
      positions    <= 32'd0;
      pos          <= 32'd0;
      i            <= 32'd0;
      smax         <= 32'sh80000000;
      dot          <= {ACC{1'b0}};
      sum_e        <= 32'd0;
      divisor      <= {REMBITS{1'b0}};
      dividend     <= 32'd0;
      rem          <= {REMBITS{1'b0}};
      quo          <= 16'd0;
      div_count    <= 6'd0;
      out          <= {HEADS*HD*16{1'b0}};
      out_valid    <= 1'b0;
      done         <= 1'b0;
      k_addr       <= {ADDR{1'b0}};
      v_addr       <= {ADDR{1'b0}};
      exp_en       <= 1'b0;
      exp_index    <= 14'd0;
      for (k = 0; k < HD; k = k + 1) begin
        q_buf[k] <= 16'sd0;
        acc[k]   <= {ACC{1'b0}};
      end
      for (k = 0; k < MAXS; k = k + 1) begin
        score_mem[k] <= 32'sd0;
        e_mem[k]     <= 16'd0;
        p_mem[k]     <= 16'd0;
      end
    end else begin
      out_valid <= 1'b0;
      done      <= 1'b0;
      exp_en    <= 1'b0;

      case (state)
        IDLE: begin
          if (start) begin
            positions    <= {16'd0, length} + 32'd1;
            head         <= 32'd0;
            kv_head      <= 32'd0;
            head_in_group <= 32'd0;
            state        <= HEAD;
          end
        end

        HEAD: begin
          for (k = 0; k < HD; k = k + 1) begin
            q_buf[k] <= q[head*HD*16 + k*16 +: 16];
            acc[k]   <= {ACC{1'b0}};
          end
          smax  <= 32'sh80000000;
          sum_e <= 32'd0;
          pos   <= 32'd0;
          i     <= 32'd0;
          state <= DOT_ADDR;
        end

        DOT_ADDR: begin
          k_addr <= k_index;
          state  <= DOT_WAIT;
        end

        DOT_WAIT: begin
          dot   <= {ACC{1'b0}};
          i     <= 32'd0;
          state <= DOT_ACC;
        end

        DOT_ACC: begin
          if (i < HD) begin
            dot <= dot + {{(ACC-34){mul_p[33]}}, mul_p};
            i   <= i + 32'd1;
          end else begin
            if (score_sat > smax) begin
              smax <= score_sat;
            end
            score_mem[pos] <= score_sat;
            if (pos + 32'd1 == positions) begin
              pos   <= 32'd0;
              state <= EXP_ADDR;
            end else begin
              pos   <= pos + 32'd1;
              state <= DOT_ADDR;
            end
          end
        end

        EXP_ADDR: begin
          exp_en    <= 1'b1;
          exp_index <= d_index;
          state     <= EXP_WAIT;
        end

        EXP_WAIT: begin
          state <= EXP_GET;
        end

        EXP_GET: begin
          e_mem[pos] <= exp_value;
          sum_e      <= sum_e + {16'd0, exp_value};
          if (pos + 32'd1 == positions) begin
            pos   <= 32'd0;
            state <= DIV_INIT;
          end else begin
            pos   <= pos + 32'd1;
            state <= EXP_ADDR;
          end
        end

        DIV_INIT: begin
          divisor   <= sum_e[REMBITS-1:0];
          dividend  <= {16'd0, e_mem[pos]} << 15;
          rem       <= {REMBITS{1'b0}};
          quo       <= 16'd0;
          div_count <= 6'd32;
          state     <= DIV_STEP;
        end

        DIV_STEP: begin
          if (div_count != 6'd0) begin
            dividend  <= dividend << 1;
            div_count <= div_count - 6'd1;
            if (rem_next >= {1'b0, divisor}) begin
              rem <= rem_sub;
              quo <= {quo[14:0], 1'b1};
            end else begin
              rem <= rem_next[REMBITS-1:0];
              quo <= {quo[14:0], 1'b0};
            end
          end else begin
            p_mem[pos] <= quo;
            if (pos + 32'd1 == positions) begin
              pos   <= 32'd0;
              i     <= 32'd0;
              state <= WSUM_ADDR;
            end else begin
              pos   <= pos + 32'd1;
              state <= DIV_INIT;
            end
          end
        end

        WSUM_ADDR: begin
          v_addr <= v_index;
          i      <= 32'd0;
          state  <= WSUM_WAIT;
        end

        WSUM_WAIT: begin
          i     <= 32'd0;
          state <= WSUM_ACC;
        end

        WSUM_ACC: begin
          if (i < HD) begin
            acc[i] <= acc_sum;
            i      <= i + 32'd1;
          end else begin
            if (pos + 32'd1 == positions) begin
              pos   <= 32'd0;
              i     <= 32'd0;
              state <= EMIT;
            end else begin
              pos   <= pos + 32'd1;
              i     <= 32'd0;
              state <= WSUM_ADDR;
            end
          end
        end

        EMIT: begin
          out[(head*HD + i)*16 +: 16] <= emit_sat;
          if (i + 32'd1 == HD) begin
            out_valid <= 1'b1;
            state     <= NEXT;
          end else begin
            i <= i + 32'd1;
          end
        end

        NEXT: begin
          if (head + 32'd1 == HEADS) begin
            done  <= 1'b1;
            state <= IDLE;
          end else begin
            head <= head + 32'd1;
            if (head_in_group + 32'd1 == GROUP) begin
              head_in_group <= 32'd0;
              kv_head       <= kv_head + 32'd1;
            end else begin
              head_in_group <= head_in_group + 32'd1;
            end
            state <= HEAD;
          end
        end

        default: state <= IDLE;
      endcase
    end
  end

endmodule
