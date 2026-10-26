`timescale 1ns/1ps
module tile_residual_tb;
  reg signed [511:0] a, b;
  wire signed [511:0] y;
  integer errors;
  integer lane, i;
  reg signed [15:0] ta, tb, expct;
  reg signed [16:0] s;

  tile_residual dut (.a(a), .b(b), .y(y));

  task check(input integer ln, input signed [15:0] va, input signed [15:0] vb);
    begin
      a = 512'd0; b = 512'd0;
      a[ln*16 +: 16] = va; b[ln*16 +: 16] = vb; #1;
      s = va + vb;
      expct = (s > 17'sd32767) ? 16'sd32767 : (s < -17'sd32768) ? -16'sd32768 : s[15:0];
      if ($signed(y[ln*16 +: 16]) !== expct) begin
        $display("ERROR lane %0d: %0d + %0d -> %0d expected %0d", ln, va, vb, $signed(y[ln*16 +: 16]), expct);
        errors = errors + 1;
      end
    end
  endtask

  initial begin
    errors = 0;
    for (i = 0; i < 65536; i = i + 1) begin
      check(0, i - 32768, 0);
      check(0, i - 32768, 1);
      check(0, i - 32768, -1);
    end
    for (lane = 0; lane < 32; lane = lane + 1) begin
      check(lane, 0, 0); check(lane, 1, 2); check(lane, -1, -2);
      check(lane, 32767, 0); check(lane, 32767, 1); check(lane, 32766, 1);
      check(lane, -32768, 0); check(lane, -32768, -1); check(lane, -32767, -1);
      check(lane, 20000, 20000); check(lane, -20000, -20000);
      check(lane, 12345, -2345); check(lane, -12345, 2345);
      check(lane, 16384, 16383); check(lane, -16384, -16384);
      for (i = 0; i < 16; i = i + 1) begin
        check(lane, (1 << i), 0);
        check(lane, -(1 << i), 0);
      end
      check(lane, 24575, 0); check(lane, 24576, 0);
      check(lane, 30000, 0); check(lane, 32000, 0);
      check(lane, -24575, 0); check(lane, -24576, 0);
      check(lane, -30000, 0); check(lane, -32000, 0);
      for (i = 0; i < 512; i = i + 1) begin
        ta = $random;
        tb = $random;
        check(lane, ta, tb);
      end
    end
    if (errors == 0) $display("PASS"); else $display("FAIL %0d", errors);
    $finish;
  end
endmodule
