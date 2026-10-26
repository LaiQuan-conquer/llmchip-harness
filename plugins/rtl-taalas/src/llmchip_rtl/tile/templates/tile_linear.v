// The bit-exact linear operator of taalas-tile/1: `tile_engine`'s word products, accumulated per
// quantization group and scaled by the group mantissas, then rounded, biased and narrowed exactly
// as `GraphL2.linear` does. The engine's S2b group sum travels with its chunk (`word_sum`,
// `word_chunk`); this stage adds it to the quantization group the chunk belongs to, and at the
// last chunk of a row combines the groups with their mantissas in one combinational tree.
// Arithmetic: y = narrow(rshift(sum_g m[o,g] * sum_{i in g} x_i q[o,i], FRAC_IN + EXP - FRAC_OUT)
//                        + b[o]), with rshift's half-up rounding.
`default_nettype none

module tile_linear #(
  parameter integer MACS = 8,
  parameter integer BITS = 4,
  parameter integer ACC = 32,
  parameter integer GROUPS = 8,
  parameter integer ROWS = 8,
  parameter integer CHUNKS = 4,
  parameter integer LOG2CHUNKS = 2,
  parameter integer LOG2ROWS = 3,
  parameter integer LOG2ADDR = 5,
  parameter integer LOG2MACS = 3,
  parameter integer LOG2CPG = 1,
  parameter integer NGROUPS = 2,
  parameter integer FRAC_IN = 12,
  parameter integer FRAC_OUT = 15,
  parameter integer EXP = 21,
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
  output reg  signed [15:0]            y,
  output reg                           y_valid,
  output reg  [LOG2ROWS-1:0]           y_index,
  output wire                          done,
  output wire signed [ACC-1:0]         raw_y,
  output wire                          raw_y_valid
);

  localparam integer SHIFT = FRAC_IN + EXP - FRAC_OUT;
  localparam integer LASTCHUNK = CHUNKS - 1;

  wire signed [ACC-1:0] word_sum;
  wire                  word_valid;
  wire [LOG2ROWS-1:0]   word_row;
  wire [LOG2CHUNKS-1:0] word_chunk;

  tile_engine #(
    .MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS), .ROWS(ROWS), .CHUNKS(CHUNKS),
    .LOG2CHUNKS(LOG2CHUNKS), .LOG2ROWS(LOG2ROWS), .LOG2ADDR(LOG2ADDR), .LOG2MACS(LOG2MACS),
    .INIT0(INIT0), .INIT1(INIT1), .INIT2(INIT2), .INIT3(INIT3)
  ) engine (
    .clk(clk), .rst_n(rst_n), .start(start),
    .x_wr_en(x_wr_en), .x_wr_chunk(x_wr_chunk), .x_wr_lane(x_wr_lane), .x_wr_data(x_wr_data),
    .y(raw_y), .y_valid(raw_y_valid), .done(done),
    .word_sum(word_sum), .word_valid(word_valid), .word_row(word_row), .word_chunk(word_chunk)
  );

  reg [14:0] mant [0:ROWS*NGROUPS-1];
  reg signed [15:0] bias [0:ROWS-1];

  initial begin
    $readmemh(INIT_M, mant);
    $readmemh(INIT_B, bias);
  end

  wire [31:0] group_index = {{(32-LOG2CHUNKS){1'b0}}, word_chunk} >> LOG2CPG;

  reg signed [ACC-1:0] raw [0:NGROUPS-1];
  reg signed [ACC-1:0] raw_next [0:NGROUPS-1];
  reg signed [63:0] total_next;
  reg signed [63:0] rounded_next;
  reg signed [63:0] shifted_next;
  wire signed [63:0] bias_ext = {{48{bias[word_row][15]}}, bias[word_row]};
  wire signed [63:0] biased_next = shifted_next + bias_ext;
  reg signed [15:0] y_next;
  integer gi;

  always @(*) begin
    for (gi = 0; gi < NGROUPS; gi = gi + 1) begin
      raw_next[gi] = raw[gi];
      if (word_chunk == {LOG2CHUNKS{1'b0}}) begin
        raw_next[gi] = (gi == group_index) ? word_sum : {ACC{1'b0}};
      end else if (gi == group_index) begin
        raw_next[gi] = raw[gi] + word_sum;
      end
    end
    total_next = 64'sd0;
    for (gi = 0; gi < NGROUPS; gi = gi + 1) begin
      total_next = total_next + $signed({49'd0, mant[word_row*NGROUPS + gi]}) *
                                $signed({{(64-ACC){raw_next[gi][ACC-1]}}, raw_next[gi]});
    end
    rounded_next = total_next + (64'sd1 <<< (SHIFT - 1));
    shifted_next = rounded_next >>> SHIFT;
    y_next = (biased_next > 64'sd32767) ? 16'sh7fff :
             (biased_next < -64'sd32768) ? 16'sh8000 : biased_next[15:0];
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (gi = 0; gi < NGROUPS; gi = gi + 1) begin
        raw[gi] <= {ACC{1'b0}};
      end
      y       <= 16'sd0;
      y_valid <= 1'b0;
      y_index <= {LOG2ROWS{1'b0}};
    end else begin
      y_valid <= 1'b0;
      if (word_valid) begin
        for (gi = 0; gi < NGROUPS; gi = gi + 1) begin
          raw[gi] <= raw_next[gi];
        end
        if (word_chunk == LASTCHUNK[LOG2CHUNKS-1:0]) begin
          y       <= y_next;
          y_index <= word_row;
          y_valid <= 1'b1;
        end
      end
    end
  end

endmodule
