// The parallel MAC array of taalas-tile/1 with a group tree (drafts/tile-180nm-pipeline.md): one
// ROM word carries MACS consecutive weights of one output row, so the array computes MACS products
// against the row's MACS activations and sums them with GROUPS partial adders (each over MACS/GROUPS
// products), S2 registering the group sums and S3 accumulating them. The group accumulators are
// combined into the row's value once per row (by the engine); `clear` marks the row's first chunk.
`default_nettype none

module tile_mac #(
  parameter integer MACS = 128,
  parameter integer BITS = 4,
  parameter integer ACC  = 32,
  parameter integer GROUPS = 8
) (
  input  wire                         clk,
  input  wire                         rst_n,
  input  wire                         en,
  input  wire                         clear,
  input  wire signed [MACS*16-1:0]    x,
  input  wire [MACS*BITS-1:0]         w,
  output wire [GROUPS*ACC-1:0]        y
);

  localparam integer GROUP = MACS / GROUPS;

  genvar k;
  genvar g;

  wire signed [ACC-1:0] prod [0:MACS-1];

  generate
    for (k = 0; k < MACS; k = k + 1) begin : lane
      wire signed [BITS-1:0] level = w[k*BITS +: BITS];
      wire signed [ACC-1:0]  xa    = {{(ACC-16){x[k*16+15]}}, x[k*16 +: 16]};
      wire signed [ACC-1:0]  wa    = {{(ACC-BITS){level[BITS-1]}}, level};
      assign prod[k] = xa * wa;
    end
  endgenerate

  reg signed [ACC-1:0] gsum [0:GROUPS-1];
  reg signed [ACC-1:0] gsum_d [0:GROUPS-1];
  reg signed [ACC-1:0] gacc [0:GROUPS-1];
  reg                  clear_d;

  integer gi;
  integer pi;
  integer gs;

  // The group sum is a tree, not a chain: the products are combined eight at a time and the eight
  // partial sums by a three-level tree (GroupSize/8 + 3 adders of depth instead of GroupSize).
  // Addition modulo 2^ACC is associative, so the value is bit identical.
  reg signed [ACC-1:0] sub [0:7];
  integer si;
  always @(*) begin
    for (gi = 0; gi < GROUPS; gi = gi + 1) begin
      if (GROUP >= 8) begin
        for (si = 0; si < 8; si = si + 1) begin
          sub[si] = {ACC{1'b0}};
          for (pi = si * (GROUP / 8); pi < (si + 1) * (GROUP / 8); pi = pi + 1) begin
            sub[si] = sub[si] + prod[gi*GROUP + pi];
          end
        end
        gsum[gi] = ((sub[0] + sub[1]) + (sub[2] + sub[3])) + ((sub[4] + sub[5]) + (sub[6] + sub[7]));
      end else begin
        gsum[gi] = {ACC{1'b0}};
        for (pi = 0; pi < GROUP; pi = pi + 1) begin
          gsum[gi] = gsum[gi] + prod[gi*GROUP + pi];
        end
      end
    end
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      clear_d <= 1'b0;
      for (gs = 0; gs < GROUPS; gs = gs + 1) begin
        gsum_d[gs] <= {ACC{1'b0}};
        gacc[gs]   <= {ACC{1'b0}};
      end
    end else if (en) begin
      clear_d <= clear;
      for (gs = 0; gs < GROUPS; gs = gs + 1) begin
        gsum_d[gs] <= gsum[gs];
        gacc[gs]   <= clear_d ? gsum_d[gs] : gacc[gs] + gsum_d[gs];
      end
    end
  end

  generate
    for (g = 0; g < GROUPS; g = g + 1) begin : group_out
      assign y[g*ACC +: ACC] = gacc[g];
    end
  endgenerate

endmodule
