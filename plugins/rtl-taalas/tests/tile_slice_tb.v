// Testbench for tile_slice_top: the activation vector and the expected rows come from
// GraphL2's q_proj observation point. The weight banks, mantissas and biases are the generated
// slice files, so the comparison is against the numeric model bit for bit.
`timescale 1ns/1ps
`default_nettype none

module tile_slice_tb;
  parameter integer MACS = 8;
  parameter integer BITS = 4;
  parameter integer GROUPS = 8;
  parameter integer ROWS = 8;
  parameter integer CHUNKS = 4;
  parameter integer LOG2CHUNKS = 2;
  parameter integer LOG2ROWS = 3;
  parameter integer LOG2ADDR = 5;
  parameter integer LOG2MACS = 3;
  parameter integer LOG2CPG = 1;
  parameter integer NGROUPS = 2;
  parameter integer FRAC_IN = 12;
  parameter integer FRAC_OUT = 15;
  parameter integer EXP = 21;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  reg start = 1'b0;
  reg x_wr_en = 1'b0;
  reg [LOG2CHUNKS-1:0] x_wr_chunk = {LOG2CHUNKS{1'b0}};
  reg [LOG2MACS-1:0] x_wr_lane = {LOG2MACS{1'b0}};
  reg signed [15:0] x_wr_data = 16'sd0;

  wire signed [15:0] y;
  wire y_valid;
  wire [LOG2ROWS-1:0] y_index;
  wire done;
  wire signed [31:0] raw_y;
  wire raw_y_valid;

  reg signed [15:0] xmem [0:CHUNKS*MACS-1];
  reg [15:0] expected [0:ROWS-1];
  reg [15:0] got [0:ROWS-1];
  reg seen [0:ROWS-1];

  integer c;
  integer l;
  integer i;
  integer errors;
  integer cycles;
  integer done_seen;

  always #5 clk = ~clk;

  tile_slice_top #(
    .MACS(MACS), .BITS(BITS), .GROUPS(GROUPS), .ROWS(ROWS), .CHUNKS(CHUNKS),
    .LOG2CHUNKS(LOG2CHUNKS), .LOG2ROWS(LOG2ROWS), .LOG2ADDR(LOG2ADDR), .LOG2MACS(LOG2MACS),
    .LOG2CPG(LOG2CPG), .NGROUPS(NGROUPS), .FRAC_IN(FRAC_IN), .FRAC_OUT(FRAC_OUT), .EXP(EXP)
  ) dut (
    .clk(clk), .rst_n(rst_n), .start(start),
    .x_wr_en(x_wr_en), .x_wr_chunk(x_wr_chunk), .x_wr_lane(x_wr_lane), .x_wr_data(x_wr_data),
    .y(y), .y_valid(y_valid), .y_index(y_index), .done(done), .raw_y(raw_y), .raw_y_valid(raw_y_valid)
  );

  always @(posedge clk) begin
    if (y_valid) begin
      got[y_index]  <= y;
      seen[y_index] <= 1'b1;
    end
    if (done) begin
      done_seen <= 1;
    end
  end

  initial begin
    errors = 0;
    cycles = 0;
    done_seen = 0;
    for (i = 0; i < ROWS; i = i + 1) begin
      seen[i] = 1'b0;
      got[i] = 16'd0;
    end
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
    repeat (10) @(posedge clk);
    if (!done_seen) begin
      $display("FAIL timeout after %0d cycles", cycles);
      errors = errors + 1;
    end else begin
      for (i = 0; i < ROWS; i = i + 1) begin
        if (!seen[i]) begin
          $display("FAIL row %0d never emitted", i);
          errors = errors + 1;
        end else if ($signed(got[i]) !== $signed(expected[i])) begin
          $display("FAIL row %0d = %0d, expected %0d", i, $signed(got[i]), $signed(expected[i]));
          errors = errors + 1;
        end
      end
    end
    if (errors == 0) begin
      $display("PASS after %0d cycles", cycles);
    end else begin
      $display("FAIL %0d errors", errors);
    end
    $finish;
  end

endmodule
