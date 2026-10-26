// The Q-full top with one weight-bank set per decoder layer (drafts/tile-180nm-pipeline.md §12.2).
// The generator fills the engine block with one `tile_engine` instance per layer, each loading
// `layer<n>_bank0..3.hex`; the chain logic is the same as `tile_qfull_top`:
// PIPELINED = 0 releases layer i+1 on layer i's `done`, PIPELINED = 1 on its first `y_valid`.
// `layer` names the layer most recently started and `y` is that layer's row output.
`default_nettype none

module tile_qfull_banked #(
  parameter integer LAYERS = /*LAYERS*/ 2,
  parameter integer MACS = /*MACS*/ 8,
  parameter integer BITS = /*BITS*/ 4,
  parameter integer ACC = 32,
  parameter integer GROUPS = 8,
  parameter integer ROWS = 3,
  parameter integer CHUNKS = 2,
  parameter integer LOG2CHUNKS = 1,
  parameter integer LOG2ROWS = 2,
  parameter integer LOG2ADDR = /*LOG2ADDR*/ 10,
  parameter integer LOG2MACS = 3,
  parameter integer PIPELINED = 0
) (
  input  wire                          clk,
  input  wire                          rst_n,
  input  wire                          start,
  input  wire                          x_wr_en,
  input  wire [LOG2CHUNKS-1:0]         x_wr_chunk,
  input  wire [LOG2MACS-1:0]           x_wr_lane,
  input  wire signed [15:0]            x_wr_data,
  output reg  [7:0]                    layer,
  output wire signed [ACC-1:0]         y,
  output wire                          y_valid,
  output wire                          done,
  output wire signed [ACC-1:0]         debug_sum,
  output wire                          debug_valid,
  output wire [LOG2ROWS-1:0]           debug_row,
  output wire [LOG2CHUNKS-1:0]         debug_chunk
);

  wire [LAYERS-1:0] start_w;
  wire [LAYERS-1:0] done_w;
  wire [LAYERS-1:0] y_valid_w;
  reg  [LAYERS-2:0] first_w;
  wire [LAYERS*ACC-1:0] y_w;
  wire [LAYERS*ACC-1:0] word_sum_w;
  wire [LAYERS-1:0] word_valid_w;
  wire [LAYERS*LOG2ROWS-1:0] word_row_w;
  wire [LAYERS*LOG2CHUNKS-1:0] word_chunk_w;

  reg [LAYERS-1:0] seen;
  integer li;

/*ENGINES*/

  assign start_w[0] = start;
  genvar gi;
  generate
    for (gi = 1; gi < LAYERS; gi = gi + 1) begin : gen_start
      assign start_w[gi] = (PIPELINED != 0) ? first_w[gi-1] : done_w[gi-1];
    end
  endgenerate

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      seen    <= {LAYERS{1'b0}};
      first_w <= 0;
    end else begin
      first_w <= 0;
      if (start) begin
        seen <= {LAYERS{1'b0}};
      end else begin
        for (li = 0; li < LAYERS-1; li = li + 1) begin
          if (y_valid_w[li] && !seen[li]) begin
            first_w[li] <= 1'b1;
            seen[li]    <= 1'b1;
          end
        end
      end
    end
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      layer <= 8'd0;
    end else if (start) begin
      layer <= 8'd0;
    end else begin
      for (li = 1; li < LAYERS; li = li + 1) begin
        if (start_w[li]) begin
          layer <= li[7:0];
        end
      end
    end
  end

  reg signed [ACC-1:0] y_mux;
  reg                  y_valid_mux;
  always @(*) begin
    y_mux       = {ACC{1'b0}};
    y_valid_mux = 1'b0;
    for (li = 0; li < LAYERS; li = li + 1) begin
      if (layer == li[7:0]) begin
        y_mux       = y_w[li*ACC +: ACC];
        y_valid_mux = y_valid_w[li];
      end
    end
  end

  assign y       = y_mux;
  assign y_valid = y_valid_mux;
  assign done    = done_w[LAYERS-1];

  reg signed [ACC-1:0] debug_sum_mux;
  reg                  debug_valid_mux;
  reg [LOG2ROWS-1:0]   debug_row_mux;
  reg [LOG2CHUNKS-1:0] debug_chunk_mux;
  always @(*) begin
    debug_sum_mux   = {ACC{1'b0}};
    debug_valid_mux = 1'b0;
    debug_row_mux   = {LOG2ROWS{1'b0}};
    debug_chunk_mux = {LOG2CHUNKS{1'b0}};
    for (li = 0; li < LAYERS; li = li + 1) begin
      if (layer == li[7:0]) begin
        debug_sum_mux   = word_sum_w[li*ACC +: ACC];
        debug_valid_mux = word_valid_w[li];
        debug_row_mux   = word_row_w[li*LOG2ROWS +: LOG2ROWS];
        debug_chunk_mux = word_chunk_w[li*LOG2CHUNKS +: LOG2CHUNKS];
      end
    end
  end
  assign debug_sum   = debug_sum_mux;
  assign debug_valid = debug_valid_mux;
  assign debug_row   = debug_row_mux;
  assign debug_chunk = debug_chunk_mux;

endmodule
