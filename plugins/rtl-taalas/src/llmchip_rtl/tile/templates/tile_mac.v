// The parallel MAC array of taalas-tile/1: MACS signed multipliers, one per output row chunk,
// each multiplying the word's `BITS`-wide level by the same 16-bit signed activation.
module tile_mac #(
  parameter integer MACS = 128,
  parameter integer BITS = 4,
  parameter integer ACC  = 32
) (
  input  wire signed [15:0]           x,
  input  wire [MACS*BITS-1:0]         w,
  output wire [MACS*ACC-1:0]          y
);

  genvar k;

  generate
    for (k = 0; k < MACS; k = k + 1) begin : lane
      wire signed [BITS-1:0] level;
      wire signed [ACC-1:0]  xa;
      wire signed [ACC-1:0]  wa;
      wire signed [ACC-1:0]  product;

      assign level   = w[k*BITS +: BITS];
      assign xa      = {{(ACC-16){x[15]}}, x};
      assign wa      = {{(ACC-BITS){level[BITS-1]}}, level};
      assign product = xa * wa;
      assign y[k*ACC +: ACC] = product;
    end
  endgenerate

endmodule
