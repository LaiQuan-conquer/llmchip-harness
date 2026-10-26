// Final norm + lm_head for the tiny 2-layer model: the host-pin logits tail of the chip.
`timescale 1ns/1ps
`default_nettype none

module tile_tail_tb;
  localparam integer HIDDEN = 32;
  localparam integer VOCAB = 128;
  localparam integer SHIFT = 12 + 21 - 24;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  always #5 clk = ~clk;

  reg signed [HIDDEN*16-1:0] x2 = {HIDDEN*16{1'b0}};
  reg signed [HIDDEN*16-1:0] final_w = {HIDDEN*16{1'b0}};
  reg signed [HIDDEN*16-1:0] h = {HIDDEN*16{1'b0}};
  reg norm_start = 1'b0;
  wire norm_done;
  wire signed [HIDDEN*16-1:0] norm_y;
  reg norm_seen = 1'b0;
  always @(posedge clk) if (norm_done) norm_seen <= 1'b1;

  tile_rmsnorm #(.DIM(HIDDEN), .LOG2DIM(5), .FN(15), .FO(12), .EPS_Q(64'sd68719), .INIT("rsqrt.hex")) final_norm (
    .clk(clk), .rst_n(rst_n), .start(norm_start), .x(x2), .weight(final_w), .y(norm_y), .done(norm_done)
  );

  reg lm_xen = 1'b0;
  reg [1:0] lm_xchunk = 2'd0;
  reg [2:0] lm_xlane = 3'd0;
  reg signed [15:0] lm_xdata = 16'sd0;
  reg lm_start = 1'b0;
  wire signed [15:0] lm_y;
  wire lm_yv;
  wire [6:0] lm_yi;
  wire lm_done;
  wire signed [63:0] lm_total;
  wire lm_total_v;
  reg signed [31:0] logits [0:VOCAB-1];
  integer lm_count = 0;

  always @(posedge clk) begin
    if (lm_total_v) begin
      logits[lm_yi] <= (lm_total + (64'sd1 <<< (SHIFT-1))) >>> SHIFT;
      lm_count <= lm_count + 1;
    end
  end

  tile_linear #(
    .MACS(8), .BITS(4), .ACC(32), .GROUPS(8), .ROWS(VOCAB), .CHUNKS(4), .LOG2CHUNKS(2),
    .LOG2ROWS(7), .LOG2ADDR(9), .LOG2MACS(3), .LOG2CPG(1), .NGROUPS(2),
    .FRAC_IN(12), .FRAC_OUT(24), .EXP(21),
    .INIT0("lm_bank0.hex"), .INIT1("lm_bank1.hex"), .INIT2("lm_bank2.hex"), .INIT3("lm_bank3.hex"),
    .INIT_M("lm_m.hex"), .INIT_B("lm_b.hex")
  ) lm_head (
    .clk(clk), .rst_n(rst_n), .start(lm_start),
    .x_wr_en(lm_xen), .x_wr_chunk(lm_xchunk), .x_wr_lane(lm_xlane), .x_wr_data(lm_xdata),
    .y(lm_y), .y_valid(lm_yv), .y_index(lm_yi), .done(lm_done),
    .raw_y(), .raw_y_valid(), .total_y(lm_total), .total_valid(lm_total_v)
  );

  reg [HIDDEN*16-1:0] x2mem [0:0];
  reg [HIDDEN*16-1:0] fwmem [0:0];
  reg [31:0] expected [0:VOCAB-1];
  integer i, c, l, errors, timeout, fd;

  initial begin
    errors = 0;
    $readmemh("x2.hex", x2mem);
    $readmemh("final_w.hex", fwmem);
    $readmemh("expected_logits.hex", expected);
    x2 = x2mem[0];
    final_w = fwmem[0];
    repeat (3) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk);

    norm_seen = 1'b0; norm_start = 1'b1; @(posedge clk); norm_start = 1'b0;
    timeout = 0; while (!norm_seen && timeout < 10000) begin @(posedge clk); timeout = timeout + 1; end
    h = norm_y;

    for (c = 0; c < 4; c = c + 1) begin
      for (l = 0; l < 8; l = l + 1) begin
        lm_xen = 1'b1; lm_xchunk = c; lm_xlane = l; lm_xdata = h[(c*8 + l)*16 +: 16];
        @(posedge clk);
      end
    end
    lm_xen = 1'b0; @(posedge clk);
    lm_start = 1'b1; @(posedge clk); lm_start = 1'b0;
    timeout = 0; while (lm_count < VOCAB && timeout < 200000) begin @(posedge clk); timeout = timeout + 1; end

    for (i = 0; i < VOCAB; i = i + 1) begin
      if (logits[i] !== expected[i]) begin
        $display("FAIL logits[%0d] = %0d, expected %0d", i, logits[i], expected[i]);
        errors = errors + 1;
      end
    end
    fd = $fopen("logits.hex", "w");
    for (i = 0; i < VOCAB; i = i + 1) $fwrite(fd, "%08x\n", logits[i]);
    $fclose(fd);
    if (errors == 0) $display("PASS");
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
