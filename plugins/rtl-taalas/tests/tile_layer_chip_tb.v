// The whole decoder layer under its own schedule: `tile_layer` drives every operator, the host only
// pulses `start` with the token position. Every observation point is compared with GraphL2 in
// tests/test_tile_layer.py; this harness is the B1 evidence that the phase timing lives in the RTL.
`timescale 1ns/1ps
`default_nettype none

module tile_layer_chip_tb ();
  parameter integer HIDDEN = 32;
  parameter integer INTER = 64;
  parameter integer HEADS = 4;
  parameter integer KVHEADS = 2;
  parameter integer HD = 8;
  parameter integer RES = 16;
  parameter integer O_FO = 18;
  parameter integer D_FO = 18;
  parameter signed [63:0] EPS_ATTN = 64'sd68719;
  parameter signed [63:0] EPS_MLP = 64'sd68719;
  localparam integer POSITION = 3;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  always #5 clk = ~clk;

  reg start = 1'b0;
  reg [15:0] position = 16'd3;
  reg signed [HIDDEN*RES-1:0] x = {HIDDEN*RES{1'b0}};
  wire signed [HIDDEN*RES-1:0] x2;
  wire done;
  wire busy;

  wire signed [HIDDEN*16-1:0] obs_h, obs_h2;
  wire signed [HIDDEN*RES-1:0] obs_o, obs_f;
  wire signed [HIDDEN*RES-1:0] obs_x1;
  wire signed [HEADS*HD*16-1:0] obs_q, obs_qn, obs_qr, obs_a;
  wire signed [KVHEADS*HD*16-1:0] obs_k, obs_v, obs_kn, obs_kr;
  wire signed [INTER*16-1:0] obs_g, obs_u, obs_m;
  wire [63:0] obs_raw_sum;
  wire obs_raw_valid;

  tile_layer #(
    .HIDDEN(HIDDEN), .INTER(INTER), .HEADS(HEADS), .KVHEADS(KVHEADS), .HD(HD),
    .LOG2CONTEXT(4), .MACS(8), .BITS(4), .ACC(32), .GROUPS(8), .CPG(2),
    .EPS_HEAD(64'sd1074),
    .ROPE_ADDR(6), .EXP_ADDR(9), .ATTN_SCALE(11585),
    .SILU_F1(15), .SILU_F3(15), .SILU_FG(19),
    .RES(RES), .EPS_ATTN(EPS_ATTN), .EPS_MLP(EPS_MLP),
    .Q_FI(12), .Q_FO(15), .Q_EXP(21),
    .K_FI(12), .K_FO(15), .K_EXP(21),
    .V_FI(12), .V_FO(15), .V_EXP(21),
    .O_FI(15), .O_FO(O_FO), .O_EXP(21),
    .G_FI(12), .G_FO(15), .G_EXP(21),
    .U_FI(12), .U_FO(15), .U_EXP(21),
    .D_FI(19), .D_FO(D_FO), .D_EXP(21),
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
    .D_M("down_m.hex"), .D_BIAS("down_b.hex")
  ) dut (
    .clk(clk), .rst_n(rst_n), .start(start), .position(position), .x(x), .x2(x2),
    .done(done), .busy(busy),
    .obs_h(obs_h), .obs_x1(obs_x1), .obs_h2(obs_h2), .obs_o(obs_o), .obs_f(obs_f),
    .obs_q(obs_q), .obs_qn(obs_qn), .obs_qr(obs_qr), .obs_a(obs_a),
    .obs_k(obs_k), .obs_v(obs_v), .obs_kn(obs_kn), .obs_kr(obs_kr),
    .obs_g(obs_g), .obs_u(obs_u), .obs_m(obs_m),
    .obs_raw_sum(obs_raw_sum), .obs_raw_valid(obs_raw_valid)
  );

  localparam integer STEPS = 4;
  reg [HIDDEN*RES-1:0] xmem [0:STEPS-1];
  reg [1023:0] expbig [0:0];
  reg [HIDDEN*16-1:0] ehm [0:0];
  reg [HIDDEN*RES-1:0] ex1, ex2;
  reg [HIDDEN*RES-1:0] eo, ef;
  reg [HIDDEN*16-1:0] eh, eh2;
  reg [INTER*16-1:0] eg, eu, em;
  reg [511:0] eqm [0:0], ekm [0:0], evm [0:0], eqnm [0:0], eknm [0:0];
  reg [511:0] eqrm [0:0], ekrm [0:0], eam [0:0];
  reg [HIDDEN*16-1:0] eq;
  reg [KVHEADS*HD*16-1:0] ek, ev, ekn, ekr;
  reg [HEADS*HD*16-1:0] eqn, eqr, ea;
  integer ci, errors, timeout, step, up;

`define CHECK(GOT, EXP, LANES, WIDTH, LABEL) \
  for (ci = 0; ci < (LANES); ci = ci + 1) begin \
    if ($signed(GOT[ci*WIDTH +: WIDTH]) !== $signed(EXP[ci*WIDTH +: WIDTH])) begin \
      $display("FAIL %s lane %0d: %0d != %0d", LABEL, ci, $signed(GOT[ci*WIDTH +: WIDTH]), $signed(EXP[ci*WIDTH +: WIDTH])); \
      errors = errors + 1; \
    end \
  end

  initial begin
    errors = 0;
    $readmemh("x.hex", xmem);
    $readmemh("exp_h.hex", ehm); eh = ehm[0];
    $readmemh("exp_q.hex", eqm); eq = eqm[0][HIDDEN*16-1:0];
    $readmemh("exp_k.hex", ekm); ek = ekm[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_v.hex", evm); ev = evm[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_qn.hex", eqnm); eqn = eqnm[0][HEADS*HD*16-1:0];
    $readmemh("exp_kn.hex", eknm); ekn = eknm[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_qr.hex", eqrm); eqr = eqrm[0][HEADS*HD*16-1:0];
    $readmemh("exp_kr.hex", ekrm); ekr = ekrm[0][KVHEADS*HD*16-1:0];
    $readmemh("exp_a.hex", eam); ea = eam[0][HEADS*HD*16-1:0];
    $readmemh("exp_o.hex", expbig); eo = expbig[0][HIDDEN*RES-1:0];
    $readmemh("exp_x1.hex", expbig); ex1 = expbig[0][HIDDEN*RES-1:0];
    $readmemh("exp_h2.hex", ehm); eh2 = ehm[0];
    $readmemh("exp_g.hex", expbig); eg = expbig[0][INTER*16-1:0];
    $readmemh("exp_u.hex", expbig); eu = expbig[0][INTER*16-1:0];
    $readmemh("exp_m.hex", expbig); em = expbig[0][INTER*16-1:0];
    $readmemh("exp_f.hex", expbig); ef = expbig[0][HIDDEN*RES-1:0];
    $readmemh("exp_x2.hex", expbig); ex2 = expbig[0][HIDDEN*RES-1:0];
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
        $display("FAIL the schedule never started at step %0d", step);
        errors = errors + 1;
        step = STEPS;
      end else begin
        timeout = 0;
        while (busy && timeout < 200000) begin
          @(posedge clk);
          timeout = timeout + 1;
        end
        if (busy) begin
          $display("FAIL timeout at step %0d after %0d cycles", step, timeout);
          errors = errors + 1;
          step = STEPS;
        end else begin
          step = step + 1;
        end
      end
    end
    #1;
    if (errors == 0) begin
      `CHECK(obs_h, eh, HIDDEN, 16, "h")
      `CHECK(obs_q, eq, HIDDEN, 16, "q0")
      `CHECK(obs_k, ek, KVHEADS*HD, 16, "k0")
      `CHECK(obs_v, ev, KVHEADS*HD, 16, "v0")
      `CHECK(obs_qn, eqn, HEADS*HD, 16, "qn")
      `CHECK(obs_kn, ekn, KVHEADS*HD, 16, "kn")
      `CHECK(obs_qr, eqr, HEADS*HD, 16, "qr")
      `CHECK(obs_kr, ekr, KVHEADS*HD, 16, "kr")
      `CHECK(obs_a, ea, HEADS*HD, 16, "a")
      `CHECK(obs_o, eo, HIDDEN, RES, "o")
      `CHECK(obs_x1, ex1, HIDDEN, 16, "x1")
      `CHECK(obs_h2, eh2, HIDDEN, 16, "h2")
      `CHECK(obs_g, eg, INTER, 16, "g")
      `CHECK(obs_u, eu, INTER, 16, "u")
      `CHECK(obs_m, em, INTER, 16, "m")
      `CHECK(obs_f, ef, HIDDEN, RES, "f")
      `CHECK(x2, ex2, HIDDEN, 16, "x2")
      if (busy) begin
        $display("FAIL busy stayed high after done");
        errors = errors + 1;
      end
    end
    if (errors == 0) $display("PASS after %0d cycles", timeout);
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
