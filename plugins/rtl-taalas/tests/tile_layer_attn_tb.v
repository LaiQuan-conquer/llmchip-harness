// The attention half of one taalas-tile/1 layer, wired as a verification harness:
// attn_norm -> q/k/v -> per-head q_norm/k_norm -> RoPE -> KV caches -> attention -> o_proj ->
// residual. The testbench sequences the operators; every observation is compared with GraphL2 in
// tests/test_tile_layer.py. Dimensions are the tiny Qwen3 fixture: hidden 32, 4 heads, 2 KV heads,
// headDim 8, context 16.
`timescale 1ns/1ps
`default_nettype none

module tile_layer_attn_tb;
  localparam integer HIDDEN = 32;
  localparam integer INTER = 64;
  localparam integer HEADS = 4;
  localparam integer KVHEADS = 2;
  localparam integer HD = 8;
  localparam integer LOG2CONTEXT = 4;
  localparam integer KVDEPTH = KVHEADS * (1 << LOG2CONTEXT);
  localparam integer POSITION = 3;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  always #5 clk = ~clk;

  // ---- activations and weights ---------------------------------------------------------
  reg signed [HIDDEN*16-1:0] x = {HIDDEN*16{1'b0}};
  reg signed [HIDDEN*16-1:0] attn_w = {HIDDEN*16{1'b0}};
  reg signed [HIDDEN*16-1:0] h = {HIDDEN*16{1'b0}};
  reg signed [HIDDEN*16-1:0] q0 = {HIDDEN*16{1'b0}};
  reg signed [KVHEADS*HD*16-1:0] k0 = {(KVHEADS*HD*16){1'b0}};
  reg signed [KVHEADS*HD*16-1:0] v0 = {(KVHEADS*HD*16){1'b0}};
  reg signed [HEADS*HD*16-1:0] qn = {(HEADS*HD*16){1'b0}};
  reg signed [KVHEADS*HD*16-1:0] kn = {(KVHEADS*HD*16){1'b0}};
  reg signed [HEADS*HD*16-1:0] qr = {(HEADS*HD*16){1'b0}};
  reg signed [KVHEADS*HD*16-1:0] kr = {(KVHEADS*HD*16){1'b0}};
  reg signed [HEADS*HD*16-1:0] a = {(HEADS*HD*16){1'b0}};
  reg signed [HIDDEN*16-1:0] o = {HIDDEN*16{1'b0}};
  reg signed [HIDDEN*16-1:0] x1 = {HIDDEN*16{1'b0}};
  reg signed [HIDDEN*16-1:0] mlp_w = {HIDDEN*16{1'b0}};
  reg signed [HIDDEN*16-1:0] h2 = {HIDDEN*16{1'b0}};
  reg signed [INTER*16-1:0] g = {INTER*16{1'b0}};
  reg signed [INTER*16-1:0] u = {INTER*16{1'b0}};
  reg signed [INTER*16-1:0] m = {INTER*16{1'b0}};
  reg signed [HIDDEN*16-1:0] f = {HIDDEN*16{1'b0}};
  reg signed [HIDDEN*16-1:0] x2 = {HIDDEN*16{1'b0}};
  reg signed [HD*16-1:0] qnorm_w = {HD*16{1'b0}};
  reg signed [HD*16-1:0] knorm_w = {HD*16{1'b0}};

  // ---- attn_norm -----------------------------------------------------------------------
  reg norm_start = 1'b0;
  wire norm_done;
  wire signed [HIDDEN*16-1:0] attn_y;
  reg attn_seen = 1'b0;
  always @(posedge clk) if (norm_done) attn_seen <= 1'b1;

  tile_rmsnorm #(.DIM(HIDDEN), .LOG2DIM(5), .FN(15), .FO(12), .EPS_Q(64'sd68719), .INIT("rsqrt.hex")) attn_norm (
    .clk(clk), .rst_n(rst_n), .start(norm_start), .x(x), .weight(attn_w), .y(attn_y), .done(norm_done)
  );

  reg mlpn_start = 1'b0;
  wire mlpn_done;
  wire signed [HIDDEN*16-1:0] mlpn_y;
  reg mlpn_seen = 1'b0;
  always @(posedge clk) if (mlpn_done) mlpn_seen <= 1'b1;
  tile_rmsnorm #(.DIM(HIDDEN), .LOG2DIM(5), .FN(15), .FO(12), .EPS_Q(64'sd68719), .INIT("rsqrt.hex")) mlp_norm (
    .clk(clk), .rst_n(rst_n), .start(mlpn_start), .x(x1), .weight(mlp_w), .y(mlpn_y), .done(mlpn_done)
  );

  // ---- q/k/v linear units --------------------------------------------------------------
  reg q_xen = 1'b0, k_xen = 1'b0, v_xen = 1'b0;
  reg [1:0] q_xchunk = 2'd0, k_xchunk = 2'd0, v_xchunk = 2'd0;
  reg [2:0] q_xlane = 3'd0, k_xlane = 3'd0, v_xlane = 3'd0;
  reg signed [15:0] q_xdata = 16'sd0, k_xdata = 16'sd0, v_xdata = 16'sd0;
  reg q_start = 1'b0, k_start = 1'b0, v_start = 1'b0;
  wire signed [15:0] q_y, k_y, v_y;
  wire q_yv, k_yv, v_yv, q_done, k_done, v_done;
  wire [4:0] q_yi;
  wire [3:0] k_yi, v_yi;
  integer q_count = 0, k_count = 0, v_count = 0;
  always @(posedge clk) begin
    if (q_yv) begin q0[q_yi*16 +: 16] <= q_y; q_count <= q_count + 1; end
    if (k_yv) begin k0[k_yi*16 +: 16] <= k_y; k_count <= k_count + 1; end
    if (v_yv) begin v0[v_yi*16 +: 16] <= v_y; v_count <= v_count + 1; end
  end

  tile_linear #(
    .MACS(8), .BITS(4), .ACC(32), .GROUPS(8), .ROWS(HIDDEN), .CHUNKS(4), .LOG2CHUNKS(2),
    .LOG2ROWS(5), .LOG2ADDR(7), .LOG2MACS(3), .LOG2CPG(1), .NGROUPS(2),
    .FRAC_IN(12), .FRAC_OUT(15), .EXP(21),
    .INIT0("q_bank0.hex"), .INIT1("q_bank1.hex"), .INIT2("q_bank2.hex"), .INIT3("q_bank3.hex"),
    .INIT_M("q_m.hex"), .INIT_B("q_b.hex")
  ) q_lin (
    .clk(clk), .rst_n(rst_n), .start(q_start), .x_wr_en(q_xen), .x_wr_chunk(q_xchunk),
    .x_wr_lane(q_xlane), .x_wr_data(q_xdata), .y(q_y), .y_valid(q_yv), .y_index(q_yi), .done(q_done),
    .raw_y(), .raw_y_valid()
  );

  tile_linear #(
    .MACS(8), .BITS(4), .ACC(32), .GROUPS(8), .ROWS(KVHEADS*HD), .CHUNKS(4), .LOG2CHUNKS(2),
    .LOG2ROWS(4), .LOG2ADDR(6), .LOG2MACS(3), .LOG2CPG(1), .NGROUPS(2),
    .FRAC_IN(12), .FRAC_OUT(15), .EXP(21),
    .INIT0("k_bank0.hex"), .INIT1("k_bank1.hex"), .INIT2("k_bank2.hex"), .INIT3("k_bank3.hex"),
    .INIT_M("k_m.hex"), .INIT_B("k_b.hex")
  ) k_lin (
    .clk(clk), .rst_n(rst_n), .start(k_start), .x_wr_en(k_xen), .x_wr_chunk(k_xchunk),
    .x_wr_lane(k_xlane), .x_wr_data(k_xdata), .y(k_y), .y_valid(k_yv), .y_index(k_yi), .done(k_done),
    .raw_y(), .raw_y_valid()
  );

  tile_linear #(
    .MACS(8), .BITS(4), .ACC(32), .GROUPS(8), .ROWS(KVHEADS*HD), .CHUNKS(4), .LOG2CHUNKS(2),
    .LOG2ROWS(4), .LOG2ADDR(6), .LOG2MACS(3), .LOG2CPG(1), .NGROUPS(2),
    .FRAC_IN(12), .FRAC_OUT(15), .EXP(21),
    .INIT0("v_bank0.hex"), .INIT1("v_bank1.hex"), .INIT2("v_bank2.hex"), .INIT3("v_bank3.hex"),
    .INIT_M("v_m.hex"), .INIT_B("v_b.hex")
  ) v_lin (
    .clk(clk), .rst_n(rst_n), .start(v_start), .x_wr_en(v_xen), .x_wr_chunk(v_xchunk),
    .x_wr_lane(v_xlane), .x_wr_data(v_xdata), .y(v_y), .y_valid(v_yv), .y_index(v_yi), .done(v_done),
    .raw_y(), .raw_y_valid()
  );

  // ---- head q_norm / k_norm (one unit reused per head) ---------------------------------
  reg qn_start = 1'b0, kn_start = 1'b0;
  reg signed [HD*16-1:0] qn_x = {HD*16{1'b0}}, kn_x = {HD*16{1'b0}};
  wire signed [HD*16-1:0] qn_y, kn_y;
  wire qn_done, kn_done;
  reg qn_seen = 1'b0, kn_seen = 1'b0;
  always @(posedge clk) begin
    if (qn_done) qn_seen <= 1'b1;
    if (kn_done) kn_seen <= 1'b1;
  end

  tile_rmsnorm #(.DIM(HD), .LOG2DIM(3), .FN(15), .FO(13), .EPS_Q(64'sd1074), .INIT("rsqrt.hex")) q_norm (
    .clk(clk), .rst_n(rst_n), .start(qn_start), .x(qn_x), .weight(qnorm_w), .y(qn_y), .done(qn_done)
  );
  tile_rmsnorm #(.DIM(HD), .LOG2DIM(3), .FN(15), .FO(13), .EPS_Q(64'sd1074), .INIT("rsqrt.hex")) k_norm (
    .clk(clk), .rst_n(rst_n), .start(kn_start), .x(kn_x), .weight(knorm_w), .y(kn_y), .done(kn_done)
  );

  // ---- RoPE ----------------------------------------------------------------------------
  reg qr_start = 1'b0, kr_start = 1'b0;
  reg [15:0] position = POSITION;
  wire signed [HEADS*HD*16-1:0] qr_y;
  wire signed [KVHEADS*HD*16-1:0] kr_y;
  wire qr_done, kr_done;
  reg qr_seen = 1'b0, kr_seen = 1'b0;
  always @(posedge clk) begin
    if (qr_done) qr_seen <= 1'b1;
    if (kr_done) kr_seen <= 1'b1;
  end
  tile_rope #(.HEADS(HEADS), .HD(HD), .STYLE(0), .LOG2HD(3), .LOG2HEADS(2), .ADDR(6)) q_rope (
    .clk(clk), .rst_n(rst_n), .start(qr_start), .position(position), .x(qn), .y(qr_y), .done(qr_done)
  );
  tile_rope #(.HEADS(KVHEADS), .HD(HD), .STYLE(0), .LOG2HD(3), .LOG2HEADS(1), .ADDR(6)) k_rope (
    .clk(clk), .rst_n(rst_n), .start(kr_start), .position(position), .x(kn), .y(kr_y), .done(kr_done)
  );

  // ---- KV caches and attention ---------------------------------------------------------
  reg k_wen = 1'b0, v_wen = 1'b0;
  reg [15:0] wr_head = 16'd0, wr_pos = 16'd0;
  reg signed [HD*16-1:0] k_wdata = {HD*16{1'b0}}, v_wdata = {HD*16{1'b0}};
  wire [4:0] k_addr, v_addr;
  wire [15:0] k_rd_head = {11'd0, k_addr[4]};
  wire [15:0] k_rd_pos = {12'd0, k_addr[3:0]};
  wire [15:0] v_rd_head = {11'd0, v_addr[4]};
  wire [15:0] v_rd_pos = {12'd0, v_addr[3:0]};
  wire signed [HD*16-1:0] k_data, v_data;
  reg attn_start = 1'b0;
  reg [15:0] attn_length = POSITION;
  wire signed [HEADS*HD*16-1:0] attn_out;
  wire attn_done, attn_ov;
  wire exp_en;
  wire [13:0] exp_index;
  wire [15:0] exp_value;
  reg attn_seen2 = 1'b0;
  always @(posedge clk) if (attn_done) attn_seen2 <= 1'b1;

  tile_kv #(.HD(HD), .LOG2CONTEXT(LOG2CONTEXT), .ADDR(5)) k_cache (
    .clk(clk), .rst_n(rst_n), .wr_en(k_wen), .wr_head(wr_head), .wr_pos(wr_pos), .wr_data(k_wdata),
    .rd_en(1'b1), .rd_head(k_rd_head), .rd_pos(k_rd_pos), .rd_data(k_data), .rd_valid()
  );
  tile_kv #(.HD(HD), .LOG2CONTEXT(LOG2CONTEXT), .ADDR(5)) v_cache (
    .clk(clk), .rst_n(rst_n), .wr_en(v_wen), .wr_head(wr_head), .wr_pos(wr_pos), .wr_data(v_wdata),
    .rd_en(1'b1), .rd_head(v_rd_head), .rd_pos(v_rd_pos), .rd_data(v_data), .rd_valid()
  );
  tile_attention #(
    .HEADS(HEADS), .KVHEADS(KVHEADS), .HD(HD), .LOG2CONTEXT(LOG2CONTEXT), .ADDR(5),
    .FQ(13), .FK(13), .ATTN_SCALE(11585)
  ) attention (
    .clk(clk), .rst_n(rst_n), .start(attn_start), .length(attn_length), .q(qr), .out(attn_out),
    .out_valid(attn_ov), .done(attn_done), .k_addr(k_addr), .k_data(k_data), .v_addr(v_addr),
    .v_data(v_data), .exp_en(exp_en), .exp_index(exp_index), .exp_value(exp_value)
  );
  tile_lut #(.WIDTH(16), .DEPTH(512), .ADDR(9), .INIT("lut.exp.hex")) exp_lut (
    .clk(clk), .rst_n(rst_n), .en(exp_en), .index(exp_index[8:0]), .value(exp_value)
  );

  // ---- o_proj and residual -------------------------------------------------------------
  reg o_xen = 1'b0;
  reg [1:0] o_xchunk = 2'd0;
  reg [2:0] o_xlane = 3'd0;
  reg signed [15:0] o_xdata = 16'sd0;
  reg o_start = 1'b0;
  wire signed [15:0] o_y;
  wire o_yv, o_done;
  wire [4:0] o_yi;
  integer o_count = 0;
  always @(posedge clk) if (o_yv) begin o[o_yi*16 +: 16] <= o_y; o_count <= o_count + 1; end

  tile_linear #(
    .MACS(8), .BITS(4), .ACC(32), .GROUPS(8), .ROWS(HIDDEN), .CHUNKS(4), .LOG2CHUNKS(2),
    .LOG2ROWS(5), .LOG2ADDR(7), .LOG2MACS(3), .LOG2CPG(1), .NGROUPS(2),
    .FRAC_IN(15), .FRAC_OUT(18), .EXP(21),
    .INIT0("o_bank0.hex"), .INIT1("o_bank1.hex"), .INIT2("o_bank2.hex"), .INIT3("o_bank3.hex"),
    .INIT_M("o_m.hex"), .INIT_B("o_b.hex")
  ) o_lin (
    .clk(clk), .rst_n(rst_n), .start(o_start), .x_wr_en(o_xen), .x_wr_chunk(o_xchunk),
    .x_wr_lane(o_xlane), .x_wr_data(o_xdata), .y(o_y), .y_valid(o_yv), .y_index(o_yi), .done(o_done),
    .raw_y(), .raw_y_valid()
  );

  reg g_xen = 1'b0, u_xen = 1'b0, d_xen = 1'b0;
  reg [1:0] g_xchunk = 2'd0, u_xchunk = 2'd0;
  reg [2:0] g_xlane = 3'd0, u_xlane = 3'd0;
  reg signed [15:0] g_xdata = 16'sd0, u_xdata = 16'sd0;
  reg [2:0] d_xchunk = 3'd0;
  reg [2:0] d_xlane = 3'd0;
  reg signed [15:0] d_xdata = 16'sd0;
  reg g_start = 1'b0, u_start = 1'b0, d_start = 1'b0;
  wire signed [15:0] g_y, u_y, d_y;
  wire g_yv, u_yv, d_yv;
  wire [5:0] g_yi, u_yi;
  wire [4:0] d_yi;
  integer g_count = 0, u_count = 0, d_count = 0;
  always @(posedge clk) begin
    if (g_yv) begin g[g_yi*16 +: 16] <= g_y; g_count <= g_count + 1; end
    if (u_yv) begin u[u_yi*16 +: 16] <= u_y; u_count <= u_count + 1; end
    if (d_yv) begin
      f[d_yi*16 +: 16] <= d_y;
      d_count <= d_count + 1;
    end
  end

  tile_linear #(
    .MACS(8), .BITS(4), .ACC(32), .GROUPS(8), .ROWS(INTER), .CHUNKS(4), .LOG2CHUNKS(2),
    .LOG2ROWS(6), .LOG2ADDR(8), .LOG2MACS(3), .LOG2CPG(1), .NGROUPS(2),
    .FRAC_IN(12), .FRAC_OUT(15), .EXP(21),
    .INIT0("gate_bank0.hex"), .INIT1("gate_bank1.hex"), .INIT2("gate_bank2.hex"), .INIT3("gate_bank3.hex"),
    .INIT_M("gate_m.hex"), .INIT_B("gate_b.hex")
  ) gate_lin (
    .clk(clk), .rst_n(rst_n), .start(g_start), .x_wr_en(g_xen), .x_wr_chunk(g_xchunk),
    .x_wr_lane(g_xlane), .x_wr_data(g_xdata), .y(g_y), .y_valid(g_yv), .y_index(g_yi), .done(),
    .raw_y(), .raw_y_valid()
  );
  tile_linear #(
    .MACS(8), .BITS(4), .ACC(32), .GROUPS(8), .ROWS(INTER), .CHUNKS(4), .LOG2CHUNKS(2),
    .LOG2ROWS(6), .LOG2ADDR(8), .LOG2MACS(3), .LOG2CPG(1), .NGROUPS(2),
    .FRAC_IN(12), .FRAC_OUT(15), .EXP(21),
    .INIT0("up_bank0.hex"), .INIT1("up_bank1.hex"), .INIT2("up_bank2.hex"), .INIT3("up_bank3.hex"),
    .INIT_M("up_m.hex"), .INIT_B("up_b.hex")
  ) up_lin (
    .clk(clk), .rst_n(rst_n), .start(u_start), .x_wr_en(u_xen), .x_wr_chunk(u_xchunk),
    .x_wr_lane(u_xlane), .x_wr_data(u_xdata), .y(u_y), .y_valid(u_yv), .y_index(u_yi), .done(),
    .raw_y(), .raw_y_valid()
  );
  tile_linear #(
    .MACS(8), .BITS(4), .ACC(32), .GROUPS(8), .ROWS(HIDDEN), .CHUNKS(8), .LOG2CHUNKS(3),
    .LOG2ROWS(5), .LOG2ADDR(8), .LOG2MACS(3), .LOG2CPG(1), .NGROUPS(4),
    .FRAC_IN(19), .FRAC_OUT(18), .EXP(21),
    .INIT0("down_bank0.hex"), .INIT1("down_bank1.hex"), .INIT2("down_bank2.hex"), .INIT3("down_bank3.hex"),
    .INIT_M("down_m.hex"), .INIT_B("down_b.hex")
  ) down_lin (
    .clk(clk), .rst_n(rst_n), .start(d_start), .x_wr_en(d_xen), .x_wr_chunk(d_xchunk),
    .x_wr_lane(d_xlane), .x_wr_data(d_xdata), .y(d_y), .y_valid(d_yv), .y_index(d_yi), .done(),
    .raw_y(), .raw_y_valid()
  );
  tile_silu #(.DIM(INTER), .F1(15), .F3(15), .FG(19), .INIT("sigmoid.hex")) silu (
    .clk(clk), .en(1'b1), .h1(g), .h3(u), .g(m)
  );

  wire signed [HIDDEN*16-1:0] res_y;
  tile_residual #(.DIM(HIDDEN), .WIDTH(16)) res_attn (.a(x), .b(o), .y(res_y));
  wire signed [HIDDEN*16-1:0] res2_y;
  tile_residual #(.DIM(HIDDEN), .WIDTH(16)) res_mlp (.a(x1), .b(f), .y(res2_y));

  // ---- files and sequence --------------------------------------------------------------
  reg [HIDDEN*16-1:0] xmem [0:0];
  reg [HIDDEN*16-1:0] awmem [0:0];
  reg [HIDDEN*16-1:0] mwmem [0:0];
  reg [HD*16-1:0] qnwmem [0:0];
  reg [HD*16-1:0] knwmem [0:0];
  reg [HD*16-1:0] kmem [0:KVDEPTH-1];
  reg [HD*16-1:0] vmem [0:KVDEPTH-1];
  reg [1023:0] expbig [0:0];
  reg [HIDDEN*16-1:0] eh2, ef, ex2;
  reg [INTER*16-1:0] eg, eu, em;
  reg [511:0] ehm [0:0], eqm [0:0], ekm [0:0], evm [0:0], eqnm [0:0], eknm [0:0];
  reg [511:0] eqrm [0:0], ekrm [0:0], eam [0:0], eom [0:0], ex1m [0:0];
  reg [HIDDEN*16-1:0] eh, eq, eo, ex1;
  reg [KVHEADS*HD*16-1:0] ek, ev, ekn, ekr;
  reg [HEADS*HD*16-1:0] eqn, eqr, ea;
  integer i, c, l, hh, errors, cycles, ci;
  reg [15:0] word;
  integer timeout;

`define CHECK(GOT, EXP, LANES, LABEL) \
  for (ci = 0; ci < (LANES); ci = ci + 1) begin \
    if ($signed(GOT[ci*16 +: 16]) !== $signed(EXP[ci*16 +: 16])) begin \
      $display("FAIL lane %0d: %0d != %0d", ci, $signed(GOT[ci*16 +: 16]), $signed(EXP[ci*16 +: 16])); \
      errors = errors + 1; \
    end \
  end

  initial begin
    errors = 0;
    cycles = 0;
    $readmemh("x.hex", xmem);
    $readmemh("attn_w.hex", awmem);
    $readmemh("mlp_w.hex", mwmem);
    $readmemh("qnorm_w.hex", qnwmem);
    $readmemh("knorm_w.hex", knwmem);
    $readmemh("k_cache.hex", kmem);
    $readmemh("v_cache.hex", vmem);
    $readmemh("exp_h.hex", ehm); eh = ehm[0][HIDDEN*16-1:0];
    $readmemh("exp_q.hex", eqm); eq = eqm[0][HIDDEN*16-1:0];
    $readmemh("exp_k.hex", ekm); ek = ekm[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_v.hex", evm); ev = evm[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_qn.hex", eqnm); eqn = eqnm[0][HEADS*HD*16-1:0];
    $readmemh("exp_kn.hex", eknm); ekn = eknm[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_qr.hex", eqrm); eqr = eqrm[0][HEADS*HD*16-1:0];
    $readmemh("exp_kr.hex", ekrm); ekr = ekrm[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_a.hex", eam); ea = eam[0][HEADS*HD*16-1:0];
    $readmemh("exp_o.hex", eom); eo = eom[0][HIDDEN*16-1:0];
    $readmemh("exp_x1.hex", ex1m); ex1 = ex1m[0][HIDDEN*16-1:0];
    $readmemh("exp_h2.hex", ehm); eh2 = ehm[0][HIDDEN*16-1:0];
    $readmemh("exp_g.hex", expbig); eg = expbig[0][INTER*16-1:0];
    $readmemh("exp_u.hex", expbig); eu = expbig[0][INTER*16-1:0];
    $readmemh("exp_m.hex", expbig); em = expbig[0][INTER*16-1:0];
    $readmemh("exp_f.hex", ehm); ef = ehm[0][HIDDEN*16-1:0];
    $readmemh("exp_x2.hex", ehm); ex2 = ehm[0][HIDDEN*16-1:0];
    #1;
    x = xmem[0];
    attn_w = awmem[0];
    qnorm_w = qnwmem[0];
    knorm_w = knwmem[0];
    mlp_w = mwmem[0];
    repeat (3) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk);

    // attn_norm
    norm_start = 1'b1; @(posedge clk); norm_start = 1'b0;
    timeout = 0;
    while (!attn_seen && timeout < 10000) begin @(posedge clk); timeout = timeout + 1; end
    if (!attn_seen) $display("FAIL attn_norm timeout");
    h = attn_y;

    // q/k/v
    @(posedge clk);
    for (c = 0; c < 4; c = c + 1) begin
      for (l = 0; l < 8; l = l + 1) begin
        q_xen = 1'b1; q_xchunk = c; q_xlane = l; q_xdata = h[(c*8 + l)*16 +: 16];
        k_xen = 1'b1; k_xchunk = c; k_xlane = l; k_xdata = h[(c*8 + l)*16 +: 16];
        v_xen = 1'b1; v_xchunk = c; v_xlane = l; v_xdata = h[(c*8 + l)*16 +: 16];
        @(posedge clk);
      end
    end
    q_xen = 1'b0; k_xen = 1'b0; v_xen = 1'b0;
    @(posedge clk);
    q_start = 1'b1; k_start = 1'b1; v_start = 1'b1; @(posedge clk);
    q_start = 1'b0; k_start = 1'b0; v_start = 1'b0;
    timeout = 0;
    while ((q_count < HIDDEN || k_count < KVHEADS*HD || v_count < KVHEADS*HD) && timeout < 100000) begin
      @(posedge clk); timeout = timeout + 1;
    end

    // head norms
    for (hh = 0; hh < HEADS; hh = hh + 1) begin
      qn_x = q0[hh*HD*16 +: HD*16];
      qn_seen = 1'b0; qn_start = 1'b1; @(posedge clk); qn_start = 1'b0;
      timeout = 0; while (!qn_seen && timeout < 10000) begin @(posedge clk); timeout = timeout + 1; end
      qn[hh*HD*16 +: HD*16] = qn_y;
    end
    for (hh = 0; hh < KVHEADS; hh = hh + 1) begin
      kn_x = k0[hh*HD*16 +: HD*16];
      kn_seen = 1'b0; kn_start = 1'b1; @(posedge clk); kn_start = 1'b0;
      timeout = 0; while (!kn_seen && timeout < 10000) begin @(posedge clk); timeout = timeout + 1; end
      kn[hh*HD*16 +: HD*16] = kn_y;
    end

    // RoPE
    qr_seen = 1'b0; kr_seen = 1'b0; qr_start = 1'b1; kr_start = 1'b1; @(posedge clk);
    qr_start = 1'b0; kr_start = 1'b0;
    timeout = 0; while ((!qr_seen || !kr_seen) && timeout < 10000) begin @(posedge clk); timeout = timeout + 1; end
    qr = qr_y; kr = kr_y;

    // KV preload
    for (i = 0; i < KVDEPTH; i = i + 1) begin
      k_wen = 1'b1; v_wen = 1'b1; wr_head = i >> LOG2CONTEXT;
      wr_pos = i & ((1 << LOG2CONTEXT) - 1); k_wdata = kmem[i]; v_wdata = vmem[i];
      @(posedge clk);
    end
    k_wen = 1'b0; v_wen = 1'b0; @(posedge clk);

    // attention
    attn_seen2 = 1'b0; attn_start = 1'b1; @(posedge clk); attn_start = 1'b0;
    timeout = 0; while (!attn_seen2 && timeout < 200000) begin @(posedge clk); timeout = timeout + 1; end
    a = attn_out;

    // o_proj
    @(posedge clk);
    for (c = 0; c < 4; c = c + 1) begin
      for (l = 0; l < 8; l = l + 1) begin
        o_xen = 1'b1; o_xchunk = c; o_xlane = l; o_xdata = a[(c*8 + l)*16 +: 16];
        @(posedge clk);
      end
    end
    o_xen = 1'b0;
    @(posedge clk);
    o_start = 1'b1; @(posedge clk); o_start = 1'b0;
    timeout = 0; while (o_count < HIDDEN && timeout < 100000) begin @(posedge clk); timeout = timeout + 1; end
    #1 x1 = res_y;
    @(posedge clk);

    // mlp_norm
    mlpn_seen = 1'b0; mlpn_start = 1'b1; @(posedge clk); mlpn_start = 1'b0;
    timeout = 0; while (!mlpn_seen && timeout < 10000) begin @(posedge clk); timeout = timeout + 1; end
    h2 = mlpn_y;

    // gate/up
    @(posedge clk);
    for (c = 0; c < 4; c = c + 1) begin
      for (l = 0; l < 8; l = l + 1) begin
        g_xen = 1'b1; g_xchunk = c; g_xlane = l; g_xdata = h2[(c*8 + l)*16 +: 16];
        u_xen = 1'b1; u_xchunk = c; u_xlane = l; u_xdata = h2[(c*8 + l)*16 +: 16];
        @(posedge clk);
      end
    end
    g_xen = 1'b0; u_xen = 1'b0;
    @(posedge clk);
    g_start = 1'b1; u_start = 1'b1; @(posedge clk); g_start = 1'b0; u_start = 1'b0;
    timeout = 0;
    while ((g_count < INTER || u_count < INTER) && timeout < 200000) begin @(posedge clk); timeout = timeout + 1; end
    #1;

    // down
    @(posedge clk);
    for (c = 0; c < 8; c = c + 1) begin
      for (l = 0; l < 8; l = l + 1) begin
        d_xen = 1'b1; d_xchunk = c; d_xlane = l; d_xdata = m[(c*8 + l)*16 +: 16];
        @(posedge clk);
      end
    end
    d_xen = 1'b0;
    @(posedge clk);
    d_start = 1'b1; @(posedge clk); d_start = 1'b0;
    timeout = 0; while (d_count < HIDDEN && timeout < 200000) begin @(posedge clk); timeout = timeout + 1; end
    repeat (2) @(posedge clk);
    #1 x2 = res2_y;

    `CHECK(h, eh, HIDDEN, "h")
    `CHECK(q0, eq, HIDDEN, "q0")
    `CHECK(k0, ek, KVHEADS*HD, "k0")
    `CHECK(v0, ev, KVHEADS*HD, "v0")
    `CHECK(qn, eqn, HEADS*HD, "qn")
    `CHECK(kn, ekn, KVHEADS*HD, "kn")
    `CHECK(qr, eqr, HEADS*HD, "qr")
    `CHECK(kr, ekr, KVHEADS*HD, "kr")
    `CHECK(a, ea, HEADS*HD, "a")
    `CHECK(o, eo, HIDDEN, "o")
    `CHECK(x1, ex1, HIDDEN, "x1")
    `CHECK(h2, eh2, HIDDEN, "h2")
    `CHECK(g, eg, INTER, "g")
    `CHECK(u, eu, INTER, "u")
    `CHECK(m, em, INTER, "m")
    `CHECK(f, ef, HIDDEN, "f")
    begin : dump_x2
      integer fd;
      fd = $fopen("x2.hex", "w");
      for (i = 0; i < HIDDEN; i = i + 1) $fwrite(fd, "%04x\n", x2[i*16 +: 16]);
      $fclose(fd);
    end
    `CHECK(x2, ex2, HIDDEN, "x2")
    if (errors == 0) $display("PASS");
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
