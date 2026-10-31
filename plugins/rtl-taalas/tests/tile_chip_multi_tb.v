// Many prompts through one chip: each START runs the token and LENGTH tokens, and every emitted
// token is compared with L2's greedy sequence. tests/test_tile_chip_prompts.py drives it.
`timescale 1ns/1ps
`default_nettype none

module tile_chip_multi_tb ();
  parameter integer HIDDEN = 32;
  parameter integer RES = 16;
  parameter integer LAYERS = 2;
  parameter integer PROMPTS = 20;
  parameter integer LENGTH = 8;

  localparam [3:0] S_EMIT = 4'd11;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  always #5 clk = ~clk;

  reg start = 1'b0;
  reg [23:0] token = 24'd0;
  reg [15:0] length = LENGTH;
  wire busy, done;
  wire [23:0] next_token;

  tile_chip dut (
    .clk(clk), .rst_n(rst_n), .start(start), .token(token), .position(16'd0), .length(length),
    .busy(busy), .done(done), .next_token(next_token), .obs_addr(16'd0), .obs_word(),
    .dbg_sig(), .dbg_sum(), .dbg_valid()
  );

  reg [23:0] prompts [0:PROMPTS-1];
  reg [23:0] expected [0:PROMPTS*LENGTH-1];
  integer p, errors, timeout, up, emit_idx;

  always @(posedge clk) begin
    if (dut.state == S_EMIT) begin
      if (dut.best_idx !== expected[p*LENGTH + emit_idx]) begin
        if (errors < 8) $display("FAIL prompt %0d token %0d: %0d != %0d", p, emit_idx, dut.best_idx, expected[p*LENGTH + emit_idx]);
        errors = errors + 1;
      end
      emit_idx = emit_idx + 1;
    end
  end

  initial begin
    errors = 0;
    $readmemh("prompts.hex", prompts);
    $readmemh("expected.hex", expected);
    repeat (3) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk);
    for (p = 0; p < PROMPTS; p = p + 1) begin
      emit_idx = 0;
      @(posedge clk);
      token = prompts[p];
      length = LENGTH;
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
        $display("FAIL prompt %0d never started", p);
        errors = errors + 1;
      end else begin
        timeout = 0;
        while (!done && timeout < 100000000) begin
          @(posedge clk);
          timeout = timeout + 1;
        end
        if (!done) begin
          $display("FAIL prompt %0d timeout", p);
          errors = errors + 1;
        end else if (emit_idx != LENGTH) begin
          $display("FAIL prompt %0d emitted %0d tokens", p, emit_idx);
          errors = errors + 1;
        end
      end
    end
    if (errors == 0) $display("PASS");
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
