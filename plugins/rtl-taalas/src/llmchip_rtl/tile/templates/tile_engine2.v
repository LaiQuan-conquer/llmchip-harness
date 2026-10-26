// Two engines in lockstep for a phase that streams two matrices (optimisation 2: gate_proj and
// up_proj share one activation vector, so they run as two streams - the "2 x MAC" option of the
// design draft). The host writes the activation buffer once; both engines keep a copy and march the
// same addresses, so the pair finishes one phase in the time one engine took for one matrix.
`default_nettype none

module tile_engine2 #(
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
  parameter         INIT_A0 = "rom_a_bank0.hex",
  parameter         INIT_A1 = "rom_a_bank1.hex",
  parameter         INIT_A2 = "rom_a_bank2.hex",
  parameter         INIT_A3 = "rom_a_bank3.hex",
  parameter         INIT_B0 = "rom_b_bank0.hex",
  parameter         INIT_B1 = "rom_b_bank1.hex",
  parameter         INIT_B2 = "rom_b_bank2.hex",
  parameter         INIT_B3 = "rom_b_bank3.hex"
) (
  input  wire                          clk,
  input  wire                          rst_n,
  input  wire                          start,
  input  wire                          x_wr_en,
  input  wire [LOG2CHUNKS-1:0]         x_wr_chunk,
  input  wire [LOG2MACS-1:0]           x_wr_lane,
  input  wire signed [15:0]            x_wr_data,
  output wire signed [ACC-1:0]         y_a,
  output wire signed [ACC-1:0]         y_b,
  output reg                           y_valid,
  output wire                          done
);

  wire done_a;
  wire done_b;
  wire signed [ACC-1:0] y_a_int;
  wire signed [ACC-1:0] y_b_int;
  wire valid_a;
  wire valid_b;
  wire signed [ACC-1:0] sum_a;
  wire signed [ACC-1:0] sum_b;
  wire wvalid_a;
  wire wvalid_b;
  wire [LOG2ROWS-1:0] wrow_a;
  wire [LOG2ROWS-1:0] wrow_b;
  wire [LOG2CHUNKS-1:0] wchunk_a;
  wire [LOG2CHUNKS-1:0] wchunk_b;

  tile_engine #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS),
    .ROWS(ROWS), .CHUNKS(CHUNKS), .LOG2CHUNKS(LOG2CHUNKS),
    .LOG2ROWS(LOG2ROWS), .LOG2ADDR(LOG2ADDR), .LOG2MACS(LOG2MACS),
    .INIT0(INIT_A0), .INIT1(INIT_A1), .INIT2(INIT_A2), .INIT3(INIT_A3)
  ) a (
    .clk(clk), .rst_n(rst_n), .start(start),
    .x_wr_en(x_wr_en), .x_wr_chunk(x_wr_chunk), .x_wr_lane(x_wr_lane), .x_wr_data(x_wr_data),
    .y(y_a_int), .y_valid(valid_a), .done(done_a),
    .word_sum(sum_a), .word_valid(wvalid_a), .word_row(wrow_a), .word_chunk(wchunk_a)
  );

  tile_engine #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS),
    .ROWS(ROWS), .CHUNKS(CHUNKS), .LOG2CHUNKS(LOG2CHUNKS),
    .LOG2ROWS(LOG2ROWS), .LOG2ADDR(LOG2ADDR), .LOG2MACS(LOG2MACS),
    .INIT0(INIT_B0), .INIT1(INIT_B1), .INIT2(INIT_B2), .INIT3(INIT_B3)
  ) b (
    .clk(clk), .rst_n(rst_n), .start(start),
    .x_wr_en(x_wr_en), .x_wr_chunk(x_wr_chunk), .x_wr_lane(x_wr_lane), .x_wr_data(x_wr_data),
    .y(y_b_int), .y_valid(valid_b), .done(done_b),
    .word_sum(sum_b), .word_valid(wvalid_b), .word_row(wrow_b), .word_chunk(wchunk_b)
  );

  assign y_a = y_a_int;
  assign y_b = y_b_int;
  assign done = done_a & done_b;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      y_valid <= 1'b0;
    end else begin
      y_valid <= valid_a & valid_b;
    end
  end

endmodule
