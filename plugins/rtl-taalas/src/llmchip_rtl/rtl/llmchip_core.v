// llmchip vector engine: executes the microcode generated from a numeric spec
// (see isa.py for the instruction set and its reference semantics).
//
// One step computes the next-token logits for (token_in, pos_in): pulse
// `start`, wait for `done`; `token_out` is the argmax (lowest id on ties).
// A position beyond the context or a token beyond the vocabulary ends the
// step at once with `error`; `start` is ignored while busy.
// TRACE instructions stream observation points on the obs_* port (the debug
// readback of the chip spec), one word per cycle.
//
// Memories: weight, scale, constant and program ROMs and the data memory are
// read asynchronously (the standard-cell ROMs of the GF180 prerun are
// combinational); the data memory, including the KV cache, has one write per
// cycle.
`timescale 1ns/1ps
`include "llmchip_params.vh"
// Width policy: values are sign-extended to the 64-bit datapath on purpose, and
// memory indexes are computed in 32 or 64 bits and truncated to the address
// width; both are intended, so these two lint categories are off for this file.
/* verilator lint_off WIDTHEXPAND */
/* verilator lint_off WIDTHTRUNC */
module llmchip_core (
  input  wire        clk,
  input  wire        rst_n,
  input  wire        start,
  input  wire [31:0] token_in,
  input  wire [31:0] pos_in,
  output reg         busy,
  output reg         done,
  output reg         error,
  output reg  [31:0] token_out,
  output reg         obs_valid,
  output reg  [7:0]  obs_point,
  output reg  [31:0] obs_layer,
  output reg  [31:0] obs_index,
  output reg  signed [31:0] obs_data,
  output reg  [31:0] pc
);
  `include "llmchip_functions.vh"

  localparam integer L = `LANES;
  localparam integer WB = `WBITS;
  localparam integer LG = `LANES_LOG2;
  localparam integer CTX = `CTX;
  localparam integer VOCAB = `VOCAB;
  localparam integer PW = 8 + 32 * 16;

  reg [L*WB-1:0]    wrom [0:`NWROM-1];
  reg [L*15-1:0]    srom [0:`NSROM-1];
  reg signed [31:0] crom [0:`NCROM-1];
  reg [PW-1:0]      prog [0:`NPROG-1];
  reg signed [31:0] dmem [0:`NDMEM-1];

  reg [8*256-1:0] progfile;
  initial begin
    $readmemh("wrom.hex", wrom);
    $readmemh("srom.hex", srom);
    $readmemh("crom.hex", crom);
    if (!$value$plusargs("prog=%s", progfile)) progfile = "prog.hex";
    $readmemh(progfile, prog);
  end

  localparam [7:0] OP_END = 8'd0, OP_EMB = 8'd1, OP_MATVEC = 8'd2, OP_RMS = 8'd3, OP_ROPE = 8'd4,
                   OP_ATTN = 8'd5, OP_SILU = 8'd6, OP_ADD = 8'd7, OP_TRACE = 8'd8, OP_ARGMAX = 8'd9;

  localparam [5:0] S_IDLE = 6'd0, S_FETCH = 6'd1, S_DECODE = 6'd2, S_NEXT = 6'd3,
                   S_EMB = 6'd4, S_MV = 6'd5, S_MV_W = 6'd6,
                   S_RMS_SUM = 6'd7, S_RMS_NORM = 6'd8, S_RMS_OUT = 6'd9,
                   S_ROPE_A = 6'd10, S_ROPE_B = 6'd11,
                   S_AT_HEAD = 6'd12, S_AT_DOT = 6'd13, S_AT_EXP = 6'd14, S_AT_DIVSTART = 6'd15, S_AT_DIVDONE = 6'd16, S_AT_OUT = 6'd17,
                   S_SILU = 6'd18, S_ADD = 6'd19, S_TRACE = 6'd20, S_ARGMAX = 6'd21, S_DIV = 6'd22, S_DIV_INIT = 6'd23;

  reg [5:0] state, div_ret;
  reg [7:0] op;
  reg signed [31:0] f [0:15];
  reg [31:0] pos, token;

  reg [31:0] i, j, t, h, hc, g, gi, lane_i, obase;
  reg [31:0] waddr, saddr, base, dstb, rowb, kaddr, vaddr, kvh, blk, lane;
  reg signed [63:0] acc [0:L-1];
  reg signed [63:0] tot [0:L-1];
  reg signed [63:0] ss, dot, maxs, sum, sacc, rope_b, rr, best;
  reg [31:0] besti;
  reg [6:0] e2;

  // Sequential restoring divider (unsigned).
  reg [63:0] div_num, div_den, div_q, div_r;
  reg [6:0]  div_bit;
  reg [63:0] r_next;

  integer l;
  reg signed [63:0] a_next, xv, wv, mv, v1, v2, cv, sv, r64, gate;
  reg [L*WB-1:0] word;
  reg [L*15-1:0] sword;
  reg [63:0] vnorm;

  wire pos_rel_mv = f[9][1];
  wire sat32_mv = f[9][0];

  always @(posedge clk) begin
    if (!rst_n) begin
      state <= S_IDLE;
      busy <= 1'b0;
      done <= 1'b0;
      error <= 1'b0;
      obs_valid <= 1'b0;
      pc <= 32'd0;
      token_out <= 32'd0;
    end else begin
      done <= 1'b0;
      obs_valid <= 1'b0;
      case (state)
        S_IDLE: begin
          if (start) begin
            if (pos_in >= CTX || token_in >= VOCAB) begin
              error <= 1'b1;
              done <= 1'b1;
            end else begin
              error <= 1'b0;
              pos <= pos_in;
              token <= token_in;
              pc <= 32'd0;
              busy <= 1'b1;
              state <= S_FETCH;
            end
          end
        end

        S_FETCH: begin
          op <= prog[pc][7:0];
          for (l = 0; l < 16; l = l + 1) f[l] <= prog[pc][8 + 32 * l +: 32];
          state <= S_DECODE;
        end

        S_NEXT: begin
          pc <= pc + 32'd1;
          state <= S_FETCH;
        end

        S_DECODE: begin
          i <= 32'd0; j <= 32'd0; t <= 32'd0; g <= 32'd0; gi <= 32'd0;
          case (op)
            OP_END: begin
              busy <= 1'b0;
              done <= 1'b1;
              state <= S_IDLE;
            end
            OP_EMB: begin
              blk = token >> LG;
              lane <= token & (L - 1);
              waddr <= f[2] + blk * f[1];
              saddr <= f[3] + blk * f[5];
              state <= S_EMB;
            end
            OP_MATVEC: begin
              waddr <= f[4];
              saddr <= f[5];
              obase <= 32'd0;
              dstb <= f[1] + (pos_rel_mv ? pos * f[10] : 32'd0);
              for (l = 0; l < L; l = l + 1) begin
                acc[l] <= 64'sd0;
                tot[l] <= 64'sd0;
              end
              state <= S_MV;
            end
            OP_RMS: begin
              ss <= 64'sd0;
              state <= S_RMS_SUM;
            end
            OP_ROPE: begin
              base <= f[0] + (f[5][1] ? pos * f[6] : 32'd0);
              rowb <= pos * (f[2] >> 1);
              state <= S_ROPE_A;
            end
            OP_ATTN: begin
              h <= 32'd0;
              hc <= 32'd0;
              kvh <= 32'd0;
              state <= S_AT_HEAD;
            end
            OP_SILU: state <= S_SILU;
            OP_ADD: state <= S_ADD;
            OP_TRACE: begin
              base <= f[0] + (f[4][1] ? pos * f[5] : 32'd0);
              state <= S_TRACE;
            end
            OP_ARGMAX: begin
              best <= dmem[f[0]];
              besti <= 32'd0;
              i <= 32'd1;
              state <= S_ARGMAX;
            end
            default: begin
              error <= 1'b1;
              busy <= 1'b0;
              done <= 1'b1;
              state <= S_IDLE;
            end
          endcase
        end

        // x[i] = sat16(rsh(q[token, i] * m[token, i / group], shift))
        S_EMB: begin
          word = wrom[waddr + i];
          sword = srom[saddr + g];
          wv = f_sx(word >> (lane * WB), WB);
          mv = $signed({49'd0, sword[lane * 15 +: 15]});
          r64 = f_sat16(f_rsh(wv * mv, f[6]));
          dmem[f[0] + i] <= r64[31:0];
          if (gi == f[4] - 1) begin
            gi <= 32'd0;
            g <= g + 32'd1;
          end else gi <= gi + 32'd1;
          if (i == f[1] - 1) state <= S_NEXT;
          else i <= i + 32'd1;
        end

        // One input per cycle for L output rows; scaled at the end of every group.
        S_MV: begin
          xv = dmem[f[0] + i];
          word = wrom[waddr];
          sword = srom[saddr];
          for (l = 0; l < L; l = l + 1) begin
            wv = f_sx(word >> (l * WB), WB);
            a_next = acc[l] + xv * wv;
            if (gi == f[6] - 1 || i == f[2] - 1) begin
              mv = $signed({49'd0, sword[l * 15 +: 15]});
              tot[l] <= tot[l] + a_next * mv;
              acc[l] <= 64'sd0;
            end else acc[l] <= a_next;
          end
          waddr <= waddr + 32'd1;
          if (gi == f[6] - 1 || i == f[2] - 1) begin
            saddr <= saddr + 32'd1;
            gi <= 32'd0;
          end else gi <= gi + 32'd1;
          if (i == f[2] - 1) begin
            lane_i <= 32'd0;
            state <= S_MV_W;
          end else i <= i + 32'd1;
        end

        S_MV_W: begin
          if (obase + lane_i < f[3]) begin
            r64 = sat32_mv ? f_sat32(f_rsh(tot[lane_i], f[8])) : f_sat16(f_rsh(tot[lane_i], f[8]));
            dmem[dstb + obase + lane_i] <= r64[31:0];
          end
          tot[lane_i] <= 64'sd0;
          if (lane_i == L - 1) begin
            if (obase + L >= f[3]) state <= S_NEXT;
            else begin
              obase <= obase + L;
              i <= 32'd0;
              gi <= 32'd0;
              state <= S_MV;
            end
          end else lane_i <= lane_i + 32'd1;
        end

        // rmsnorm: sum of squares, divide by n, normalize, table lookup, scale.
        S_RMS_SUM: begin
          xv = dmem[f[0] + i];
          if (i == f[2] - 1) begin
            div_num <= ss + xv * xv;
            div_den <= {32'd0, f[2]};
            div_bit <= 7'd64;
            div_ret <= S_RMS_NORM;
            state <= S_DIV_INIT;
          end else begin
            ss <= ss + xv * xv;
            i <= i + 32'd1;
          end
        end

        S_RMS_NORM: begin
          vnorm = div_q + {f[5], f[4]};
          if (vnorm == 64'd0) vnorm = 64'd1;
          e2 <= f_rsqrt_e2(vnorm);
          rr <= crom[f[7] + f_rsqrt_t(vnorm) - 64];
          i <= 32'd0;
          state <= S_RMS_OUT;
        end

        S_RMS_OUT: begin
          xv = dmem[f[0] + i];
          cv = crom[f[3] + i];
          r64 = f_sat16(f_rsh(xv * cv * rr, f[6] + {26'd0, e2[6:1]}));
          dmem[f[1] + i] <= r64[31:0];
          if (i == f[2] - 1) state <= S_NEXT;
          else i <= i + 32'd1;
        end

        // RoPE: rotate pair (a, b) with the table row of the position.
        S_ROPE_A: begin
          v1 = dmem[base + i];
          v2 = dmem[base + i + 1];
          cv = crom[f[3] + rowb + j];
          sv = crom[f[4] + rowb + j];
          r64 = f_sat16(f_rsh(v1 * cv - v2 * sv, 14));
          dmem[base + i] <= r64[31:0];
          rope_b <= f_sat16(f_rsh(v1 * sv + v2 * cv, 14));
          state <= S_ROPE_B;
        end

        S_ROPE_B: begin
          dmem[base + i + 1] <= rope_b[31:0];
          j <= (j + 1 == (f[2] >> 1)) ? 32'd0 : j + 32'd1;
          if (i + 2 >= f[1]) state <= S_NEXT;
          else begin
            i <= i + 32'd2;
            state <= S_ROPE_A;
          end
        end

        // Attention, head by head: scores, max, exp, normalize (divide), weighted sum.
        S_AT_HEAD: begin
          if (h == f[4]) state <= S_NEXT;
          else begin
            t <= 32'd0;
            j <= 32'd0;
            dot <= 64'sd0;
            kaddr <= f[1] + kvh;
            state <= S_AT_DOT;
          end
        end

        S_AT_DOT: begin
          xv = dmem[f[0] + h * f[6] + j];
          v1 = dmem[kaddr + j];
          a_next = dot + xv * v1;
          if (j == f[6] - 1) begin
            r64 = f_sat32(f_rsh(a_next * f[7], f[8]));
            dmem[f[10] + t] <= r64[31:0];
            maxs <= (t == 0 || r64 > maxs) ? r64 : maxs;
            dot <= 64'sd0;
            j <= 32'd0;
            kaddr <= kaddr + f[11];
            if (t == pos) begin
              t <= 32'd0;
              sum <= 64'sd0;
              state <= S_AT_EXP;
            end else t <= t + 32'd1;
          end else begin
            dot <= a_next;
            j <= j + 32'd1;
          end
        end

        S_AT_EXP: begin
          v1 = dmem[f[10] + t];
          cv = crom[f[9] + f_exp_index(maxs - v1, f[12], f[13])];
          dmem[f[10] + CTX + t] <= cv[31:0];
          sum <= sum + cv;
          if (t == pos) begin
            t <= 32'd0;
            state <= S_AT_DIVSTART;
          end else t <= t + 32'd1;
        end

        S_AT_DIVSTART: begin
          v1 = dmem[f[10] + CTX + t];
          div_num <= v1 << 15;
          div_den <= sum;
          div_bit <= 7'd17;  // p <= 2^15
          div_ret <= S_AT_DIVDONE;
          state <= S_DIV_INIT;
        end

        S_AT_DIVDONE: begin
          dmem[f[10] + CTX + t] <= div_q[31:0];
          if (t == pos) begin
            t <= 32'd0;
            j <= 32'd0;
            sacc <= 64'sd0;
            vaddr <= f[2] + kvh;
            state <= S_AT_OUT;
          end else begin
            t <= t + 32'd1;
            state <= S_AT_DIVSTART;
          end
        end

        S_AT_OUT: begin
          v1 = dmem[f[10] + CTX + t];
          v2 = dmem[vaddr];
          a_next = sacc + v1 * v2;
          if (t == pos) begin
            r64 = f_sat16(f_rsh(a_next, 15));
            dmem[f[3] + h * f[6] + j] <= r64[31:0];
            sacc <= 64'sd0;
            t <= 32'd0;
            if (j == f[6] - 1) begin
              h <= h + 32'd1;
              if (hc + 1 == f[5]) begin
                hc <= 32'd0;
                kvh <= kvh + f[6];
              end else hc <= hc + 32'd1;
              state <= S_AT_HEAD;
            end else begin
              j <= j + 32'd1;
              vaddr <= f[2] + kvh + j + 1;
            end
          end else begin
            sacc <= a_next;
            t <= t + 32'd1;
            vaddr <= vaddr + f[11];
          end
        end

        // SiLU gate: g = sat16(rsh(rsh(h1 * sigmoid(h1), 15) * h3, sh2))
        S_SILU: begin
          v1 = dmem[f[0] + i];
          v2 = dmem[f[1] + i];
          cv = crom[f[5] + f_silu_index(v1, f[4], f[7], f[8])];
          gate = f_rsh(v1 * cv, 15);
          r64 = f_sat16(f_rsh(gate * v2, f[6]));
          dmem[f[2] + i] <= r64[31:0];
          if (i == f[3] - 1) state <= S_NEXT;
          else i <= i + 32'd1;
        end

        S_ADD: begin
          v1 = dmem[f[0] + i];
          v2 = dmem[f[1] + i];
          r64 = f_sat16(v1 + v2);
          dmem[f[0] + i] <= r64[31:0];
          if (i == f[2] - 1) state <= S_NEXT;
          else i <= i + 32'd1;
        end

        S_TRACE: begin
          obs_valid <= 1'b1;
          obs_point <= f[2][7:0];
          obs_layer <= f[3];
          obs_index <= i;
          obs_data <= dmem[base + i];
          if (i == f[1] - 1) state <= S_NEXT;
          else i <= i + 32'd1;
        end

        S_ARGMAX: begin
          if (i >= f[1]) begin
            token_out <= besti;
            state <= S_NEXT;
          end else begin
            v1 = dmem[f[0] + i];
            if (v1 > best) begin
              best <= v1;
              besti <= i;
            end
            i <= i + 32'd1;
          end
        end

        // Restoring division: div_q = div_num / div_den, the quotient known to fit div_bit bits;
        // the remainder starts with the dividend bits above the quotient.
        S_DIV_INIT: begin
          div_r <= (div_bit == 7'd64) ? 64'd0 : (div_num >> div_bit);
          div_q <= 64'd0;
          state <= S_DIV;
        end

        S_DIV: begin
          if (div_bit == 7'd0) state <= div_ret;
          else begin
            r_next = {div_r[62:0], div_num[div_bit - 7'd1]};
            if (r_next >= div_den) begin
              div_r <= r_next - div_den;
              div_q <= div_q | (64'd1 << (div_bit - 7'd1));
            end else div_r <= r_next;
            div_bit <= div_bit - 7'd1;
          end
        end

        default: state <= S_IDLE;
      endcase
    end
  end
endmodule
