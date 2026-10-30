// The per-token schedule of one taalas-tile/1 decoder layer (drafts/Qwen05B_Taalas_Refactor_Plan_zh.md
// §12.1, node B1). The phase timing lives here, in the chip, not in a testbench: one state per
// phase of the schedule table, in the table's order, driving the hand-written operator templates.
//
//   attn_norm -> q/k/v -> per-head q_norm/k_norm -> RoPE -> KV write -> attention -> o_proj ->
//   residual -> mlp_norm -> gate/up -> SiLU -> down_proj -> residual
//
// Every operator is released with a one-cycle `start` (an FSM pulse state) and reports `done`; the
// row results stream out on `y_valid`/`y_index` and are collected into the layer's buffers. The
// phase order is the architecture's, so the controller is a template too: only dimensions and the
// numeric spec's fraction bits are parameters. `done` is one cycle wide; `busy` is high from the
// start of a token until the last residual is written.
`default_nettype none

module tile_layer #(
  parameter integer HIDDEN = 32,
  parameter integer INTER = 64,
  parameter integer HEADS = 4,
  parameter integer KVHEADS = 2,
  parameter integer HD = 8,
  parameter integer LOG2CONTEXT = 4,
  parameter integer MACS = 8,
  parameter integer BITS = 4,
  parameter integer ACC = 32,
  parameter integer GROUPS = 8,
  parameter integer CPG = 2,
  parameter integer FRAC_H = 12,
  parameter integer FRAC_QK = 13,
  parameter integer NORM_FN = 15,
  parameter signed [63:0] EPS_ATTN = 64'sd68719,
  parameter signed [63:0] EPS_MLP = 64'sd68719,
  parameter signed [63:0] EPS_HEAD = 64'sd1074,
  parameter integer ROPE_ADDR = 6,
  parameter integer EXP_ADDR = 9,
  parameter integer ATTN_SCALE = 11585,
  parameter integer SILU_F1 = 15,
  parameter integer SILU_F3 = 15,
  parameter integer SILU_FG = 19,
  parameter integer Q_FI = 12, Q_FO = 15, Q_EXP = 21,
  parameter integer K_FI = 12, K_FO = 15, K_EXP = 21,
  parameter integer V_FI = 12, V_FO = 15, V_EXP = 21,
  parameter integer O_FI = 15, O_FO = 18, O_EXP = 21,
  parameter integer G_FI = 12, G_FO = 15, G_EXP = 21,
  parameter integer U_FI = 12, U_FO = 15, U_EXP = 21,
  parameter integer D_FI = 19, D_FO = 18, D_EXP = 21,
  parameter Q_B0 = "q_bank0.hex", Q_B1 = "q_bank1.hex",
  parameter Q_B2 = "q_bank2.hex", Q_B3 = "q_bank3.hex",
  parameter Q_M = "q_m.hex", Q_BIAS = "q_b.hex",
  parameter K_B0 = "k_bank0.hex", K_B1 = "k_bank1.hex",
  parameter K_B2 = "k_bank2.hex", K_B3 = "k_bank3.hex",
  parameter K_M = "k_m.hex", K_BIAS = "k_b.hex",
  parameter V_B0 = "v_bank0.hex", V_B1 = "v_bank1.hex",
  parameter V_B2 = "v_bank2.hex", V_B3 = "v_bank3.hex",
  parameter V_M = "v_m.hex", V_BIAS = "v_b.hex",
  parameter O_B0 = "o_bank0.hex", O_B1 = "o_bank1.hex",
  parameter O_B2 = "o_bank2.hex", O_B3 = "o_bank3.hex",
  parameter O_M = "o_m.hex", O_BIAS = "o_b.hex",
  parameter G_B0 = "g_bank0.hex", G_B1 = "g_bank1.hex",
  parameter G_B2 = "g_bank2.hex", G_B3 = "g_bank3.hex",
  parameter G_M = "g_m.hex", G_BIAS = "g_b.hex",
  parameter U_B0 = "u_bank0.hex", U_B1 = "u_bank1.hex",
  parameter U_B2 = "u_bank2.hex", U_B3 = "u_bank3.hex",
  parameter U_M = "u_m.hex", U_BIAS = "u_b.hex",
  parameter D_B0 = "d_bank0.hex", D_B1 = "d_bank1.hex",
  parameter D_B2 = "d_bank2.hex", D_B3 = "d_bank3.hex",
  parameter D_M = "d_m.hex", D_BIAS = "d_b.hex",
  parameter ATTN_W_INIT = "attn_w.hex", MLP_W_INIT = "mlp_w.hex",
  parameter QNORM_W_INIT = "qnorm_w.hex", KNORM_W_INIT = "knorm_w.hex",
  parameter EXP_INIT = "lut.exp.hex",
  parameter RSQRT_INIT = "rsqrt.hex",
  parameter ROPE_COS_INIT = "rope_cos.hex",
  parameter ROPE_SIN_INIT = "rope_sin.hex",
  parameter SIGMOID_INIT = "sigmoid.hex"
) (
  input  wire                          clk,
  input  wire                          rst_n,
  input  wire                          start,
  input  wire [15:0]                   position,
  input  wire signed [HIDDEN*16-1:0]   x,
  output reg  signed [HIDDEN*16-1:0]   x2,
  output wire                          done,
  output wire                          busy,
  output wire signed [HIDDEN*16-1:0]   obs_h,
  output wire signed [HIDDEN*16-1:0]   obs_x1,
  output wire signed [HIDDEN*16-1:0]   obs_h2,
  output wire signed [HIDDEN*16-1:0]   obs_o,
  output wire signed [HIDDEN*16-1:0]   obs_f,
  output wire signed [HEADS*HD*16-1:0] obs_q,
  output wire signed [HEADS*HD*16-1:0] obs_qn,
  output wire signed [HEADS*HD*16-1:0] obs_qr,
  output wire signed [HEADS*HD*16-1:0] obs_a,
  output wire signed [KVHEADS*HD*16-1:0] obs_k,
  output wire signed [KVHEADS*HD*16-1:0] obs_v,
  output wire signed [KVHEADS*HD*16-1:0] obs_kn,
  output wire signed [KVHEADS*HD*16-1:0] obs_kr,
  output wire signed [INTER*16-1:0]    obs_g,
  output wire signed [INTER*16-1:0]    obs_u,
  output wire signed [INTER*16-1:0]    obs_m,
  output wire [63:0]                   obs_raw_sum,
  output wire                          obs_raw_valid
);

  localparam integer ATTN_W = HEADS * HD;
  localparam integer KV_W = KVHEADS * HD;
  localparam integer CH_H = HIDDEN / MACS;
  localparam integer CH_A = ATTN_W / MACS;
  localparam integer CH_I = INTER / MACS;
  localparam integer L2CH_H = $clog2(CH_H);
  localparam integer L2CH_A = $clog2(CH_A);
  localparam integer L2CH_I = $clog2(CH_I);
  localparam integer L2MACS = $clog2(MACS);
  localparam integer L2R_H = $clog2(HIDDEN);
  localparam integer L2R_K = $clog2(KV_W);
  localparam integer L2R_I = $clog2(INTER);
  localparam integer L2CPG = $clog2(CPG);
  localparam integer NG_H = CH_H / CPG;
  localparam integer NG_A = CH_A / CPG;
  localparam integer NG_I = CH_I / CPG;
  localparam integer L2A_Q = $clog2(HIDDEN * CH_H);
  localparam integer L2A_K = $clog2(KV_W * CH_H);
  localparam integer L2A_O = $clog2(HIDDEN * CH_A);
  localparam integer L2A_G = $clog2(INTER * CH_H);
  localparam integer L2A_D = $clog2(HIDDEN * CH_I);
  localparam integer KV_ADDR = LOG2CONTEXT + $clog2(KVHEADS);

  localparam [5:0] S_IDLE = 6'd0, S_NA0 = 6'd1, S_NA1 = 6'd2, S_QKVW = 6'd3,
                   S_QKVS = 6'd4, S_QKVWT = 6'd5, S_QN0 = 6'd6, S_QN1 = 6'd7,
                   S_KN0 = 6'd8, S_KN1 = 6'd9, S_RP0 = 6'd10, S_RP1 = 6'd11,
                   S_KVW = 6'd12, S_AT0 = 6'd13, S_AT1 = 6'd14, S_OW = 6'd15,
                   S_OS = 6'd16, S_OWT = 6'd17, S_RES1 = 6'd18, S_NM0 = 6'd19,
                   S_NM1 = 6'd20, S_GUW = 6'd21, S_GUS = 6'd22, S_GUWT = 6'd23,
                   S_SILU = 6'd24, S_DW = 6'd25, S_DS = 6'd26, S_DWT = 6'd27,
                   S_RES2 = 6'd28, S_DONE = 6'd29;

  reg [5:0] state;
  reg [31:0] wcount;
  reg [31:0] head;
  reg [31:0] kh;
  reg [15:0] pos_q;

  reg signed [HIDDEN*16-1:0] x_q;
  reg signed [HIDDEN*16-1:0] h;
  reg signed [HIDDEN*16-1:0] o_buf;
  reg signed [HIDDEN*16-1:0] x1;
  reg signed [HIDDEN*16-1:0] h2;
  reg signed [HIDDEN*16-1:0] f_buf;
  reg signed [ATTN_W*16-1:0] q0;
  reg signed [ATTN_W*16-1:0] qn;
  reg signed [ATTN_W*16-1:0] qr;
  reg signed [ATTN_W*16-1:0] a_buf;
  reg signed [KV_W*16-1:0] k0;
  reg signed [KV_W*16-1:0] v0;
  reg signed [KV_W*16-1:0] kn;
  reg signed [KV_W*16-1:0] kr;
  reg signed [INTER*16-1:0] g_buf;
  reg signed [INTER*16-1:0] u_buf;
  reg signed [INTER*16-1:0] m_buf;

  reg [HIDDEN*16-1:0] attn_w_mem [0:0];
  reg [HIDDEN*16-1:0] mlp_w_mem [0:0];
  reg [HD*16-1:0] qnorm_w_mem [0:0];
  reg [HD*16-1:0] knorm_w_mem [0:0];

  // The norm weights are the numeric spec's arrays. A synthesised chip receives them as a
  // generated ROM; in simulation they are the layer's own files. `SYNTHESIS` is defined by the
  // RTL check, which parses every template but synthesises only the top's hierarchy.
`ifndef SYNTHESIS
  initial begin
    $readmemh(ATTN_W_INIT, attn_w_mem);
    $readmemh(MLP_W_INIT, mlp_w_mem);
    $readmemh(QNORM_W_INIT, qnorm_w_mem);
    $readmemh(KNORM_W_INIT, knorm_w_mem);
  end
`endif

  wire signed [HIDDEN*16-1:0] attn_w = attn_w_mem[0];
  wire signed [HIDDEN*16-1:0] mlp_w = mlp_w_mem[0];
  wire signed [HD*16-1:0] qnorm_w = qnorm_w_mem[0];
  wire signed [HD*16-1:0] knorm_w = knorm_w_mem[0];

  // ---- control and datapath wires ----------------------------------------------------
  wire [L2MACS-1:0] lane_w = wcount[L2MACS-1:0];
  wire [L2CH_H-1:0] chunk_h = wcount[L2MACS +: L2CH_H];
  wire [L2CH_A-1:0] chunk_a = wcount[L2MACS +: L2CH_A];
  wire [L2CH_I-1:0] chunk_i = wcount[L2MACS +: L2CH_I];
  wire signed [15:0] xw_data = h[wcount*16 +: 16];
  wire signed [15:0] aw_data = a_buf[wcount*16 +: 16];
  wire signed [15:0] gw_data = h2[wcount*16 +: 16];
  wire signed [15:0] dw_data = m_buf[wcount*16 +: 16];

  wire qkv_en = (state == S_QKVW);
  wire ow_en = (state == S_OW);
  wire gu_en = (state == S_GUW);
  wire dw_en = (state == S_DW);
  wire na_start = (state == S_NA0);
  wire nm_start = (state == S_NM0);
  wire qn_start = (state == S_QN0);
  wire kn_start = (state == S_KN0);
  wire rope_start = (state == S_RP0);
  wire attn_start = (state == S_AT0);
  wire o_start = (state == S_OS);
  wire gu_start = (state == S_GUS);
  wire d_start = (state == S_DS);

  wire signed [HD*16-1:0] qn_x = q0[head*HD*16 +: HD*16];
  wire signed [HD*16-1:0] kn_x = k0[kh*HD*16 +: HD*16];

  localparam integer HEADS_M1 = HEADS - 1;
  localparam integer KVHEADS_M1 = KVHEADS - 1;
  wire kv_write = (state == S_KVW) && (kh < KVHEADS);
  wire [15:0] kv_wr_head = kh[15:0];
  wire [15:0] kv_wr_pos = pos_q;
  wire signed [HD*16-1:0] kv_wr_k = (kh < KVHEADS) ? kr[kh*HD*16 +: HD*16] : {HD*16{1'b0}};
  wire signed [HD*16-1:0] kv_wr_v = (kh < KVHEADS) ? v0[kh*HD*16 +: HD*16] : {HD*16{1'b0}};

  wire signed [HIDDEN*16-1:0] attn_y;
  wire signed [HIDDEN*16-1:0] mlpn_y;
  wire signed [HD*16-1:0] qnorm_y;
  wire signed [HD*16-1:0] knorm_y;
  wire signed [ATTN_W*16-1:0] qrope_y;
  wire signed [KV_W*16-1:0] krope_y;
  wire signed [ATTN_W*16-1:0] attn_out;
  wire signed [INTER*16-1:0] silu_y;
  wire signed [HIDDEN*16-1:0] res1_y;
  wire signed [HIDDEN*16-1:0] res2_y;
  wire attn_done;
  wire norm_a_done;
  wire norm_m_done;
  wire qnorm_done;
  wire knorm_done;
  wire qrope_done;
  wire krope_done;

  wire signed [15:0] q_y;
  wire signed [15:0] k_y;
  wire signed [15:0] v_y;
  wire signed [15:0] o_y;
  wire signed [15:0] g_y;
  wire signed [15:0] u_y;
  wire signed [15:0] d_y;
  wire q_yv, k_yv, v_yv, o_yv, g_yv, u_yv, d_yv;
  wire q_done, k_done, v_done, o_done, g_done, u_done, d_done;
  wire [L2R_H-1:0] q_yi, o_yi, d_yi;
  wire [L2R_K-1:0] k_yi, v_yi;
  wire [L2R_I-1:0] g_yi, u_yi;
  wire signed [63:0] t_q, t_k, t_v, t_o, t_g, t_u, t_d;
  wire tv_q, tv_k, tv_v, tv_o, tv_g, tv_u, tv_d;
  wire signed [31:0] r_q, r_k, r_v, r_o, r_g, r_u, r_d;
  wire rv_q, rv_k, rv_v, rv_o, rv_g, rv_u, rv_d;
  wire attn_ov, k_rd_valid, v_rd_valid;

  wire [KV_ADDR-1:0] k_addr;
  wire [KV_ADDR-1:0] v_addr;
  wire signed [HD*16-1:0] k_data;
  wire signed [HD*16-1:0] v_data;
  wire [15:0] k_rd_head = {{(16-KV_ADDR){1'b0}}, k_addr} >> LOG2CONTEXT;
  wire [15:0] v_rd_head = {{(16-KV_ADDR){1'b0}}, v_addr} >> LOG2CONTEXT;
  wire [15:0] ctx_mask = (16'd1 << LOG2CONTEXT) - 16'd1;
  wire [15:0] k_rd_pos = {{(16-KV_ADDR){1'b0}}, k_addr} & ctx_mask;
  wire [15:0] v_rd_pos = {{(16-KV_ADDR){1'b0}}, v_addr} & ctx_mask;
  wire exp_en;
  wire [13:0] exp_index;
  wire [15:0] exp_value;
  // ---- operators ---------------------------------------------------------------------
  tile_rmsnorm #(
    .DIM(HIDDEN), .LOG2DIM($clog2(HIDDEN)), .FN(NORM_FN), .FO(FRAC_H),
    .EPS_Q(EPS_ATTN), .INIT(RSQRT_INIT)
  ) attn_norm (
    .clk(clk), .rst_n(rst_n), .start(na_start), .x(x_q), .weight(attn_w),
    .y(attn_y), .done(norm_a_done)
  );

  tile_linear #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS), .ROWS(HIDDEN), .CHUNKS(CH_H),
    .LOG2CHUNKS(L2CH_H), .LOG2ROWS(L2R_H), .LOG2ADDR(L2A_Q), .LOG2MACS(L2MACS),
    .LOG2CPG(L2CPG), .NGROUPS(NG_H), .FRAC_IN(Q_FI), .FRAC_OUT(Q_FO), .EXP(Q_EXP),
    .INIT0(Q_B0), .INIT1(Q_B1), .INIT2(Q_B2), .INIT3(Q_B3), .INIT_M(Q_M), .INIT_B(Q_BIAS)
  ) q_lin (
    .clk(clk), .rst_n(rst_n), .start(state == S_QKVS),
    .x_wr_en(qkv_en), .x_wr_chunk(chunk_h), .x_wr_lane(lane_w), .x_wr_data(xw_data),
    .y(q_y), .y_valid(q_yv), .y_index(q_yi), .done(q_done),
    .raw_y(r_q), .raw_y_valid(rv_q), .total_y(t_q), .total_valid(tv_q)
  );

  tile_linear #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS), .ROWS(KV_W), .CHUNKS(CH_H),
    .LOG2CHUNKS(L2CH_H), .LOG2ROWS(L2R_K), .LOG2ADDR(L2A_K), .LOG2MACS(L2MACS),
    .LOG2CPG(L2CPG), .NGROUPS(NG_H), .FRAC_IN(K_FI), .FRAC_OUT(K_FO), .EXP(K_EXP),
    .INIT0(K_B0), .INIT1(K_B1), .INIT2(K_B2), .INIT3(K_B3), .INIT_M(K_M), .INIT_B(K_BIAS)
  ) k_lin (
    .clk(clk), .rst_n(rst_n), .start(state == S_QKVS),
    .x_wr_en(qkv_en), .x_wr_chunk(chunk_h), .x_wr_lane(lane_w), .x_wr_data(xw_data),
    .y(k_y), .y_valid(k_yv), .y_index(k_yi), .done(k_done),
    .raw_y(r_k), .raw_y_valid(rv_k), .total_y(t_k), .total_valid(tv_k)
  );

  tile_linear #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS), .ROWS(KV_W), .CHUNKS(CH_H),
    .LOG2CHUNKS(L2CH_H), .LOG2ROWS(L2R_K), .LOG2ADDR(L2A_K), .LOG2MACS(L2MACS),
    .LOG2CPG(L2CPG), .NGROUPS(NG_H), .FRAC_IN(V_FI), .FRAC_OUT(V_FO), .EXP(V_EXP),
    .INIT0(V_B0), .INIT1(V_B1), .INIT2(V_B2), .INIT3(V_B3), .INIT_M(V_M), .INIT_B(V_BIAS)
  ) v_lin (
    .clk(clk), .rst_n(rst_n), .start(state == S_QKVS),
    .x_wr_en(qkv_en), .x_wr_chunk(chunk_h), .x_wr_lane(lane_w), .x_wr_data(xw_data),
    .y(v_y), .y_valid(v_yv), .y_index(v_yi), .done(v_done),
    .raw_y(r_v), .raw_y_valid(rv_v), .total_y(t_v), .total_valid(tv_v)
  );

  tile_rmsnorm #(
    .DIM(HD), .LOG2DIM($clog2(HD)), .FN(NORM_FN), .FO(FRAC_QK),
    .EPS_Q(EPS_HEAD), .INIT(RSQRT_INIT)
  ) q_norm (
    .clk(clk), .rst_n(rst_n), .start(qn_start), .x(qn_x), .weight(qnorm_w),
    .y(qnorm_y), .done(qnorm_done)
  );

  tile_rmsnorm #(
    .DIM(HD), .LOG2DIM($clog2(HD)), .FN(NORM_FN), .FO(FRAC_QK),
    .EPS_Q(EPS_HEAD), .INIT(RSQRT_INIT)
  ) k_norm (
    .clk(clk), .rst_n(rst_n), .start(kn_start), .x(kn_x), .weight(knorm_w),
    .y(knorm_y), .done(knorm_done)
  );

  tile_rope #(
    .HEADS(HEADS), .HD(HD), .STYLE(0), .LOG2HD($clog2(HD)), .LOG2HEADS($clog2(HEADS)),
    .ADDR(ROPE_ADDR), .COS_INIT(ROPE_COS_INIT), .SIN_INIT(ROPE_SIN_INIT)
  ) q_rope (
    .clk(clk), .rst_n(rst_n), .start(rope_start), .position(pos_q), .x(qn),
    .y(qrope_y), .done(qrope_done)
  );

  tile_rope #(
    .HEADS(KVHEADS), .HD(HD), .STYLE(0), .LOG2HD($clog2(HD)), .LOG2HEADS($clog2(KVHEADS)),
    .ADDR(ROPE_ADDR), .COS_INIT(ROPE_COS_INIT), .SIN_INIT(ROPE_SIN_INIT)
  ) k_rope (
    .clk(clk), .rst_n(rst_n), .start(rope_start), .position(pos_q), .x(kn),
    .y(krope_y), .done(krope_done)
  );

  tile_kv #(.HD(HD), .LOG2CONTEXT(LOG2CONTEXT), .ADDR(KV_ADDR)) k_cache (
    .clk(clk), .rst_n(rst_n), .wr_en(kv_write), .wr_head(kv_wr_head), .wr_pos(kv_wr_pos),
    .wr_data(kv_wr_k), .rd_en(1'b1), .rd_head(k_rd_head), .rd_pos(k_rd_pos),
    .rd_data(k_data), .rd_valid(k_rd_valid)
  );

  tile_kv #(.HD(HD), .LOG2CONTEXT(LOG2CONTEXT), .ADDR(KV_ADDR)) v_cache (
    .clk(clk), .rst_n(rst_n), .wr_en(kv_write), .wr_head(kv_wr_head), .wr_pos(kv_wr_pos),
    .wr_data(kv_wr_v), .rd_en(1'b1), .rd_head(v_rd_head), .rd_pos(v_rd_pos),
    .rd_data(v_data), .rd_valid(v_rd_valid)
  );

  tile_attention #(
    .HEADS(HEADS), .KVHEADS(KVHEADS), .HD(HD), .LOG2CONTEXT(LOG2CONTEXT), .ADDR(KV_ADDR),
    .FQ(FRAC_QK), .FK(FRAC_QK), .ATTN_SCALE(ATTN_SCALE), .EXP_BITS(EXP_ADDR)
  ) attention (
    .clk(clk), .rst_n(rst_n), .start(attn_start), .length(pos_q), .q(qr),
    .out(attn_out), .out_valid(attn_ov), .done(attn_done),
    .k_addr(k_addr), .k_data(k_data), .v_addr(v_addr), .v_data(v_data),
    .exp_en(exp_en), .exp_index(exp_index), .exp_value(exp_value)
  );

  tile_lut #(.WIDTH(16), .DEPTH(1 << EXP_ADDR), .ADDR(EXP_ADDR), .INIT(EXP_INIT)) exp_lut (
    .clk(clk), .rst_n(rst_n), .en(exp_en), .index(exp_index[EXP_ADDR-1:0]), .value(exp_value)
  );

  tile_linear #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS), .ROWS(HIDDEN), .CHUNKS(CH_A),
    .LOG2CHUNKS(L2CH_A), .LOG2ROWS(L2R_H), .LOG2ADDR(L2A_O), .LOG2MACS(L2MACS),
    .LOG2CPG(L2CPG), .NGROUPS(NG_A), .FRAC_IN(O_FI), .FRAC_OUT(O_FO), .EXP(O_EXP),
    .INIT0(O_B0), .INIT1(O_B1), .INIT2(O_B2), .INIT3(O_B3), .INIT_M(O_M), .INIT_B(O_BIAS)
  ) o_lin (
    .clk(clk), .rst_n(rst_n), .start(o_start),
    .x_wr_en(ow_en), .x_wr_chunk(chunk_a), .x_wr_lane(lane_w), .x_wr_data(aw_data),
    .y(o_y), .y_valid(o_yv), .y_index(o_yi), .done(o_done),
    .raw_y(r_o), .raw_y_valid(rv_o), .total_y(t_o), .total_valid(tv_o)
  );

  tile_residual #(.DIM(HIDDEN), .WIDTH(16)) res_attn (.a(x_q), .b(o_buf), .y(res1_y));

  tile_rmsnorm #(
    .DIM(HIDDEN), .LOG2DIM($clog2(HIDDEN)), .FN(NORM_FN), .FO(FRAC_H),
    .EPS_Q(EPS_MLP), .INIT(RSQRT_INIT)
  ) mlp_norm (
    .clk(clk), .rst_n(rst_n), .start(nm_start), .x(x1), .weight(mlp_w),
    .y(mlpn_y), .done(norm_m_done)
  );

  tile_linear #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS), .ROWS(INTER), .CHUNKS(CH_H),
    .LOG2CHUNKS(L2CH_H), .LOG2ROWS(L2R_I), .LOG2ADDR(L2A_G), .LOG2MACS(L2MACS),
    .LOG2CPG(L2CPG), .NGROUPS(NG_H), .FRAC_IN(G_FI), .FRAC_OUT(G_FO), .EXP(G_EXP),
    .INIT0(G_B0), .INIT1(G_B1), .INIT2(G_B2), .INIT3(G_B3), .INIT_M(G_M), .INIT_B(G_BIAS)
  ) gate_lin (
    .clk(clk), .rst_n(rst_n), .start(gu_start),
    .x_wr_en(gu_en), .x_wr_chunk(chunk_h), .x_wr_lane(lane_w), .x_wr_data(gw_data),
    .y(g_y), .y_valid(g_yv), .y_index(g_yi), .done(g_done),
    .raw_y(r_g), .raw_y_valid(rv_g), .total_y(t_g), .total_valid(tv_g)
  );

  tile_linear #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS), .ROWS(INTER), .CHUNKS(CH_H),
    .LOG2CHUNKS(L2CH_H), .LOG2ROWS(L2R_I), .LOG2ADDR(L2A_G), .LOG2MACS(L2MACS),
    .LOG2CPG(L2CPG), .NGROUPS(NG_H), .FRAC_IN(U_FI), .FRAC_OUT(U_FO), .EXP(U_EXP),
    .INIT0(U_B0), .INIT1(U_B1), .INIT2(U_B2), .INIT3(U_B3), .INIT_M(U_M), .INIT_B(U_BIAS)
  ) up_lin (
    .clk(clk), .rst_n(rst_n), .start(gu_start),
    .x_wr_en(gu_en), .x_wr_chunk(chunk_h), .x_wr_lane(lane_w), .x_wr_data(gw_data),
    .y(u_y), .y_valid(u_yv), .y_index(u_yi), .done(u_done),
    .raw_y(r_u), .raw_y_valid(rv_u), .total_y(t_u), .total_valid(tv_u)
  );

  tile_silu #(
    .DIM(INTER), .F1(SILU_F1), .F3(SILU_F3), .FG(SILU_FG), .INIT(SIGMOID_INIT)
  ) silu (.h1(g_buf), .h3(u_buf), .g(silu_y));

  tile_linear #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS), .ROWS(HIDDEN), .CHUNKS(CH_I),
    .LOG2CHUNKS(L2CH_I), .LOG2ROWS(L2R_H), .LOG2ADDR(L2A_D), .LOG2MACS(L2MACS),
    .LOG2CPG(L2CPG), .NGROUPS(NG_I), .FRAC_IN(D_FI), .FRAC_OUT(D_FO), .EXP(D_EXP),
    .INIT0(D_B0), .INIT1(D_B1), .INIT2(D_B2), .INIT3(D_B3), .INIT_M(D_M), .INIT_B(D_BIAS)
  ) down_lin (
    .clk(clk), .rst_n(rst_n), .start(d_start),
    .x_wr_en(dw_en), .x_wr_chunk(chunk_i), .x_wr_lane(lane_w), .x_wr_data(dw_data),
    .y(d_y), .y_valid(d_yv), .y_index(d_yi), .done(d_done),
    .raw_y(r_d), .raw_y_valid(rv_d), .total_y(t_d), .total_valid(tv_d)
  );

  tile_residual #(.DIM(HIDDEN), .WIDTH(16)) res_mlp (.a(x1), .b(f_buf), .y(res2_y));
  // ---- the schedule: one state per phase ---------------------------------------------
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state  <= S_IDLE;
      wcount <= 32'd0;
      head   <= 32'd0;
      kh     <= 32'd0;
      pos_q  <= 16'd0;
      x_q    <= {HIDDEN*16{1'b0}};
      x2     <= {HIDDEN*16{1'b0}};
      h      <= {HIDDEN*16{1'b0}};
      x1     <= {HIDDEN*16{1'b0}};
      h2     <= {HIDDEN*16{1'b0}};
      qn     <= {ATTN_W*16{1'b0}};
      qr     <= {ATTN_W*16{1'b0}};
      a_buf  <= {ATTN_W*16{1'b0}};
      kn     <= {KV_W*16{1'b0}};
      kr     <= {KV_W*16{1'b0}};
      m_buf  <= {INTER*16{1'b0}};
    end else begin
      case (state)
        S_IDLE: begin
          if (start) begin
            x_q  <= x;
            pos_q <= position;
            wcount <= 32'd0;
            head <= 32'd0;
            kh   <= 32'd0;
            state <= S_NA0;
          end
        end
        S_NA0: state <= S_NA1;
        S_NA1: begin
          if (norm_a_done) begin
            h <= attn_y;
            state <= S_QKVW;
          end
        end
        S_QKVW: begin
          if (wcount == HIDDEN - 1) begin
            state <= S_QKVS;
          end else begin
            wcount <= wcount + 32'd1;
          end
        end
        S_QKVS: state <= S_QKVWT;
        S_QKVWT: begin
          if (q_done) begin
            head <= 32'd0;
            state <= S_QN0;
          end
        end
        S_QN0: state <= S_QN1;
        S_QN1: begin
          if (qnorm_done) begin
            qn[head*HD*16 +: HD*16] <= qnorm_y;
            if (head == HEADS_M1) begin
              head <= 32'd0;
              state <= S_KN0;
            end else begin
              head <= head + 1'b1;
              state <= S_QN0;
            end
          end
        end
        S_KN0: state <= S_KN1;
        S_KN1: begin
          if (knorm_done) begin
            kn[kh*HD*16 +: HD*16] <= knorm_y;
            if (kh == KVHEADS_M1) begin
              kh <= 32'd0;
              state <= S_RP0;
            end else begin
              kh <= kh + 1'b1;
              state <= S_KN0;
            end
          end
        end
        S_RP0: state <= S_RP1;
        S_RP1: begin
          if (qrope_done) begin
            qr <= qrope_y;
            kr <= krope_y;
            kh <= 32'd0;
            state <= S_KVW;
          end
        end
        S_KVW: begin
          if (kh == KVHEADS) begin
            state <= S_AT0;
          end else begin
            kh <= kh + 1'b1;
          end
        end
        S_AT0: state <= S_AT1;
        S_AT1: begin
          if (attn_done) begin
            a_buf <= attn_out;
            wcount <= 32'd0;
            state <= S_OW;
          end
        end
        S_OW: begin
          if (wcount == ATTN_W - 1) begin
            state <= S_OS;
          end else begin
            wcount <= wcount + 32'd1;
          end
        end
        S_OS: state <= S_OWT;
        S_OWT: begin
          if (o_done) begin
            state <= S_RES1;
          end
        end
        S_RES1: begin
          x1 <= res1_y;
          state <= S_NM0;
        end
        S_NM0: state <= S_NM1;
        S_NM1: begin
          if (norm_m_done) begin
            h2 <= mlpn_y;
            wcount <= 32'd0;
            state <= S_GUW;
          end
        end
        S_GUW: begin
          if (wcount == INTER - 1) begin
            state <= S_GUS;
          end else begin
            wcount <= wcount + 32'd1;
          end
        end
        S_GUS: state <= S_GUWT;
        S_GUWT: begin
          if (g_done) begin
            state <= S_SILU;
          end
        end
        S_SILU: begin
          m_buf <= silu_y;
          wcount <= 32'd0;
          state <= S_DW;
        end
        S_DW: begin
          if (wcount == INTER - 1) begin
            state <= S_DS;
          end else begin
            wcount <= wcount + 32'd1;
          end
        end
        S_DS: state <= S_DWT;
        S_DWT: begin
          if (d_done) begin
            state <= S_RES2;
          end
        end
        S_RES2: begin
          x2 <= res2_y;
          state <= S_DONE;
        end
        S_DONE: state <= S_IDLE;
        default: state <= S_IDLE;
      endcase
    end
  end

  // ---- row results into the layer's buffers ------------------------------------------
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      q0    <= {ATTN_W*16{1'b0}};
      k0    <= {KV_W*16{1'b0}};
      v0    <= {KV_W*16{1'b0}};
      o_buf <= {HIDDEN*16{1'b0}};
      g_buf <= {INTER*16{1'b0}};
      u_buf <= {INTER*16{1'b0}};
      f_buf <= {HIDDEN*16{1'b0}};
    end else begin
      if (q_yv) q0[q_yi*16 +: 16] <= q_y;
      if (k_yv) k0[k_yi*16 +: 16] <= k_y;
      if (v_yv) v0[v_yi*16 +: 16] <= v_y;
      if (o_yv) o_buf[o_yi*16 +: 16] <= o_y;
      if (g_yv) g_buf[g_yi*16 +: 16] <= g_y;
      if (u_yv) u_buf[u_yi*16 +: 16] <= u_y;
      if (d_yv) f_buf[d_yi*16 +: 16] <= d_y;
    end
  end

  assign done = (state == S_DONE);
  assign busy = (state != S_IDLE);
  assign obs_h = h;
  assign obs_x1 = x1;
  assign obs_h2 = h2;
  assign obs_o = o_buf;
  assign obs_f = f_buf;
  assign obs_q = q0;
  assign obs_qn = qn;
  assign obs_qr = qr;
  assign obs_a = a_buf;
  assign obs_k = k0;
  assign obs_v = v0;
  assign obs_kn = kn;
  assign obs_kr = kr;
  assign obs_g = g_buf;
  assign obs_u = u_buf;
  assign obs_m = m_buf;
  assign obs_raw_sum = t_q ^ t_k ^ t_v ^ t_o ^ t_g ^ t_u ^ t_d ^
                       {32'd0, r_q} ^ {32'd0, r_k} ^ {32'd0, r_v} ^ {32'd0, r_o} ^
                       {32'd0, r_g} ^ {32'd0, r_u} ^ {32'd0, r_d};
  assign obs_raw_valid = tv_q | tv_k | tv_v | tv_o | tv_g | tv_u | tv_d |
                         rv_q | rv_k | rv_v | rv_o | rv_g | rv_u | rv_d |
                         k_done | v_done | u_done | krope_done |
                         attn_ov | k_rd_valid | v_rd_valid;

endmodule
