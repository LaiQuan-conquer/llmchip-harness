// The functional S-1 slice top of taalas-tile/1: a host x write port, the bit-exact linear
// operator of the slice's first matrix, and the row output stream with its index. The generator
// fills the parameters and the mantissa/bias files; tests/test_tile_slice.py compares the rows
// with GraphL2's observation point of the same matrix.
`default_nettype none

module tile_slice_top #(
  parameter integer MACS = /*MACS*/ 8,
  parameter integer BITS = /*BITS*/ 4,
  parameter integer ACC = 32,
  parameter integer GROUPS = 8,
  parameter integer ROWS = /*ROWS*/ 8,
  parameter integer CHUNKS = /*CHUNKS*/ 4,
  parameter integer LOG2CHUNKS = /*LOG2CHUNKS*/ 2,
  parameter integer LOG2ROWS = /*LOG2ROWS*/ 3,
  parameter integer LOG2ADDR = /*LOG2ADDR*/ 5,
  parameter integer LOG2MACS = /*LOG2MACS*/ 3,
  parameter integer LOG2CPG = /*LOG2CPG*/ 1,
  parameter integer NGROUPS = /*NGROUPS*/ 2,
  parameter integer FRAC_IN = /*FRAC_IN*/ 12,
  parameter integer FRAC_OUT = /*FRAC_OUT*/ 15,
  parameter integer EXP = /*EXP*/ 21,
  parameter         INIT0 = "rom_bank0.hex",
  parameter         INIT1 = "rom_bank1.hex",
  parameter         INIT2 = "rom_bank2.hex",
  parameter         INIT3 = "rom_bank3.hex",
  parameter         INIT_M = "slice_m.hex",
  parameter         INIT_B = "slice_b.hex"
) (
  input  wire                          clk,
  input  wire                          rst_n,
  input  wire                          start,
  input  wire                          x_wr_en,
  input  wire [LOG2CHUNKS-1:0]         x_wr_chunk,
  input  wire [LOG2MACS-1:0]           x_wr_lane,
  input  wire signed [15:0]            x_wr_data,
  output wire signed [15:0]            y,
  output wire                          y_valid,
  output wire [LOG2ROWS-1:0]           y_index,
  output wire                          done,
  output wire signed [ACC-1:0]         raw_y,
  output wire                          raw_y_valid,
  output wire signed [63:0]            total_y,
  output wire                          total_valid
);

  tile_linear #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS), .ROWS(ROWS), .CHUNKS(CHUNKS),
    .LOG2CHUNKS(LOG2CHUNKS), .LOG2ROWS(LOG2ROWS), .LOG2ADDR(LOG2ADDR), .LOG2MACS(LOG2MACS),
    .LOG2CPG(LOG2CPG), .NGROUPS(NGROUPS), .FRAC_IN(FRAC_IN), .FRAC_OUT(FRAC_OUT), .EXP(EXP),
    .INIT0(INIT0), .INIT1(INIT1), .INIT2(INIT2), .INIT3(INIT3), .INIT_M(INIT_M), .INIT_B(INIT_B)
  ) linear (
    .clk(clk), .rst_n(rst_n), .start(start),
    .x_wr_en(x_wr_en), .x_wr_chunk(x_wr_chunk), .x_wr_lane(x_wr_lane), .x_wr_data(x_wr_data),
    .y(y), .y_valid(y_valid), .y_index(y_index), .done(done), .raw_y(raw_y), .raw_y_valid(raw_y_valid),
    .total_y(total_y), .total_valid(total_valid)
  );

endmodule
