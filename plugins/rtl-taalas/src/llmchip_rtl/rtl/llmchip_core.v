// llmchip vector engine: executes the microcode generated from a numeric spec
// (see isa.py for the instruction set and its reference semantics).
//
// One step computes the next token for (token_in, pos_in): pulse `start`,
// wait for `done`; `token_out` is the argmax (lowest id on ties). token_in,
// pos_in and mode must stay stable while busy. A position beyond the context,
// a token beyond the vocabulary or an undefined opcode ends the step with
// `error`; `start` is ignored while busy.
//
// Storage: weights, scales, constants and the program are on-chip read-only
// memories (llmchip_roms.v, generated). All data (activations, KV cache,
// attention scratch, trace buffers) lives in the external memory behind the
// mem_* port, one 32-bit word per access. TRACE copies an observation point
// into its trace buffer in the external memory (mode[0]: the observation
// points of the numeric spec, mode[1]: the debug traces after every
// operation), where the host reads it back.
//
// Area: one sequential multiplier/divider, one shift-and-saturate unit, one
// address adder and one accumulator adder are shared by all operations; only
// the L weight lanes of MATVEC run in parallel. Instruction fields are used
// at the widths the generator proves sufficient (llmchip_params.vh), and no
// value needs more than DW bits.
`timescale 1ns/1ps
`include "llmchip_params.vh"
/* verilator lint_off WIDTHEXPAND */
/* verilator lint_off WIDTHTRUNC */
module llmchip_core (
  input  wire            clk,
  input  wire            rst_n,
  input  wire            start,
  input  wire [15:0]     token_in,
  input  wire [15:0]     pos_in,
  input  wire [1:0]      mode,
  output reg             busy,
  output reg             done,
  output reg             error,
  output reg  [15:0]     token_out,
  output reg             mem_req,
  output reg             mem_we,
  output reg  [`AW-1:0]  mem_addr,
  output reg  [31:0]     mem_wdata,
  input  wire            mem_ack,
  input  wire [31:0]     mem_rdata,
  output reg  [`PCW-1:0] pc,
  output reg             trace_start
);
  `include "llmchip_functions.vh"

  localparam integer L = `LANES;
  localparam integer WB = `WBITS;
  localparam integer DW = `DW;
  localparam integer AW = `AW;
  localparam integer IW = `IW;
  localparam integer CTX = `CTX;
  localparam integer BW = 18;  // width of the second multiplier operand (signed)

  localparam [7:0] OP_END = 8'd0, OP_EMB = 8'd1, OP_MATVEC = 8'd2, OP_RMS = 8'd3, OP_ROPE = 8'd4,
                   OP_ATTN = 8'd5, OP_SILU = 8'd6, OP_ADD = 8'd7, OP_TRACE = 8'd8, OP_ARGMAX = 8'd9;

  // ---- program and constant memories (generated) ---------------------------------
  wire [7:0] op;
  wire signed [31:0] F0, F1, F2, F3, F4, F5, F6, F7, F8, F9, F10, F11, F12, F13;
  llmchip_prog u_prog (.pc(pc), .op(op), .f0(F0), .f1(F1), .f2(F2), .f3(F3), .f4(F4), .f5(F5), .f6(F6),
                       .f7(F7), .f8(F8), .f9(F9), .f10(F10), .f11(F11), .f12(F12), .f13(F13));
  reg  [`WAW-1:0] waddr;
  wire [L*WB-1:0] wword;
  llmchip_wrom u_wrom (.addr(waddr), .data(wword));
  reg  [`SAW-1:0] saddr;
  wire [L*15-1:0] sword;
  llmchip_srom u_srom (.addr(saddr), .data(sword));
  reg  [`CAW-1:0] caddr;
  wire signed [`CW-1:0] cword;
  llmchip_crom u_crom (.addr(caddr), .data(cword));

  // Field views at their proven widths: data addresses (A), counts (N), constant
  // addresses (C), shifts (S, signed 8 bit).
  wire [AW-1:0] A0 = F0, A1 = F1, A2 = F2, A3 = F3, A6 = F6, A10 = F10, A11 = F11;
  wire [IW-1:0] N1 = F1, N2 = F2, N3 = F3, N4 = F4, N5 = F5, N6 = F6;
  wire [`CAW-1:0] C3 = F3, C4 = F4, C5 = F5, C7 = F7, C9 = F9;
  wire signed [7:0] S4 = F4, S6 = F6, S8 = F8, S13 = F13;

  // ---- state -----------------------------------------------------------------------
  localparam [6:0]
    S_IDLE = 0, S_FETCH = 1, S_NEXT = 2, S_LOAD = 3, S_STORE = 4, S_MUL = 5, S_DIV = 6,
    S_EMB_0 = 7, S_EMB_1 = 8, S_EMB = 9, S_EMB_N = 10,
    S_MV_0 = 11, S_MV_BLK = 12, S_MV_X = 13, S_MV_ACC = 14, S_MV_SC = 15, S_MV_SC2 = 16, S_MV_W = 17,
    S_RMS_SUM = 18, S_RMS_SQ = 19, S_RMS_ACC = 20, S_RMS_NORM = 21, S_RMS_X = 22, S_RMS_M1 = 23, S_RMS_M2 = 24,
    S_RMS_W = 25,
    S_RO_0 = 26, S_RO_1 = 27, S_RO_A = 28, S_RO_B = 29, S_RO_C = 30, S_RO_S = 31, S_RO_W1 = 32, S_RO_M3 = 33,
    S_RO_M4 = 34, S_RO_W2 = 35, S_RO_N = 36,
    S_AT_HEAD = 37, S_AT_Q = 38, S_AT_K = 39, S_AT_QK = 40, S_AT_DOT = 41, S_AT_SC = 42, S_AT_SN = 43,
    S_AT_EL0 = 44, S_AT_EL = 45, S_AT_EN = 46, S_AT_PL = 47, S_AT_PD = 48, S_AT_PW = 49, S_AT_PN = 50,
    S_AT_OP = 51, S_AT_OV = 52, S_AT_OM = 53, S_AT_OA = 54, S_AT_ON = 55,
    S_SI_A = 56, S_SI_B = 57, S_SI_C = 58, S_SI_D = 59, S_SI_W = 60,
    S_ADD_A = 61, S_ADD_B = 62, S_ADD_W = 63, S_TR_0 = 64, S_TR_L = 65, S_TR_W = 66, S_AM_L = 67, S_AM_C = 68;

  reg [6:0] state, ret;
  reg ldsel;    // destination of a load: 0 = ra, 1 = rb
  reg div_rms;  // divisor of the running division: 1 = the RMS count, 0 = the exp sum

  // Loop counters and address registers.
  reg [IW-1:0] i, j, t, h, hc, gi, obase;
  reg [7:0] lane;
  reg [AW-1:0] base, hoff, kaddr, vrow, kvh;
  reg [`CAW-1:0] rowb;

  // Operands and scalar accumulators. s0 is the accumulator (sum of squares, dot
  // products, RoPE terms) and the remainder of the divider.
  reg signed [31:0] ra, best;  // best: running maximum of ATTN scores and of ARGMAX
  reg signed [15:0] rb;
  reg [IW-1:0] besti;
  reg signed [DW-1:0] s0;
  reg [31:0] sum;
  reg signed [`CW-1:0] rr;
  /* verilator lint_off UNUSEDSIGNAL */
  reg [6:0] e2;  // even: bit 0 unused
  /* verilator lint_on UNUSEDSIGNAL */

  // MATVEC lanes.
  reg signed [`ACCW-1:0] acc [0:L-1];
  reg signed [`TOTW-1:0] tot [0:L-1];

  // Shared sequential multiplier (mp = ma * mb, mb signed BW bits) and divider (mp = ma / den).
  reg signed [DW-1:0] ma, mp;
  reg signed [BW-1:0] mb;
  reg [6:0] mcnt;

  integer l, m;  // loop variables of the combinational and the clocked process
  wire [IW-1:0] i1 = i + 1'b1, j1 = j + 1'b1, t1 = t + 1'b1, gi1 = gi + 1'b1;
  wire [IW-1:0] pos = pos_in;
  wire [15:0] token = token_in;

  // ---- lane values -------------------------------------------------------------------
  wire [7:0] tlane = token & (L - 1);
  wire signed [WB-1:0] w_emb = wword >> (tlane * WB);
  wire [14:0] m_emb = sword >> (tlane * 15);
  wire [14:0] m_lane = sword >> (lane * 15);
  reg signed [`ACCW-1:0] acc_next [0:L-1];
  always @* begin
    for (l = 0; l < L; l = l + 1) acc_next[l] = acc[l] + $signed(ra[15:0]) * $signed(wword[l * WB +: WB]);
  end

  // ---- shared accumulator adder: s0 + mp or s0 - mp ----------------------------------------
  wire acc_sub = (state == S_RO_W1);
  wire signed [DW-1:0] s0_mp = s0 + (acc_sub ? ~mp : mp) + acc_sub;

  // ---- shared shift-and-saturate unit -------------------------------------------------------
  reg signed [DW-1:0] post_in;
  reg signed [7:0] post_sh;
  always @* begin
    post_in = mp;
    post_sh = 8'sd0;
    case (state)
      S_EMB:   begin post_in = $signed(w_emb) * $signed({1'b0, m_emb}); post_sh = S6; end
      S_MV_W:  begin post_in = tot[lane]; post_sh = S8; end
      S_RMS_W: post_sh = S6 + $signed({2'b00, e2[6:1]});
      S_RO_W1, S_RO_W2: begin post_in = s0_mp; post_sh = 8'sd14; end
      S_AT_SC: post_sh = S8;
      S_AT_OA: begin post_in = s0_mp; post_sh = 8'sd15; end
      S_SI_D:  post_sh = 8'sd15;
      S_SI_W:  post_sh = S6;
      S_ADD_W: begin post_in = (ra + rb) <<< 1; post_sh = 8'sd1; end  // rsh(2v, 1) = v
      default: ;
    endcase
  end
  wire signed [DW-1:0] post_rsh = f_rsh(post_in, post_sh);
  wire signed [DW-1:0] post16 = f_sat16(post_rsh);
  wire signed [DW-1:0] post32 = f_sat32(post_rsh);

  // ---- constant memory address per state ---------------------------------------------------
  wire [32:0] at_d = {best[31], best} - {ra[31], ra};  // max - s >= 0, needs 33 bits
  // v = sum(x^2) / n + eps < 2^31 (the generator checks eps), so 32 bits suffice.
  wire [31:0] rms_v = mp[31:0] + F4;
  wire [31:0] rms_vn = (rms_v == 0) ? 32'd1 : rms_v;
  // One adder: table base + index (the rsqrt table starts at t = 64).
  wire [`FW-1:0] rsq_t = f_rsqrt_t({{(`FW - 32){1'b0}}, rms_vn});
  // Functions are evaluated outside the clocked block (their locals must not become registers).
  wire [6:0] rms_e2 = f_rsqrt_e2({{(`FW - 32){1'b0}}, rms_vn});
  reg [`CAW-1:0] cb, co;
  wire [`CAW-1:0] ridx = rowb + j;
  always @* begin
    cb = 0;
    co = 0;
    case (state)
      S_RMS_NORM: begin cb = C7; co = rsq_t[7:0] - 8'd64; end
      S_RMS_M1: begin cb = C3; co = i; end
      S_RO_C, S_RO_M4: begin cb = C3; co = ridx; end
      S_RO_S, S_RO_M3: begin cb = C4; co = ridx; end
      S_AT_EL: begin cb = C9; co = f_exp_index({{(`FW - 33){1'b0}}, at_d}, F12[15:0], S13[4:0]); end
      S_SI_C: begin cb = C5; co = f_silu_index(ra[15:0], S4, F7[23:0], F8[15:0]); end
      default: ;
    endcase
    caddr = cb + co;
  end

  // ---- shared data memory address adder -------------------------------------------------------
  reg [AW-1:0] ab, ao;
  always @* begin
    ab = 0;
    ao = 0;
    case (state)
      S_EMB, S_MV_X, S_RMS_SUM, S_RMS_X, S_SI_A, S_ADD_A, S_ADD_W, S_AM_L: begin ab = A0; ao = i; end
      S_MV_W:    begin ab = base; ao = obase + lane; end
      S_RMS_W, S_SI_B, S_ADD_B: begin ab = A1; ao = i; end
      S_RO_A, S_RO_W1, S_TR_L: begin ab = base; ao = i; end
      S_RO_B, S_RO_W2: begin ab = base; ao = i1; end
      S_AT_Q:    begin ab = A0; ao = hoff + j; end
      S_AT_K:    begin ab = kaddr; ao = j; end
      S_AT_SC, S_AT_EL0: begin ab = A10; ao = t; end
      S_AT_EL, S_AT_PL, S_AT_PW, S_AT_OP: begin ab = A10; ao = CTX + t; end
      S_AT_OV:   begin ab = kaddr; ao = 0; end
      S_AT_OA:   begin ab = A3; ao = hoff + j; end
      S_SI_W:    begin ab = A2; ao = i; end
      S_TR_W:    begin ab = A6; ao = i; end
      default: ;
    endcase
  end
  wire [AW-1:0] daddr = ab + ao;

  // ---- operands of the multiplier / divider and data of stores, per state ----------------
  reg signed [DW-1:0] mul_a;
  reg signed [BW-1:0] mul_b;
  reg [31:0] st_data;
  always @* begin
    mul_a = ra;
    mul_b = cword;
    st_data = post16;
    case (state)
      S_FETCH: begin
        mul_a = pos;
        case (op)
          OP_EMB: begin mul_a = token >> `LANES_LOG2; mul_b = F1; end
          OP_MATVEC: mul_b = F9[1] ? F10 : 32'd0;
          OP_ROPE: mul_b = F5[1] ? F6 : 32'd0;
          default: mul_b = F4[1] ? F5 : 32'd0;  // TRACE
        endcase
      end
      S_EMB_0: begin mul_a = token >> `LANES_LOG2; mul_b = F5; end
      S_MV_SC: begin mul_a = acc[lane]; mul_b = {3'b000, m_lane}; end
      S_MV_W: st_data = F9[0] ? post32 : post16;
      S_RMS_SQ: mul_b = ra[BW-1:0];
      S_RMS_ACC: mul_a = s0_mp;
      S_RMS_M2: begin mul_a = mp; mul_b = rr; end
      S_RO_0: begin mul_a = pos; mul_b = F2 >>> 1; end
      S_RO_S, S_RO_M4: mul_a = rb;
      S_AT_QK, S_AT_OM: mul_b = rb;
      S_AT_DOT: begin mul_a = s0_mp; mul_b = F7; end
      S_AT_SC: st_data = post32;
      S_AT_EL: st_data = cword;
      S_AT_PD: mul_a = ra <<< 15;
      S_AT_PW: st_data = mp;
      S_SI_D: begin mul_a = post_rsh; mul_b = rb; end
      S_TR_W: st_data = ra;
      default: ;
    endcase
  end
  wire mul_neg = (mcnt == BW - 1);  // the sign bit of mb has negative weight

  // ---- subroutine calls (macros: task arguments would become registers in synthesis) ----
`define LOAD(SEL, R) begin mem_req <= 1'b1; mem_we <= 1'b0; mem_addr <= daddr; ldsel <= SEL; ret <= R; state <= S_LOAD; end
`define STORE(R) begin mem_req <= 1'b1; mem_we <= 1'b1; mem_addr <= daddr; mem_wdata <= st_data; ret <= R; state <= S_STORE; end
`define MUL(R) begin ma <= mul_a; mb <= mul_b; mp <= 0; mcnt <= 0; ret <= R; state <= S_MUL; end
`define DIV(RMS, R) begin ma <= mul_a; div_rms <= RMS; mp <= 0; s0 <= 0; mcnt <= 0; ret <= R; state <= S_DIV; end

  // Restoring division step: the remainder (in s0) stays below the 32-bit divisor.
  wire [32:0] den = div_rms ? {{(33 - IW){1'b0}}, N2} : {1'b0, sum};
  wire [32:0] rem_sh = {s0[31:0], ma[DW-1]};
  wire rem_ge = rem_sh >= den;

  wire sc_max = (t == 0) || (post32 > best);
  wire am_max = (i == 0) || (ra > best);

  always @(posedge clk) begin
    if (!rst_n) begin
      state <= S_IDLE;
      busy <= 1'b0;
      done <= 1'b0;
      error <= 1'b0;
      mem_req <= 1'b0;
      mem_we <= 1'b0;
      pc <= 0;
      token_out <= 16'd0;
      trace_start <= 1'b0;
      // Every register has a reset value: gate-level simulation then starts from known
      // values (no X pessimism in synthesized logic), and the chip from a defined state.
      ret <= S_IDLE;
      ldsel <= 1'b0;
      div_rms <= 1'b0;
      i <= 0; j <= 0; t <= 0; h <= 0; hc <= 0; gi <= 0; obase <= 0; lane <= 0;
      base <= 0; hoff <= 0; kaddr <= 0; vrow <= 0; kvh <= 0; rowb <= 0;
      ra <= 0; rb <= 0; best <= 0; besti <= 0; s0 <= 0; sum <= 0; rr <= 0; e2 <= 0;
      ma <= 0; mp <= 0; mb <= 0; mcnt <= 0;
      waddr <= 0; saddr <= 0;
      mem_addr <= 0; mem_wdata <= 0;
      for (m = 0; m < L; m = m + 1) begin
        acc[m] <= 0;
        tot[m] <= 0;
      end
    end else begin
      done <= 1'b0;
      trace_start <= 1'b0;
      case (state)
        S_IDLE: begin
          if (start) begin
            if (pos_in >= CTX || token_in >= `VOCAB) begin
              error <= 1'b1;
              done <= 1'b1;
            end else begin
              error <= 1'b0;
              pc <= 0;
              busy <= 1'b1;
              state <= S_FETCH;
            end
          end
        end

        // ---- subroutines; each returns to `ret` ----
        S_LOAD: if (mem_ack) begin
          mem_req <= 1'b0;
          if (ldsel) rb <= mem_rdata[15:0];
          else ra <= mem_rdata;
          state <= ret;
        end
        S_STORE: if (mem_ack) begin
          mem_req <= 1'b0;
          state <= ret;
        end
        S_MUL: begin
          // Shift-and-add, LSB first; the sign bit of mb has negative weight. Exact
          // modulo 2^DW, and the product fits DW bits.
          if (mb[0]) mp <= mp + (mul_neg ? ~ma : ma) + mul_neg;
          ma <= ma <<< 1;
          mb <= mb >>> 1;
          mcnt <= mcnt + 7'd1;
          if (mcnt == BW - 1) state <= ret;
        end
        S_DIV: begin
          s0 <= rem_ge ? rem_sh - den : rem_sh;
          mp <= {mp[DW-2:0], rem_ge};
          ma <= ma << 1;
          mcnt <= mcnt + 7'd1;
          if (mcnt == DW - 1) state <= ret;
        end

        // ---- instruction dispatch ----
        S_FETCH: begin
          i <= 0; j <= 0; t <= 0; h <= 0; hc <= 0; gi <= 0; obase <= 0; lane <= 0;
          s0 <= 0;
          case (op)
            OP_END: begin
              busy <= 1'b0;
              done <= 1'b1;
              state <= S_IDLE;
            end
            OP_EMB: `MUL(S_EMB_0)
            OP_MATVEC: `MUL(S_MV_0)
            OP_RMS: state <= S_RMS_SUM;
            OP_ROPE: `MUL(S_RO_0)
            OP_ATTN: begin
              hoff <= 0;
              kvh <= 0;
              state <= S_AT_HEAD;
            end
            OP_SILU: state <= S_SI_A;
            OP_ADD: state <= S_ADD_A;
            OP_TRACE: begin
              if (F4[2] ? mode[1] : mode[0]) `MUL(S_TR_0)
              else state <= S_NEXT;
            end
            OP_ARGMAX: state <= S_AM_L;
            default: begin
              error <= 1'b1;
              busy <= 1'b0;
              done <= 1'b1;
              state <= S_IDLE;
            end
          endcase
        end

        S_NEXT: begin
          pc <= pc + 1'b1;
          state <= S_FETCH;
        end

        // ---- EMB: x[i] = sat16(rsh(q[token, i] * m[token, i / group], shift)) ----
        S_EMB_0: begin
          waddr <= F2 + mp;
          `MUL(S_EMB_1)
        end
        S_EMB_1: begin
          saddr <= F3 + mp;
          state <= S_EMB;
        end
        S_EMB: `STORE(S_EMB_N)
        S_EMB_N: begin
          waddr <= waddr + 1'b1;
          if (gi1 == N4) begin
            gi <= 0;
            saddr <= saddr + 1'b1;
          end else gi <= gi1;
          if (i1 == N1) state <= S_NEXT;
          else begin
            i <= i1;
            state <= S_EMB;
          end
        end

        // ---- MATVEC: L rows per block, one input word per load, scaled per group ----
        S_MV_0: begin
          base <= A1 + mp;
          waddr <= F4;
          saddr <= F5;
          state <= S_MV_BLK;
        end
        S_MV_BLK: begin
          for (m = 0; m < L; m = m + 1) begin
            acc[m] <= 0;
            tot[m] <= 0;
          end
          i <= 0;
          gi <= 0;
          lane <= 0;
          state <= S_MV_X;
        end
        S_MV_X: `LOAD(1'b0, S_MV_ACC)
        S_MV_ACC: begin
          for (m = 0; m < L; m = m + 1) acc[m] <= acc_next[m];
          if (gi1 == N6 || i1 == N2) begin
            lane <= 0;
            state <= S_MV_SC;
          end else begin
            i <= i1;
            gi <= gi1;
            waddr <= waddr + 1'b1;
            state <= S_MV_X;
          end
        end
        S_MV_SC: `MUL(S_MV_SC2)
        S_MV_SC2: begin
          tot[lane] <= f_acc(tot[lane] + mp);  // narrowed when the accumulator is (ACC_NARROW)
          acc[lane] <= 0;
          if (lane == L - 1) begin
            lane <= 0;
            gi <= 0;
            saddr <= saddr + 1'b1;
            waddr <= waddr + 1'b1;
            if (i1 == N2) state <= S_MV_W;
            else begin
              i <= i1;
              state <= S_MV_X;
            end
          end else begin
            lane <= lane + 1'b1;
            state <= S_MV_SC;
          end
        end
        S_MV_W: begin
          if (lane == L) begin
            if (obase + L >= N3) state <= S_NEXT;
            else begin
              obase <= obase + L;
              state <= S_MV_BLK;
            end
          end else begin
            lane <= lane + 1'b1;
            if (obase + lane < N3) `STORE(S_MV_W)
          end
        end

        // ---- RMS: v = sum(x^2) / n + eps; out = sat16(rsh(x * w * rsqrt[t], shift + e2 / 2)) ----
        S_RMS_SUM: `LOAD(1'b0, S_RMS_SQ)
        S_RMS_SQ: `MUL(S_RMS_ACC)
        S_RMS_ACC: begin
          s0 <= s0_mp;
          if (i1 == N2) `DIV(1'b1, S_RMS_NORM)
          else begin
            i <= i1;
            state <= S_RMS_SUM;
          end
        end
        S_RMS_NORM: begin
          e2 <= rms_e2;
          rr <= cword;
          i <= 0;
          state <= S_RMS_X;
        end
        S_RMS_X: `LOAD(1'b0, S_RMS_M1)
        S_RMS_M1: `MUL(S_RMS_M2)
        S_RMS_M2: `MUL(S_RMS_W)
        S_RMS_W: begin
          `STORE((i1 == N2) ? S_NEXT : S_RMS_X)
          i <= i1;
        end

        // ---- ROPE: rotate each pair (a, b) by the table row of the position ----
        S_RO_0: begin
          base <= A0 + mp;
          `MUL(S_RO_1)
        end
        S_RO_1: begin
          rowb <= mp;
          state <= S_RO_A;
        end
        S_RO_A: `LOAD(1'b0, S_RO_B)
        S_RO_B: `LOAD(1'b1, S_RO_C)
        S_RO_C: `MUL(S_RO_S)                  // a * cos
        S_RO_S: begin
          s0 <= mp;
          `MUL(S_RO_W1)                      // b * sin
        end
        S_RO_W1: `STORE(S_RO_M3)                 // a * cos - b * sin
        S_RO_M3: `MUL(S_RO_M4)                // a * sin
        S_RO_M4: begin
          s0 <= mp;
          `MUL(S_RO_W2)                      // b * cos
        end
        S_RO_W2: `STORE(S_RO_N)                  // a * sin + b * cos
        S_RO_N: begin
          j <= (j1 == (N2 >> 1)) ? 0 : j1;
          if (i1 + 1'b1 >= N1) state <= S_NEXT;
          else begin
            i <= i1 + 1'b1;
            state <= S_RO_A;
          end
        end

        // ---- ATTN, head by head: scores, max, exp, normalize (divide), weighted sum ----
        S_AT_HEAD: begin
          if (h == N4) state <= S_NEXT;
          else begin
            t <= 0;
            j <= 0;
            s0 <= 0;
            kaddr <= A1 + kvh;
            state <= S_AT_Q;
          end
        end
        S_AT_Q: `LOAD(1'b0, S_AT_K)
        S_AT_K: `LOAD(1'b1, S_AT_QK)
        S_AT_QK: `MUL(S_AT_DOT)
        S_AT_DOT: begin
          s0 <= s0_mp;
          if (j1 == N6) begin
            j <= 0;
            `MUL(S_AT_SC)
          end else begin
            j <= j1;
            state <= S_AT_Q;
          end
        end
        S_AT_SC: begin
          s0 <= 0;
          if (sc_max) best <= post32;
          `STORE(S_AT_SN)
        end
        S_AT_SN: begin
          if (t == pos) begin
            t <= 0;
            sum <= 0;
            state <= S_AT_EL0;
          end else begin
            t <= t1;
            kaddr <= kaddr + A11;
            state <= S_AT_Q;
          end
        end
        S_AT_EL0: `LOAD(1'b0, S_AT_EL)
        S_AT_EL: begin
          sum <= sum + cword;
          `STORE(S_AT_EN)
        end
        S_AT_EN: begin
          if (t == pos) begin
            t <= 0;
            state <= S_AT_PL;
          end else begin
            t <= t1;
            state <= S_AT_EL0;
          end
        end
        S_AT_PL: `LOAD(1'b0, S_AT_PD)
        S_AT_PD: `DIV(1'b0, S_AT_PW)
        S_AT_PW: `STORE(S_AT_PN)
        S_AT_PN: begin
          if (t == pos) begin
            t <= 0;
            j <= 0;
            s0 <= 0;
            vrow <= A2 + kvh;
            kaddr <= A2 + kvh;
            state <= S_AT_OP;
          end else begin
            t <= t1;
            state <= S_AT_PL;
          end
        end
        S_AT_OP: `LOAD(1'b0, S_AT_OV)
        S_AT_OV: `LOAD(1'b1, S_AT_OM)
        S_AT_OM: `MUL(S_AT_OA)
        S_AT_OA: begin
          if (t == pos) `STORE(S_AT_ON)
          else begin
            s0 <= s0_mp;
            t <= t1;
            kaddr <= kaddr + A11;
            state <= S_AT_OP;
          end
        end
        S_AT_ON: begin
          s0 <= 0;
          t <= 0;
          if (j1 == N6) begin
            h <= h + 1'b1;
            hoff <= hoff + A6;
            if (hc + 1'b1 == N5) begin
              hc <= 0;
              kvh <= kvh + A6;
            end else hc <= hc + 1'b1;
            state <= S_AT_HEAD;
          end else begin
            j <= j1;
            vrow <= vrow + 1'b1;
            kaddr <= vrow + 1'b1;
            state <= S_AT_OP;
          end
        end

        // ---- SILU gate: g = sat16(rsh(rsh(h1 * sigmoid[index(h1)], 15) * h3, sh2)) ----
        S_SI_A: `LOAD(1'b0, S_SI_B)
        S_SI_B: `LOAD(1'b1, S_SI_C)
        S_SI_C: `MUL(S_SI_D)
        S_SI_D: `MUL(S_SI_W)
        S_SI_W: begin
          `STORE((i1 == N3) ? S_NEXT : S_SI_A)
          i <= i1;
        end

        // ---- ADD: x = sat16(x + y) ----
        S_ADD_A: `LOAD(1'b0, S_ADD_B)
        S_ADD_B: `LOAD(1'b1, S_ADD_W)
        S_ADD_W: begin
          `STORE((i1 == N2) ? S_NEXT : S_ADD_A)
          i <= i1;
        end

        // ---- TRACE: copy an observation point into its trace buffer ----
        S_TR_0: begin
          base <= A0 + mp;
          trace_start <= 1'b1;
          state <= S_TR_L;
        end
        S_TR_L: `LOAD(1'b0, S_TR_W)
        S_TR_W: begin
          `STORE((i1 == N1) ? S_NEXT : S_TR_L)
          i <= i1;
        end

        // ---- ARGMAX (lowest index on ties) ----
        S_AM_L: `LOAD(1'b0, S_AM_C)
        S_AM_C: begin
          if (am_max) begin
            best <= ra;
            besti <= i;
          end
          if (i1 == N1) begin
            token_out <= am_max ? i : besti;
            state <= S_NEXT;
          end else begin
            i <= i1;
            state <= S_AM_L;
          end
        end

        default: state <= S_IDLE;
      endcase
    end
  end
endmodule
`undef LOAD
`undef STORE
`undef MUL
`undef DIV
/* verilator lint_on WIDTHTRUNC */
/* verilator lint_on WIDTHEXPAND */
