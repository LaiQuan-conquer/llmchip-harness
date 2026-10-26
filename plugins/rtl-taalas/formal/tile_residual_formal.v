// Formal wrapper for the residual add: the output must equal the saturating sum for every input.
`default_nettype none

module tile_residual_formal;
  (* anyseq *) reg signed [15:0] a;
  (* anyseq *) reg signed [15:0] b;
  wire signed [15:0] y;
  wire signed [16:0] sum = a + b;
  wire signed [15:0] ref = (sum > 17'sd32767) ? 16'sd32767 :
                           (sum < -17'sd32768) ? -16'sd32768 : sum[15:0];

  tile_residual #(.DIM(1), .WIDTH(16)) dut (.a(a), .b(b), .y(y));

  always @* assert (y == ref);
endmodule
