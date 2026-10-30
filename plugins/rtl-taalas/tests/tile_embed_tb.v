// One embedding row of the tiny model against GraphL2.
`timescale 1ns/1ps
`default_nettype none

module tile_embed_tb ();
  parameter integer HIDDEN = 32;
  parameter integer RES = 16;
  reg clk = 1'b0;
  reg rst_n = 1'b0;
  always #5 clk = ~clk;

  reg start = 1'b0;
  reg [23:0] token = 24'd3;
  wire signed [HIDDEN*RES-1:0] x;
  wire done;

  tile_embed #(.HIDDEN(HIDDEN), .VOCAB(128), .GROUP(16), .MACS(8), .BITS(4), .CPG(2),
    .RES(RES), .EXP(21), .FRAC_X(18),
    .INIT0("embed_bank0.hex"), .INIT1("embed_bank1.hex"),
    .INIT2("embed_bank2.hex"), .INIT3("embed_bank3.hex"), .INIT_M("embed_m.hex")
  ) dut (.clk(clk), .rst_n(rst_n), .start(start), .token(token), .x(x), .done(done));

  reg [23:0] tok_mem [0:0];
  reg [1023:0] exp_mem [0:0];
  reg [HIDDEN*RES-1:0] expected;
  integer ci, errors, timeout;

  initial begin
    errors = 0;
    $readmemh("token.hex", tok_mem);
    $readmemh("exp_x.hex", exp_mem);
    token = tok_mem[0];
    expected = exp_mem[0][HIDDEN*RES-1:0];
    repeat (3) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk);
    start = 1'b1;
    @(posedge clk);
    start = 1'b0;
    timeout = 0;
    while (!done && timeout < 1000) begin
      @(posedge clk);
      timeout = timeout + 1;
    end
    #1;
    for (ci = 0; ci < HIDDEN; ci = ci + 1) begin
      if ($signed(x[ci*RES +: RES]) !== $signed(expected[ci*RES +: RES])) begin
        $display("FAIL x lane %0d: %0d != %0d", ci, $signed(x[ci*RES +: RES]), $signed(expected[ci*RES +: RES]));
        errors = errors + 1;
      end
    end
    if (errors == 0) $display("PASS");
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
