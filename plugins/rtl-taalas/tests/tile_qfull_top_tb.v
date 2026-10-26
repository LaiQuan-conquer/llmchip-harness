// Testbench for tile_qfull_top: one weight bank set and one activation vector are shared by every
// layer, so the last layer's rows are the same reference values for the serial and pipelined
// handshakes. tests/test_tile_qfull_top.py writes x.hex, expected.hex and the bank files.
`timescale 1ns/1ps
`default_nettype none

module tile_qfull_top_tb;
  parameter integer LAYERS = 3;
  parameter integer MACS = 8;
  parameter integer BITS = 4;
  parameter integer ACC = 32;
  parameter integer GROUPS = 4;
  parameter integer ROWS = 3;
  parameter integer CHUNKS = 2;
  parameter integer LOG2CHUNKS = 1;
  parameter integer LOG2ROWS = 2;
  parameter integer LOG2ADDR = 3;
  parameter integer LOG2MACS = 3;
  parameter integer PIPELINED = 0;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  reg start = 1'b0;
  reg x_wr_en = 1'b0;
  reg [LOG2CHUNKS-1:0] x_wr_chunk = {LOG2CHUNKS{1'b0}};
  reg [LOG2MACS-1:0] x_wr_lane = {LOG2MACS{1'b0}};
  reg signed [15:0] x_wr_data = 16'sd0;

  wire [7:0] layer;
  wire signed [ACC-1:0] y;
  wire y_valid;
  wire done;

  reg signed [15:0] xmem [0:CHUNKS*MACS-1];
  reg [ACC-1:0] expected [0:ROWS-1];

  integer c;
  integer l;
  integer got;
  integer errors;
  integer cycles;
  integer done_seen;

  always #5 clk = ~clk;

  tile_qfull_top #(
    .LAYERS(LAYERS), .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS), .ROWS(ROWS),
    .CHUNKS(CHUNKS), .LOG2CHUNKS(LOG2CHUNKS), .LOG2ROWS(LOG2ROWS), .LOG2ADDR(LOG2ADDR),
    .LOG2MACS(LOG2MACS), .PIPELINED(PIPELINED)
  ) dut (
    .clk(clk), .rst_n(rst_n), .start(start),
    .x_wr_en(x_wr_en), .x_wr_chunk(x_wr_chunk), .x_wr_lane(x_wr_lane), .x_wr_data(x_wr_data),
    .layer(layer), .y(y), .y_valid(y_valid), .done(done)
  );

  always @(posedge clk) begin
    if (done) begin
      done_seen <= 1;
    end
    if (y_valid && layer == LAYERS-1 && got < ROWS) begin
      if ($signed(y) !== $signed(expected[got])) begin
        $display("FAIL y[%0d] = %0d, expected %0d", got, $signed(y), $signed(expected[got]));
        errors <= errors + 1;
      end
      got <= got + 1;
    end
  end

  initial begin
    errors = 0;
    got = 0;
    cycles = 0;
    done_seen = 0;
    $readmemh("x.hex", xmem);
    $readmemh("expected.hex", expected);
    repeat (3) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk);

    for (c = 0; c < CHUNKS; c = c + 1) begin
      for (l = 0; l < MACS; l = l + 1) begin
        x_wr_en = 1'b1;
        x_wr_chunk = c;
        x_wr_lane = l;
        x_wr_data = xmem[c*MACS + l];
        @(posedge clk);
      end
    end
    x_wr_en = 1'b0;
    @(posedge clk);

    start = 1'b1;
    @(posedge clk);
    start = 1'b0;

    while (!done_seen && cycles < 200000) begin
      @(posedge clk);
      cycles = cycles + 1;
    end
    repeat (4) @(posedge clk);

    if (!done_seen) begin
      $display("FAIL timeout after %0d cycles", cycles);
    end else if (got != ROWS) begin
      $display("FAIL got %0d rows, expected %0d", got, ROWS);
      errors = errors + 1;
    end else if (layer != LAYERS-1) begin
      $display("FAIL final layer %0d, expected %0d", layer, LAYERS-1);
      errors = errors + 1;
    end else if (errors == 0) begin
      $display("PASS after %0d cycles", cycles);
    end
    if (errors != 0) begin
      $display("FAIL %0d errors", errors);
    end
    $finish;
  end

endmodule
