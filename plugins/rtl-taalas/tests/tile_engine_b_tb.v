// Self-checking testbench of tile_engine_b (B tokens on one weight stream): reads x.hex (B*CHUNKS
// lines), rom_bank*.hex and expected_b<token>.hex, runs one phase and compares every token's rows.
`default_nettype none

module tile_engine_b_tb;
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
  parameter integer B = 2;
  parameter integer LOG2B = 1;

  reg clk = 1'b0;
  reg rst_n = 1'b0;
  reg start = 1'b0;
  reg x_wr_en = 1'b0;
  reg [LOG2B-1:0] x_wr_token = {LOG2B{1'b0}};
  reg [LOG2CHUNKS-1:0] x_wr_chunk = {LOG2CHUNKS{1'b0}};
  reg [LOG2MACS-1:0] x_wr_lane = {LOG2MACS{1'b0}};
  reg signed [15:0] x_wr_data = 16'sd0;
  wire [B*ACC-1:0] y;
  wire y_valid;
  wire done;

  reg [MACS*16-1:0] xv [0:B*CHUNKS-1];
  reg [ACC-1:0] ev0 [0:ROWS-1];
  reg [ACC-1:0] ev1 [0:ROWS-1];
  reg [ACC-1:0] ev2 [0:ROWS-1];
  reg [ACC-1:0] ev3 [0:ROWS-1];

  integer i;
  integer j;
  integer seen;
  integer failures;
  integer token;

  tile_engine_b #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS),
    .ROWS(ROWS), .CHUNKS(CHUNKS), .LOG2CHUNKS(LOG2CHUNKS),
    .LOG2ROWS(LOG2ROWS), .LOG2ADDR(LOG2ADDR), .LOG2MACS(LOG2MACS),
    .B(B), .LOG2B(LOG2B)
  ) dut (
    .clk(clk), .rst_n(rst_n), .start(start),
    .x_wr_en(x_wr_en), .x_wr_token(x_wr_token), .x_wr_chunk(x_wr_chunk),
    .x_wr_lane(x_wr_lane), .x_wr_data(x_wr_data),
    .y(y), .y_valid(y_valid), .done(done)
  );

  always #5 clk = ~clk;

  always @(posedge clk) begin
    if (y_valid) begin
      if (seen < ROWS) begin
        if (y[0*ACC +: ACC] !== ev0[seen]) begin
          failures = failures + 1;
          $display("FAIL token 0 row %0d got %0d expected %0d", seen, $signed(y[0*ACC +: ACC]), $signed(ev0[seen]));
        end
        if (B > 1 && y[1*ACC +: ACC] !== ev1[seen]) begin
          failures = failures + 1;
          $display("FAIL token 1 row %0d got %0d expected %0d", seen, $signed(y[1*ACC +: ACC]), $signed(ev1[seen]));
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
    $readmemh("expected_0.hex", ev0);
    $readmemh("expected_1.hex", ev1);
    failures = 0;
    seen = 0;
    repeat (4) @(posedge clk);
    rst_n = 1'b1;
    repeat (2) @(posedge clk);
    for (token = 0; token < B; token = token + 1) begin
      for (i = 0; i < CHUNKS; i = i + 1) begin
        for (j = 0; j < MACS; j = j + 1) begin
          x_wr_en = 1'b1;
          x_wr_token = token[LOG2B-1:0];
          x_wr_chunk = i[LOG2CHUNKS-1:0];
          x_wr_lane = j[LOG2MACS-1:0];
          x_wr_data = xv[token*CHUNKS + i][j*16 +: 16];
          @(posedge clk);
        end
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
