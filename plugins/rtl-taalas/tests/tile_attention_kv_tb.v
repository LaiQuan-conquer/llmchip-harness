// Testbench for tile_attention with the real tile_kv caches: K and V rows of positions 0..p are
// preloaded, then one query position runs. The exp table is tile_lut, as in the RTL.
`timescale 1ns/1ps
`default_nettype none

module tile_attention_kv_tb;
  parameter integer HEADS = 4;
  parameter integer KVHEADS = 2;
  parameter integer HD = 8;
  parameter integer LOG2CONTEXT = 4;
  parameter integer ADDR = 5;
  parameter integer LENGTH = 3;
  parameter integer FQ = 13;
  parameter integer FK = 13;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  reg start = 1'b0;
  reg [15:0] length = LENGTH;
  reg signed [HEADS*HD*16-1:0] q = {(HEADS*HD*16){1'b0}};

  wire signed [HEADS*HD*16-1:0] out;
  wire out_valid;
  wire done;
  wire [ADDR-1:0] k_addr;
  wire [ADDR-1:0] v_addr;
  wire exp_en;
  wire [13:0] exp_index;
  wire [15:0] exp_value;

  reg k_wr_en;
  reg v_wr_en;
  reg [15:0] wr_head;
  reg [15:0] wr_pos;
  reg signed [HD*16-1:0] k_wr_data;
  reg signed [HD*16-1:0] v_wr_data;

  wire [15:0] k_rd_head = {{(16-ADDR+LOG2CONTEXT){1'b0}}, k_addr[ADDR-1:LOG2CONTEXT]};
  wire [15:0] k_rd_pos = {{(16-LOG2CONTEXT){1'b0}}, k_addr[LOG2CONTEXT-1:0]};
  wire [15:0] v_rd_head = {{(16-ADDR+LOG2CONTEXT){1'b0}}, v_addr[ADDR-1:LOG2CONTEXT]};
  wire [15:0] v_rd_pos = {{(16-LOG2CONTEXT){1'b0}}, v_addr[LOG2CONTEXT-1:0]};
  wire signed [HD*16-1:0] k_data;
  wire signed [HD*16-1:0] v_data;

  reg [HD*16-1:0] kmem [0:(1<<ADDR)-1];
  reg [HD*16-1:0] vmem [0:(1<<ADDR)-1];
  reg [HEADS*HD*16-1:0] qmem [0:0];
  reg [15:0] expected [0:HEADS*HD-1];
  integer i;
  integer errors;
  integer cycles;
  reg done_seen = 1'b0;

  always #5 clk = ~clk;

  tile_kv #(.HD(HD), .LOG2CONTEXT(LOG2CONTEXT), .ADDR(ADDR)) k_cache (
    .clk(clk), .rst_n(rst_n), .wr_en(k_wr_en), .wr_head(wr_head), .wr_pos(wr_pos), .wr_data(k_wr_data),
    .rd_en(1'b1), .rd_head(k_rd_head), .rd_pos(k_rd_pos), .rd_data(k_data), .rd_valid()
  );
  tile_kv #(.HD(HD), .LOG2CONTEXT(LOG2CONTEXT), .ADDR(ADDR)) v_cache (
    .clk(clk), .rst_n(rst_n), .wr_en(v_wr_en), .wr_head(wr_head), .wr_pos(wr_pos), .wr_data(v_wr_data),
    .rd_en(1'b1), .rd_head(v_rd_head), .rd_pos(v_rd_pos), .rd_data(v_data), .rd_valid()
  );

  tile_attention #(
    .HEADS(HEADS), .KVHEADS(KVHEADS), .HD(HD), .LOG2CONTEXT(LOG2CONTEXT), .ADDR(ADDR),
    .FQ(FQ), .FK(FK), .ATTN_SCALE(11585)
  ) dut (
    .clk(clk), .rst_n(rst_n), .start(start), .length(length), .q(q), .out(out),
    .out_valid(out_valid), .done(done), .k_addr(k_addr), .k_data(k_data),
    .v_addr(v_addr), .v_data(v_data), .exp_en(exp_en), .exp_index(exp_index), .exp_value(exp_value)
  );

  tile_lut #(.WIDTH(16), .DEPTH(512), .ADDR(9), .INIT("lut.exp.hex")) exp_lut (
    .clk(clk), .rst_n(rst_n), .en(exp_en), .index(exp_index[8:0]), .value(exp_value)
  );

  always @(posedge clk) if (done) done_seen <= 1'b1;

  initial begin
    errors = 0;
    cycles = 0;
    k_wr_en = 1'b0;
    v_wr_en = 1'b0;
    wr_head = 16'd0;
    wr_pos = 16'd0;
    k_wr_data = {HD*16{1'b0}};
    v_wr_data = {HD*16{1'b0}};
    $readmemh("q.hex", qmem);
    $readmemh("k_cache.hex", kmem);
    $readmemh("v_cache.hex", vmem);
    $readmemh("expected.hex", expected);
    q = qmem[0];
    repeat (3) @(posedge clk);
    rst_n = 1'b1;
    // Preload every cache row through the write port.
    for (i = 0; i < (1 << ADDR); i = i + 1) begin
      k_wr_en = 1'b1;
      v_wr_en = 1'b1;
      wr_head = i >> LOG2CONTEXT;
      wr_pos = i & ((1 << LOG2CONTEXT) - 1);
      k_wr_data = kmem[i];
      v_wr_data = vmem[i];
      @(posedge clk);
    end
    k_wr_en = 1'b0;
    v_wr_en = 1'b0;
    @(posedge clk);
    $display("CACHE k0=%h k1=%h kmem0=%h kmem1=%h v0=%h v1=%h vmem0=%h vmem1=%h", k_cache.rows[0], k_cache.rows[1], kmem[0], kmem[1], v_cache.rows[0], v_cache.rows[1], vmem[0], vmem[1]);
    start = 1'b1;
    @(posedge clk);
    start = 1'b0;
    while (!done_seen && cycles < 200000) begin
      @(posedge clk);
      cycles = cycles + 1;
    end
    if (!done_seen) begin
      $display("FAIL timeout");
      errors = errors + 1;
    end else begin
      for (i = 0; i < HEADS*HD; i = i + 1) begin
        if ($signed(out[i*16 +: 16]) !== $signed(expected[i])) begin
          $display("FAIL out[%0d] = %0d, expected %0d", i, $signed(out[i*16 +: 16]), $signed(expected[i]));
          errors = errors + 1;
        end
      end
    end
    if (errors == 0) $display("PASS after %0d cycles", cycles);
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
