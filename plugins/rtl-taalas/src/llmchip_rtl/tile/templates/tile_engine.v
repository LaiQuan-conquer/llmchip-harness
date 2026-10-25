// The row-major datapath of one phase of taalas-tile/1 on GF180MCU (drafts/tile-180nm-pipeline.md).
// A ROM word holds MACS consecutive weights of one output row, and the x RAM holds the matching
// MACS activations per column chunk; the array sums each word's products with its group tree and
// accumulates the chunk sums, so every CHUNKS words produce one row's value. Four stages: S0
// address, S1 word and activation register, S2 group sums, S3 group accumulators (both inside the
// MAC array); the row's value is the tree of the eight accumulators, registered with its index.
`default_nettype none

module tile_engine #(
  parameter integer MACS = 128,
  parameter integer BITS = 4,
  parameter integer ACC  = 32,
  parameter integer GROUPS = 8,
  parameter integer ROWS = 8,
  parameter integer CHUNKS = 4,
  parameter integer LOG2CHUNKS = 2,
  parameter integer LOG2ROWS = 3,
  parameter integer LOG2ADDR = 5,
  parameter         INIT = "rom.hex"
) (
  input  wire                          clk,
  input  wire                          rst_n,
  input  wire                          start,
  input  wire                          x_wr_en,
  input  wire [LOG2CHUNKS-1:0]         x_wr_addr,
  input  wire [MACS*16-1:0]            x_wr_data,
  output reg  signed [ACC-1:0]         y,
  output reg                           y_valid,
  output reg                           done
);

  localparam [1:0] IDLE = 2'd0, RUN = 2'd1, DRAIN = 2'd2, DONE = 2'd3;
  localparam integer WORDS = ROWS * CHUNKS;

  reg [1:0]            state;
  reg [LOG2ADDR-1:0]   address;
  reg [LOG2ROWS-1:0]   row;
  reg [LOG2CHUNKS-1:0] chunk;
  reg [3:0]            drain;
  reg [31:0]           left;
  reg [31:0]           chunk_left;
  reg [MACS*16-1:0]    x_mem [0:CHUNKS-1];
  reg [MACS*16-1:0]    x_d;
  reg [MACS*BITS-1:0]  word_d;
  reg                  valid_d;
  reg                  last1;
  reg                  last2;
  reg                  last3;
  reg                  clear_d;
  wire [MACS*BITS-1:0] rom_q;
  wire [MACS*16-1:0]   x_now;

  tile_rom #(.WIDTH(MACS*BITS), .DEPTH(WORDS), .ADDR(LOG2ADDR), .INIT(INIT)) rom (
    .addr(address), .q(rom_q)
  );

  always @(posedge clk) begin
    if (x_wr_en) begin
      x_mem[x_wr_addr] <= x_wr_data;
    end
  end

  assign x_now = x_mem[chunk];

  // S1: the word and the activations of the address issued in the previous cycle.
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      word_d  <= {MACS*BITS{1'b0}};
      x_d     <= {MACS*16{1'b0}};
      valid_d <= 1'b0;
      clear_d <= 1'b0;
      last1   <= 1'b0;
      last2   <= 1'b0;
      last3   <= 1'b0;
    end else begin
      word_d  <= rom_q;
      x_d     <= x_now;
      valid_d <= (state == RUN);
      clear_d <= (state == RUN) && (chunk == {LOG2CHUNKS{1'b0}});
      last1   <= (state == RUN) && (chunk_left == 32'd1);
      last2   <= last1;
      last3   <= last2;
    end
  end

  // S2/S3: the group tree and the accumulators; the drain cycles carry zeros.
  wire [GROUPS*ACC-1:0] groups;
  tile_mac #(.MACS(MACS), .BITS(BITS), .ACC(ACC), .GROUPS(GROUPS)) mac (
    .clk(clk),
    .rst_n(rst_n),
    .en(valid_d || (drain != 4'd0)),
    .clear(clear_d),
    .x(valid_d ? x_d : {MACS*16{1'b0}}),
    .w(word_d),
    .y(groups)
  );

  // The row's value is the tree of the group accumulators, registered one cycle after the row's
  // last chunk has been accumulated (last4 marks it).
  reg signed [ACC-1:0] row_sum;
  integer gi;
  always @(*) begin
    row_sum = {ACC{1'b0}};
    for (gi = 0; gi < GROUPS; gi = gi + 1) begin
      row_sum = row_sum + $signed(groups[gi*ACC +: ACC]);
    end
  end

  // Marcher.
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state   <= IDLE;
      address <= {LOG2ADDR{1'b0}};
      row     <= {LOG2ROWS{1'b0}};
      chunk   <= {LOG2CHUNKS{1'b0}};
      drain      <= 4'd0;
      left       <= 32'd0;
      chunk_left <= 32'd0;
      y       <= {ACC{1'b0}};
      y_valid <= 1'b0;
      done    <= 1'b0;
    end else begin
      y_valid <= 1'b0;
      done    <= 1'b0;
      case (state)
        IDLE: begin
          if (start) begin
            address <= {LOG2ADDR{1'b0}};
            row     <= {LOG2ROWS{1'b0}};
            chunk      <= {LOG2CHUNKS{1'b0}};
            left       <= WORDS[31:0];
            chunk_left <= CHUNKS[31:0];
            state      <= RUN;
          end
        end
        RUN: begin
          if (left == 32'd1) begin
            state <= DRAIN;
            drain <= 4'd6;
          end else begin
            address <= address + 1'b1;
            left    <= left - 32'd1;
            if (chunk_left == 32'd1) begin
              chunk      <= {LOG2CHUNKS{1'b0}};
              chunk_left <= CHUNKS[31:0];
              row        <= row + 1'b1;
            end else begin
              chunk      <= chunk + 1'b1;
              chunk_left <= chunk_left - 32'd1;
            end
          end
        end
        DRAIN: begin
          if (drain == 4'd0) begin
            state <= DONE;
          end else begin
            drain <= drain - 4'd1;
          end
        end
        DONE: begin
          done  <= 1'b1;
          state <= IDLE;
        end
        default: state <= IDLE;
      endcase
      // Row outputs: last3 marks the cycle in which the row's last chunk has been accumulated.
      if (last3) begin
        y       <= row_sum;
        y_valid <= 1'b1;
      end
    end
  end

endmodule
