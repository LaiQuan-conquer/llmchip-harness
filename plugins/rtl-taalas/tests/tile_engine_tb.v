// Self-checking testbench of the row-major tile_engine: reads the activation chunks (x.hex, CHUNKS
// lines of MACS 16-bit values), the ROM words (rom.hex) and the expected row values (expected.hex,
// ROWS 32-bit entries), runs one phase, collects the y_valid outputs in order and compares.
`default_nettype none

module tile_engine_tb;
  parameter integer MACS = 128;
  parameter integer BITS = 4;
  parameter integer ACC  = 32;
  parameter integer GROUPS = 8;
  parameter integer ROWS = 8;
  parameter integer CHUNKS = 4;
  parameter integer LOG2CHUNKS = 2;
  parameter integer LOG2ROWS = 3;
  parameter integer LOG2ADDR = 5;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  reg start = 1'b0;
  reg x_wr_en = 1'b0;
  reg [LOG2CHUNKS-1:0] x_wr_addr = {LOG2CHUNKS{1'b0}};
  reg [MACS*16-1:0] x_wr_data = {MACS*16{1'b0}};
  wire signed [ACC-1:0] y;
  wire y_valid;
  wire done;

  reg [MACS*16-1:0] xv [0:CHUNKS-1];
  reg [ACC-1:0] ev [0:ROWS-1];

  integer i;
  integer seen;
  integer failures;

  tile_engine #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS),
    .ROWS(ROWS), .CHUNKS(CHUNKS), .LOG2CHUNKS(LOG2CHUNKS),
    .LOG2ROWS(LOG2ROWS), .LOG2ADDR(LOG2ADDR)
  ) dut (
    .clk(clk),
    .rst_n(rst_n),
    .start(start),
    .x_wr_en(x_wr_en),
    .x_wr_addr(x_wr_addr),
    .x_wr_data(x_wr_data),
    .y(y),
    .y_valid(y_valid),
    .done(done)
  );

  always #5 clk = ~clk;

  always @(posedge clk) begin
    if (y_valid) begin
      if (seen < ROWS) begin
        if (y !== ev[seen]) begin
          failures = failures + 1;
          $display("FAIL row %0d got %0d expected %0d", seen, $signed(y), $signed(ev[seen]));
        end
        seen = seen + 1;
      end else begin
        failures = failures + 1;
        $display("FAIL extra output row %0d", seen);
      end
    end
  end

  initial begin
    $readmemh("x.hex", xv);
    $readmemh("expected.hex", ev);
    failures = 0;
    seen = 0;
    repeat (4) @(posedge clk);
    rst_n = 1'b1;
    repeat (2) @(posedge clk);
    for (i = 0; i < CHUNKS; i = i + 1) begin
      x_wr_en = 1'b1;
      x_wr_addr = i[LOG2CHUNKS-1:0];
      x_wr_data = xv[i];
      @(posedge clk);
    end
    x_wr_en = 1'b0;
    @(posedge clk);
    start = 1'b1;
    @(posedge clk);
    start = 1'b0;
    wait (done);
    repeat (4) @(posedge clk);
    if (failures == 0 && seen == ROWS) $display("PASS");
    else $display("FAIL %0d problems, %0d of %0d rows", failures, seen, ROWS);
    $finish;
  end

endmodule
