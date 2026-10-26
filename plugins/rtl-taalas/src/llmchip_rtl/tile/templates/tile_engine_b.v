// B tokens on one weight stream (optimisation 3): B copies of the engine, one per token, all fed the
// same words and started together, each with its own activation buffer. In this model every copy
// carries its own ROM instance - the replicated MAC array of the plan - while a production layout
// would fan one ROM out to the B arrays; the cycle behaviour is the same either way, because the
// copies march in lockstep.
`default_nettype none

module tile_engine_b #(
  parameter integer MACS = 128,
  parameter integer BITS = 4,
  parameter integer ACC  = 32,
  parameter integer GROUPS = 8,
  parameter integer ROWS = 8,
  parameter integer CHUNKS = 4,
  parameter integer LOG2CHUNKS = 2,
  parameter integer LOG2ROWS = 3,
  parameter integer LOG2ADDR = 5,
  parameter integer LOG2MACS = 7,
  parameter integer B = 2,
  parameter integer LOG2B = 1,
  parameter         INIT0 = "rom_bank0.hex",
  parameter         INIT1 = "rom_bank1.hex",
  parameter         INIT2 = "rom_bank2.hex",
  parameter         INIT3 = "rom_bank3.hex"
) (
  input  wire                          clk,
  input  wire                          rst_n,
  input  wire                          start,
  input  wire                          x_wr_en,
  input  wire [LOG2B-1:0]              x_wr_token,
  input  wire [LOG2CHUNKS-1:0]         x_wr_chunk,
  input  wire [LOG2MACS-1:0]           x_wr_lane,
  input  wire signed [15:0]            x_wr_data,
  output wire [B*ACC-1:0]              y,
  output reg                           y_valid,
  output wire                          done
);

  wire [B-1:0] valid;
  wire [B-1:0] finished;
  wire [B*ACC-1:0] word_sum;
  wire [B-1:0] word_valid;
  wire [B*LOG2ROWS-1:0] word_row;
  wire [B*LOG2CHUNKS-1:0] word_chunk;

  genvar b;
  generate
    for (b = 0; b < B; b = b + 1) begin : token
      wire en_b = x_wr_en && (x_wr_token == b[LOG2B-1:0]);
      tile_engine #(
        .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS),
        .ROWS(ROWS), .CHUNKS(CHUNKS), .LOG2CHUNKS(LOG2CHUNKS),
        .LOG2ROWS(LOG2ROWS), .LOG2ADDR(LOG2ADDR), .LOG2MACS(LOG2MACS),
        .INIT0(INIT0), .INIT1(INIT1), .INIT2(INIT2), .INIT3(INIT3)
      ) engine (
        .clk(clk), .rst_n(rst_n), .start(start),
        .x_wr_en(en_b), .x_wr_chunk(x_wr_chunk), .x_wr_lane(x_wr_lane), .x_wr_data(x_wr_data),
        .y(y[b*ACC +: ACC]), .y_valid(valid[b]), .done(finished[b]),
        .word_sum(word_sum[b*ACC +: ACC]), .word_valid(word_valid[b]),
        .word_row(word_row[b*LOG2ROWS +: LOG2ROWS]), .word_chunk(word_chunk[b*LOG2CHUNKS +: LOG2CHUNKS])
      );
    end
  endgenerate

  assign done = &finished;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      y_valid <= 1'b0;
    end else begin
      y_valid <= &valid;
    end
  end

endmodule
