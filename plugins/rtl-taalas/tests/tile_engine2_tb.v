// Self-checking testbench of tile_engine2 (two streams in lockstep): reads x.hex, rom_a.hex,
// rom_b.hex, expected_a.hex and expected_b.hex, runs one phase and compares both output streams.
`default_nettype none

module tile_engine2_tb;
  parameter integer MACS = 8;
  parameter integer BITS = 4;
  parameter integer ACC  = 32;
  parameter integer GROUPS = 8;
  parameter integer ROWS = 4;
  parameter integer CHUNKS = 2;
  parameter integer LOG2CHUNKS = 1;
  parameter integer LOG2ROWS = 2;
  parameter integer LOG2ADDR = 3;
  parameter integer LOG2MACS = 3;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  reg start = 1'b0;
  reg x_wr_en = 1'b0;
  reg [LOG2CHUNKS-1:0] x_wr_chunk = {LOG2CHUNKS{1'b0}};
  reg [LOG2MACS-1:0] x_wr_lane = {LOG2MACS{1'b0}};
  reg signed [15:0] x_wr_data = 16'sd0;
  wire signed [ACC-1:0] y_a;
  wire signed [ACC-1:0] y_b;
  wire y_valid;
  wire done;

  reg [MACS*16-1:0] xv [0:CHUNKS-1];
  reg [ACC-1:0] ea [0:ROWS-1];
  reg [ACC-1:0] eb [0:ROWS-1];

  integer i;
  integer j;
  integer seen;
  integer failures;

  tile_engine2 #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS),
    .ROWS(ROWS), .CHUNKS(CHUNKS), .LOG2CHUNKS(LOG2CHUNKS),
    .LOG2ROWS(LOG2ROWS), .LOG2ADDR(LOG2ADDR), .LOG2MACS(LOG2MACS),
    .INIT_A0("rom_a_bank0.hex"), .INIT_A1("rom_a_bank1.hex"),
    .INIT_A2("rom_a_bank2.hex"), .INIT_A3("rom_a_bank3.hex"),
    .INIT_B0("rom_b_bank0.hex"), .INIT_B1("rom_b_bank1.hex"),
    .INIT_B2("rom_b_bank2.hex"), .INIT_B3("rom_b_bank3.hex")
  ) dut (
    .clk(clk), .rst_n(rst_n), .start(start),
    .x_wr_en(x_wr_en), .x_wr_chunk(x_wr_chunk), .x_wr_lane(x_wr_lane), .x_wr_data(x_wr_data),
    .y_a(y_a), .y_b(y_b), .y_valid(y_valid), .done(done)
  );

  always #5 clk = ~clk;

  always @(posedge clk) begin
    if (y_valid) begin
      if (seen < ROWS) begin
        if (y_a !== ea[seen]) begin
          failures = failures + 1;
          $display("FAIL stream A row %0d got %0d expected %0d", seen, $signed(y_a), $signed(ea[seen]));
        end
        if (y_b !== eb[seen]) begin
          failures = failures + 1;
          $display("FAIL stream B row %0d got %0d expected %0d", seen, $signed(y_b), $signed(eb[seen]));
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
    $readmemh("expected_a.hex", ea);
    $readmemh("expected_b.hex", eb);
    failures = 0;
    seen = 0;
    repeat (4) @(posedge clk);
    rst_n = 1'b1;
    repeat (2) @(posedge clk);
    for (i = 0; i < CHUNKS; i = i + 1) begin
      for (j = 0; j < MACS; j = j + 1) begin
        x_wr_en = 1'b1;
        x_wr_chunk = i[LOG2CHUNKS-1:0];
        x_wr_lane = j[LOG2MACS-1:0];
        x_wr_data = xv[i][j*16 +: 16];
        @(posedge clk);
      end
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
