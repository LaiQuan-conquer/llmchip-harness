// One real Qwen layer: dimensions and numeric parameters from the spec; tests/test_tile_layer_qwen.py drives it.
`timescale 1ns/1ps
`default_nettype none

module tile_layer_qwen_tb ();
  parameter integer HIDDEN = 896;
  parameter integer INTER = 4864;
  parameter integer HEADS = 14;
  parameter integer KVHEADS = 2;
  parameter integer HD = 64;
  parameter integer LOG2CONTEXT = 9;
  parameter integer MACS = 128;
  parameter integer RES = 24;
  parameter integer FRAC_H = 6;
  parameter integer FRAC_QK = 11;
  parameter integer NORM_FN = 10;
  parameter signed [63:0] EPS_ATTN = 64'sd4;
  parameter signed [63:0] EPS_MLP = 64'sd4;
  parameter signed [63:0] EPS_HEAD = 64'sd1;
  parameter integer ROPE_ADDR = 14;
  parameter integer EXP_ADDR = 9;
  parameter integer ATTN_SCALE = 4096;
  parameter integer SILU_F1 = 9;
  parameter integer SILU_F3 = 7;
  parameter integer SILU_FG = 3;
  parameter integer Q_FI = 6, Q_FO = 8, Q_EXP = 21;
  parameter integer K_FI = 6, K_FO = 6, K_EXP = 21;
  parameter integer V_FI = 6, V_FO = 10, V_EXP = 21;
  parameter integer O_FI = 10, O_FO = 11, O_EXP = 21;
  parameter integer G_FI = 6, G_FO = 9, G_EXP = 21;
  parameter integer U_FI = 6, U_FO = 7, U_EXP = 21;
  parameter integer D_FI = 3, D_FO = 11, D_EXP = 21;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  always #5 clk = ~clk;

  reg start = 1'b0;
  reg [15:0] position = 16'd3;
  reg signed [HIDDEN*RES-1:0] x = {HIDDEN*RES{1'b0}};
  wire signed [HIDDEN*RES-1:0] x2;
  wire done, busy;
  wire signed [HIDDEN*16-1:0] obs_h, obs_h2;
  wire signed [HIDDEN*RES-1:0] obs_x1, obs_o, obs_f;
  wire signed [HEADS*HD*16-1:0] obs_q, obs_qn, obs_qr, obs_a;
  wire signed [KVHEADS*HD*16-1:0] obs_k, obs_v, obs_kn, obs_kr;
  wire signed [INTER*16-1:0] obs_g, obs_u, obs_m;

  tile_layer #(
    .HIDDEN(HIDDEN), .INTER(INTER), .HEADS(HEADS), .KVHEADS(KVHEADS), .HD(HD),
    .LOG2CONTEXT(LOG2CONTEXT), .MACS(MACS), .BITS(4), .ACC(32), .GROUPS(8), .CPG(128 / MACS),
    .RES(RES), .QK_NORM(0),
    .FRAC_H(FRAC_H), .FRAC_QK(FRAC_QK), .NORM_FN(NORM_FN),
    .EPS_ATTN(EPS_ATTN), .EPS_MLP(EPS_MLP), .EPS_HEAD(EPS_HEAD),
    .ROPE_ADDR(ROPE_ADDR), .EXP_ADDR(EXP_ADDR), .ATTN_SCALE(ATTN_SCALE),
    .SILU_F1(SILU_F1), .SILU_F3(SILU_F3), .SILU_FG(SILU_FG),
    .Q_FI(Q_FI), .Q_FO(Q_FO), .Q_EXP(Q_EXP),
    .K_FI(K_FI), .K_FO(K_FO), .K_EXP(K_EXP),
    .V_FI(V_FI), .V_FO(V_FO), .V_EXP(V_EXP),
    .O_FI(O_FI), .O_FO(O_FO), .O_EXP(O_EXP),
    .G_FI(G_FI), .G_FO(G_FO), .G_EXP(G_EXP),
    .U_FI(U_FI), .U_FO(U_FO), .U_EXP(U_EXP),
    .D_FI(D_FI), .D_FO(D_FO), .D_EXP(D_EXP),
    .Q_B0("q_bank0.hex"), .Q_B1("q_bank1.hex"), .Q_B2("q_bank2.hex"), .Q_B3("q_bank3.hex"),
    .Q_M("q_m.hex"), .Q_BIAS("q_b.hex"),
    .K_B0("k_bank0.hex"), .K_B1("k_bank1.hex"), .K_B2("k_bank2.hex"), .K_B3("k_bank3.hex"),
    .K_M("k_m.hex"), .K_BIAS("k_b.hex"),
    .V_B0("v_bank0.hex"), .V_B1("v_bank1.hex"), .V_B2("v_bank2.hex"), .V_B3("v_bank3.hex"),
    .V_M("v_m.hex"), .V_BIAS("v_b.hex"),
    .O_B0("o_bank0.hex"), .O_B1("o_bank1.hex"), .O_B2("o_bank2.hex"), .O_B3("o_bank3.hex"),
    .O_M("o_m.hex"), .O_BIAS("o_b.hex"),
    .G_B0("gate_bank0.hex"), .G_B1("gate_bank1.hex"), .G_B2("gate_bank2.hex"), .G_B3("gate_bank3.hex"),
    .G_M("gate_m.hex"), .G_BIAS("gate_b.hex"),
    .U_B0("up_bank0.hex"), .U_B1("up_bank1.hex"), .U_B2("up_bank2.hex"), .U_B3("up_bank3.hex"),
    .U_M("up_m.hex"), .U_BIAS("up_b.hex"),
    .D_B0("down_bank0.hex"), .D_B1("down_bank1.hex"), .D_B2("down_bank2.hex"), .D_B3("down_bank3.hex"),
    .D_M("down_m.hex"), .D_BIAS("down_b.hex"),
    .QNORM_W_INIT("qnorm_w.hex"), .KNORM_W_INIT("knorm_w.hex"),
    .EXP_INIT("lut.exp.hex"), .RSQRT_INIT("rsqrt.hex"),
    .ROPE_COS_INIT("rope_cos.hex"), .ROPE_SIN_INIT("rope_sin.hex"), .SIGMOID_INIT("sigmoid.hex")
  ) dut (
    .clk(clk), .rst_n(rst_n), .start(start), .position(position), .x(x), .x2(x2),
    .done(done), .busy(busy),
    .obs_h(obs_h), .obs_x1(obs_x1), .obs_h2(obs_h2), .obs_o(obs_o), .obs_f(obs_f),
    .obs_q(obs_q), .obs_qn(obs_qn), .obs_qr(obs_qr), .obs_a(obs_a),
    .obs_k(obs_k), .obs_v(obs_v), .obs_kn(obs_kn), .obs_kr(obs_kr),
    .obs_g(obs_g), .obs_u(obs_u), .obs_m(obs_m),
    .obs_raw_sum(), .obs_raw_valid()
  );

  parameter integer POS = 3;
  localparam integer STEPS = POS + 1;
  reg [HIDDEN*RES-1:0] xmem [0:STEPS-1];
  reg [131071:0] big [0:0];
  reg [HIDDEN*RES-1:0] eh1, ex2;
  reg [HIDDEN*16-1:0] eh, eh2;
  reg [HIDDEN*RES-1:0] eo, ef;
  reg [INTER*16-1:0] eg, eu, em;
  reg [HEADS*HD*16-1:0] eq, eqn, eqr, ea;
  reg [KVHEADS*HD*16-1:0] ek, ev, ekn, ekr;
  integer ci, errors, timeout, step, up, checks;

`define CMP(GOT, EXP, LANES, WIDTH, LABEL) \
  checks = checks + 1; \
  for (ci = 0; ci < (LANES); ci = ci + 1) begin \
    if ($signed(GOT[ci*WIDTH +: WIDTH]) !== $signed(EXP[ci*WIDTH +: WIDTH])) begin \
      if (errors < 6) $display("FAIL %s %0d: %0d != %0d", LABEL, ci, $signed(GOT[ci*WIDTH +: WIDTH]), $signed(EXP[ci*WIDTH +: WIDTH])); \
      errors = errors + 1; \
    end \
  end

  initial begin
    errors = 0;
    checks = 0;
    $readmemh("x.hex", xmem);
    $readmemh("exp_h.hex", big); eh = big[0][HIDDEN*16-1:0];
    $readmemh("exp_q.hex", big); eq = big[0][HEADS*HD*16-1:0];
    $readmemh("exp_k.hex", big); ek = big[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_v.hex", big); ev = big[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_qr.hex", big); eqr = big[0][HEADS*HD*16-1:0];
    $readmemh("exp_kr.hex", big); ekr = big[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_qn.hex", big); eqn = big[0][HEADS*HD*16-1:0];
    $readmemh("exp_kn.hex", big); ekn = big[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_a.hex", big); ea = big[0][HEADS*HD*16-1:0];
    $readmemh("exp_o.hex", big); eo = big[0][HIDDEN*RES-1:0];
    $readmemh("exp_x1.hex", big); eh1 = big[0][HIDDEN*RES-1:0];
    $readmemh("exp_h2.hex", big); eh2 = big[0][HIDDEN*16-1:0];
    $readmemh("exp_g.hex", big); eg = big[0][INTER*16-1:0];
    $readmemh("exp_u.hex", big); eu = big[0][INTER*16-1:0];
    $readmemh("exp_m.hex", big); em = big[0][INTER*16-1:0];
    $readmemh("exp_f.hex", big); ef = big[0][HIDDEN*RES-1:0];
    $readmemh("exp_x2.hex", big); ex2 = big[0][HIDDEN*RES-1:0];
    #1;
    repeat (3) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk);
    step = 0;
    while (step < STEPS) begin
      @(posedge clk);
      x = xmem[step];
      position = step[15:0];
      start = 1'b1;
      @(posedge clk);
      start = 1'b0;
      up = 0;
      timeout = 0;
      while (!up && timeout < 20) begin
        @(posedge clk);
        up = busy;
        timeout = timeout + 1;
      end
      if (!up) begin
        $display("FAIL the layer never started at step %0d", step);
        errors = errors + 1;
        step = STEPS;
      end else begin
        timeout = 0;
        while (busy && timeout < 800000) begin
          @(posedge clk);
          timeout = timeout + 1;
        end
        if (busy) begin
          $display("FAIL timeout at step %0d", step);
          errors = errors + 1;
          step = STEPS;
        end else begin
          step = step + 1;
        end
      end
    end
    #1;
    `CMP(obs_h, eh, HIDDEN, 16, "h")
    `CMP(obs_q, eq, HEADS*HD, 16, "q")
    `CMP(obs_k, ek, KVHEADS*HD, 16, "k")
    `CMP(obs_v, ev, KVHEADS*HD, 16, "v")
    `CMP(obs_qn, eqn, HEADS*HD, 16, "qn")
    `CMP(obs_kn, ekn, KVHEADS*HD, 16, "kn")
    `CMP(obs_qr, eqr, HEADS*HD, 16, "qr")
    `CMP(obs_kr, ekr, KVHEADS*HD, 16, "kr")
    `CMP(obs_a, ea, HEADS*HD, 16, "a")
    `CMP(obs_o, eo, HIDDEN, RES, "o")
    `CMP(obs_x1, eh1, HIDDEN, RES, "x1")
    `CMP(obs_h2, eh2, HIDDEN, 16, "h2")
    `CMP(obs_g, eg, INTER, 16, "g")
    `CMP(obs_u, eu, INTER, 16, "u")
    `CMP(obs_m, em, INTER, 16, "m")
    `CMP(obs_f, ef, HIDDEN, RES, "f")
    `CMP(x2, ex2, HIDDEN, RES, "x2")
    $display("CHECKS %0d", checks);
    if (errors == 0) $display("PASS");
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
