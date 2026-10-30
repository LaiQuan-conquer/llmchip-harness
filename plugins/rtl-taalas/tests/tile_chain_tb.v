// The Q-full chain: the host feeds the layer-0 residual, every layer starts on its predecessor's
// done, and the last layer's x2 is compared with GraphL2. tests/test_tile_chain.py drives it.
`timescale 1ns/1ps
`default_nettype none

module tile_chain_tb ();
  parameter integer HIDDEN = 32;
  parameter integer RES = 16;
  parameter integer STEPS = 4;
  parameter integer LAYERS = 2;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  always #5 clk = ~clk;

  reg start = 1'b0;
  reg [15:0] position = 16'd0;
  reg signed [HIDDEN*RES-1:0] x = {HIDDEN*RES{1'b0}};
  wire signed [HIDDEN*RES-1:0] x2;
  wire done;
  wire busy;
  wire [LAYERS*16-1:0] obs_sig;
  wire [63:0] obs_sum;
  wire obs_valid;

  tile_chain dut (
    .clk(clk), .rst_n(rst_n), .start(start), .position(position), .x(x), .x2(x2), .done(done), .busy(busy),
    .obs_sig(obs_sig), .obs_sum(obs_sum), .obs_valid(obs_valid)
  );

  reg [HIDDEN*RES-1:0] xmem [0:STEPS-1];
  reg [1023:0] big [0:0];
  reg [HIDDEN*RES-1:0] expected;
  reg [HIDDEN*RES-1:0] expected_l0;
  reg [HIDDEN*RES-1:0] x2_l0;
  integer ci, errors, timeout, step, up;

  initial begin
    errors = 0;
    $readmemh("x.hex", xmem);
    $readmemh("exp_x2.hex", big);
    expected = big[0][HIDDEN*RES-1:0];
    $readmemh("exp_x2_l0.hex", big);
    expected_l0 = big[0][HIDDEN*RES-1:0];
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
        $display("FAIL the chain never started at step %0d", step);
        errors = errors + 1;
        step = STEPS;
      end else begin
        timeout = 0;
        while (busy && timeout < 400000) begin
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
    x2_l0 = dut.x_w[HIDDEN*RES +: HIDDEN*RES];
    for (ci = 0; ci < HIDDEN; ci = ci + 1) begin
      if ($signed(x2[ci*RES +: RES]) !== $signed(expected[ci*RES +: RES])) begin
        $display("FAIL x2 lane %0d: %0d != %0d", ci, $signed(x2[ci*RES +: RES]), $signed(expected[ci*RES +: RES]));
        errors = errors + 1;
      end
    end
    for (ci = 0; ci < HIDDEN; ci = ci + 1) begin
      if ($signed(x2_l0[ci*RES +: RES]) !== $signed(expected_l0[ci*RES +: RES])) begin
        $display("FAIL x2.0 lane %0d: %0d != %0d", ci, $signed(x2_l0[ci*RES +: RES]), $signed(expected_l0[ci*RES +: RES]));
        errors = errors + 1;
      end
    end
    if (errors == 0) $display("PASS");
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
