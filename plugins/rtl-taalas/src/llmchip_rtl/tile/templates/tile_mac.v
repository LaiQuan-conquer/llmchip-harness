// The parallel MAC array of taalas-tile/1, pipelined for GF180MCU (drafts/tile-180nm-pipeline.md):
// S2 multiplies every lane's `BITS`-wide level by the shared 16-bit activation and registers the
// products, S3 adds them into one 32-bit accumulator per lane. One word per cycle; the ROM read is
// the stage before, so a word reaches the accumulator three cycles after its address.
// `phase_start` marks the phase's first word at this port; the first product then overwrites the
// accumulator instead of adding to it.
`default_nettype none

module tile_mac #(
  parameter integer MACS = 128,
  parameter integer BITS = 4,
  parameter integer ACC  = 32
) (
  input  wire                         clk,
  input  wire                         rst_n,
  input  wire                         en,
  input  wire                         phase_start,
  input  wire signed [15:0]           x,
  input  wire [MACS*BITS-1:0]         w,
  output wire [MACS*ACC-1:0]          y
);

  genvar k;

  reg [MACS*ACC-1:0] product;
  reg [MACS*ACC-1:0] acc;
  reg                first_d;

  generate
    for (k = 0; k < MACS; k = k + 1) begin : lane
      wire signed [BITS-1:0] level = w[k*BITS +: BITS];
      wire signed [ACC-1:0]  xa    = {{(ACC-16){x[15]}}, x};
      wire signed [ACC-1:0]  wa    = {{(ACC-BITS){level[BITS-1]}}, level};
      wire signed [ACC-1:0]  prod  = xa * wa;
      assign y[k*ACC +: ACC] = acc[k*ACC +: ACC];
      always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
          product[k*ACC +: ACC] <= {ACC{1'b0}};
          acc[k*ACC +: ACC]     <= {ACC{1'b0}};
        end else if (en) begin
          product[k*ACC +: ACC] <= prod;
          acc[k*ACC +: ACC]     <= first_d ? product[k*ACC +: ACC] : acc[k*ACC +: ACC] + product[k*ACC +: ACC];
        end
      end
    end
  endgenerate

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      first_d <= 1'b0;
    end else if (en) begin
      first_d <= phase_start;
    end
  end

endmodule
