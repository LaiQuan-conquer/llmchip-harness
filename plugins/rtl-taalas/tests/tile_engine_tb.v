// Self-checking testbench of tile_engine: reads the activations (x.hex, WORDS entries), the ROM
// word file (rom.hex) and the expected accumulators (expected.hex, MACS 32-bit entries), runs one
// phase and compares. $display("PASS") or "FAIL <lane> <got> <expected>".
`default_nettype none

module tile_engine_tb;
  parameter integer MACS = 8;
  parameter integer BITS = 4;
  parameter integer ACC  = 32;
  parameter integer LOG2ADDR = 2;
  parameter integer WORDS = 4;
  parameter integer LOG2WORDS = 2;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  reg start = 1'b0;
  reg x_wr_en = 1'b0;
  reg [15:0] x_wr_addr = 16'd0;
  reg signed [15:0] x_wr_data = 16'sd0;
  wire [MACS*ACC-1:0] y;
  wire done;

  reg signed [15:0] xv [0:WORDS-1];
  reg [31:0] ev [0:MACS-1];

  integer i;
  integer failures;

  tile_engine #(.MACS(MACS), .BITS(BITS), .ACC(ACC), .LOG2ADDR(LOG2ADDR), .WORDS(WORDS), .LOG2WORDS(LOG2WORDS)) dut (
    .clk(clk),
    .rst_n(rst_n),
    .start(start),
    .x_wr_en(x_wr_en),
    .x_wr_addr(x_wr_addr),
    .x_wr_data(x_wr_data),
    .y(y),
    .done(done)
  );

  always #5 clk = ~clk;

  initial begin

    $readmemh("x.hex", xv);
    $readmemh("expected.hex", ev);
    failures = 0;
    repeat (4) @(posedge clk);
    rst_n = 1'b1;
    repeat (2) @(posedge clk);
    for (i = 0; i < WORDS; i = i + 1) begin
      x_wr_en = 1'b1;
      x_wr_addr = i[15:0];
      x_wr_data = xv[i];
      @(posedge clk);
    end
    x_wr_en = 1'b0;
    @(posedge clk);
    start = 1'b1;
    @(posedge clk);
    start = 1'b0;
    wait (done);
    @(posedge clk);
    for (i = 0; i < MACS; i = i + 1) begin
      if (y[i*ACC +: ACC] !== ev[i]) begin
        failures = failures + 1;
        $display("FAIL lane %0d got %0d expected %0d", i, $signed(y[i*ACC +: ACC]), $signed(ev[i]));
      end
    end
    if (failures == 0) $display("PASS");
    else $display("FAIL %0d lanes", failures);
    $finish;
  end

endmodule
