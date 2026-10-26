// Testbench for tile_attention: q, one position's KV cache, the exp table and the expected output
// are written by tests/test_tile_attention.py. The KV model has the same one-cycle read latency as
// tile_kv, and the exp table is tile_lut itself, so the RTL's wait states are exercised.
`timescale 1ns/1ps
`default_nettype none

module tile_attention_tb;
  parameter integer HEADS = 2;
  parameter integer KVHEADS = 1;
  parameter integer HD = 8;
  parameter integer LOG2CONTEXT = 3;
  parameter integer ADDR = 4;
  parameter integer FQ = 15;
  parameter integer FK = 15;
  parameter integer ATTN_SCALE = 11585;
  parameter integer S = 3;

  localparam integer MAXS = 1 << LOG2CONTEXT;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  reg start = 1'b0;
  reg [15:0] length = 16'd0;
  reg signed [HEADS*HD*16-1:0] q = {HEADS*HD*16{1'b0}};

  wire signed [HEADS*HD*16-1:0] out;
  wire out_valid;
  wire done;
  wire [ADDR-1:0] k_addr;
  wire [ADDR-1:0] v_addr;
  wire exp_en;
  wire [13:0] exp_index;
  wire [15:0] exp_value;

  reg signed [HD*16-1:0] k_data = {HD*16{1'b0}};
  reg signed [HD*16-1:0] v_data = {HD*16{1'b0}};

  reg [HEADS*HD*16-1:0] qmem [0:0];
  reg [HD*16-1:0] kmem [0:(1<<ADDR)-1];
  reg [HD*16-1:0] vmem [0:(1<<ADDR)-1];
  reg [15:0] expected [0:HEADS*HD-1];

  integer i;
  integer errors;
  integer cycles;
  integer valid_count;

  always #5 clk = ~clk;

  // The KV cache's synchronous read: the row for the address present before this edge.
  always @(posedge clk) begin
    k_data <= kmem[k_addr];
    v_data <= vmem[v_addr];
  end

  tile_attention #(
    .HEADS(HEADS), .KVHEADS(KVHEADS), .HD(HD), .LOG2CONTEXT(LOG2CONTEXT), .ADDR(ADDR),
    .FQ(FQ), .FK(FK), .ATTN_SCALE(ATTN_SCALE)
  ) dut (
    .clk(clk), .rst_n(rst_n), .start(start), .length(length), .q(q), .out(out),
    .out_valid(out_valid), .done(done), .k_addr(k_addr), .k_data(k_data),
    .v_addr(v_addr), .v_data(v_data), .exp_en(exp_en), .exp_index(exp_index),
    .exp_value(exp_value)
  );

  tile_lut #(.WIDTH(16), .DEPTH(512), .ADDR(9), .INIT("lut.exp.hex")) lut (
    .clk(clk), .rst_n(rst_n), .en(exp_en), .index(exp_index[8:0]), .value(exp_value)
  );

  always @(posedge clk) begin
    if (out_valid) begin
      valid_count <= valid_count + 1;
    end
  end

  initial begin
    errors = 0;
    cycles = 0;
    valid_count = 0;
    $readmemh("q.hex", qmem);
    $readmemh("k.hex", kmem);
    $readmemh("v.hex", vmem);
    $readmemh("expected.hex", expected);
    q = qmem[0];
    length = S - 1;
    repeat (3) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk);
    start = 1'b1;
    @(posedge clk);
    start = 1'b0;

    while (!done && cycles < 400000) begin
      @(posedge clk);
      cycles = cycles + 1;
    end

    if (!done) begin
      $display("FAIL timeout after %0d cycles", cycles);
      $finish;
    end
    if (valid_count != HEADS) begin
      $display("FAIL out_valid count %0d, expected %0d", valid_count, HEADS);
      errors = errors + 1;
    end
    for (i = 0; i < HEADS*HD; i = i + 1) begin
      if ($signed(out[i*16 +: 16]) !== $signed(expected[i])) begin
        $display("FAIL out[%0d] = %0d, expected %0d", i, $signed(out[i*16 +: 16]), $signed(expected[i]));
        errors = errors + 1;
      end
    end
    if (errors == 0) begin
      $display("PASS after %0d cycles", cycles);
    end else begin
      $display("FAIL %0d mismatches", errors);
    end
    $finish;
  end

endmodule
