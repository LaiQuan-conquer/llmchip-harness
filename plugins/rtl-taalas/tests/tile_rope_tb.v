// Testbench for tile_rope: the q/k vectors after q_norm/k_norm, the position and the expected
// rope outputs are written by tests/test_tile_normalization.py from GraphL2.rope.
`timescale 1ns/1ps
`default_nettype none

module tile_rope_tb;
  parameter integer QHEADS = 4;
  parameter integer KHEADS = 2;
  parameter integer HD = 8;
  parameter integer LOG2HD = 3;
  parameter integer STYLE = 0;
  parameter integer ADDR = 6;
  parameter integer POSITION = 0;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  reg start = 1'b0;
  reg [15:0] position = POSITION;
  reg signed [QHEADS*HD*16-1:0] q = {(QHEADS*HD*16){1'b0}};
  reg signed [KHEADS*HD*16-1:0] k = {(KHEADS*HD*16){1'b0}};
  wire signed [QHEADS*HD*16-1:0] q_out;
  wire signed [KHEADS*HD*16-1:0] k_out;
  wire q_done;
  wire k_done;

  reg [QHEADS*HD*16-1:0] qmem [0:0];
  reg [KHEADS*HD*16-1:0] kmem [0:0];
  reg [15:0] expected_q [0:QHEADS*HD-1];
  reg [15:0] expected_k [0:KHEADS*HD-1];
  integer i;
  integer errors;
  integer cycles;
  reg q_seen = 1'b0;
  reg k_seen = 1'b0;

  always #5 clk = ~clk;

  always @(posedge clk) begin
    if (q_done) q_seen <= 1'b1;
    if (k_done) k_seen <= 1'b1;
  end

  tile_rope #(.HEADS(QHEADS), .HD(HD), .STYLE(STYLE), .LOG2HD(LOG2HD), .LOG2HEADS(2), .ADDR(ADDR)) q_rope (
    .clk(clk), .rst_n(rst_n), .start(start), .position(position), .x(q), .y(q_out), .done(q_done)
  );

  tile_rope #(.HEADS(KHEADS), .HD(HD), .STYLE(STYLE), .LOG2HD(LOG2HD), .LOG2HEADS(1), .ADDR(ADDR)) k_rope (
    .clk(clk), .rst_n(rst_n), .start(start), .position(position), .x(k), .y(k_out), .done(k_done)
  );

  initial begin
    errors = 0;
    cycles = 0;
    $readmemh("q.hex", qmem);
    $readmemh("k.hex", kmem);
    $readmemh("expected_q.hex", expected_q);
    $readmemh("expected_k.hex", expected_k);
    q = qmem[0];
    k = kmem[0];
    repeat (3) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk);
    start = 1'b1;
    @(posedge clk);
    start = 1'b0;
    while ((!q_seen || !k_seen) && cycles < 100000) begin
      @(posedge clk);
      cycles = cycles + 1;
    end
    if (!q_seen || !k_seen) begin
      $display("FAIL timeout");
      errors = errors + 1;
    end else begin
      for (i = 0; i < QHEADS*HD; i = i + 1) begin
        if ($signed(q_out[i*16 +: 16]) !== $signed(expected_q[i])) begin
          $display("FAIL q[%0d] = %0d, expected %0d", i, $signed(q_out[i*16 +: 16]), $signed(expected_q[i]));
          errors = errors + 1;
        end
      end
      for (i = 0; i < KHEADS*HD; i = i + 1) begin
        if ($signed(k_out[i*16 +: 16]) !== $signed(expected_k[i])) begin
          $display("FAIL k[%0d] = %0d, expected %0d", i, $signed(k_out[i*16 +: 16]), $signed(expected_k[i]));
          errors = errors + 1;
        end
      end
    end
    if (errors == 0) $display("PASS after %0d cycles", cycles);
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
