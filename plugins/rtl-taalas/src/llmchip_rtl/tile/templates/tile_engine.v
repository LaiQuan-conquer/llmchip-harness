// The datapath of one phase of taalas-tile/1 on GF180MCU (drafts/tile-180nm-pipeline.md): the
// marcher walks the ROM's words, each word holding MACS levels of one input column; the activation
// of that column comes from the engine's x RAM. Stages: S0 address, S1 word and activation
// (registered here), S2 multiply, S3 accumulate (both inside the MAC array). One word per cycle;
// the last word's accumulator value is ready two cycles after the last address.
`default_nettype none

module tile_engine #(
  parameter integer MACS = 128,
  parameter integer BITS = 4,
  parameter integer ACC  = 32,
  parameter integer WORDS = 8,
  parameter integer LOG2WORDS = 3,
  parameter integer LOG2ADDR = 4,
  parameter         INIT = "rom.hex"
) (
  input  wire                          clk,
  input  wire                          rst_n,
  input  wire                          start,
  input  wire                          x_wr_en,
  input  wire [LOG2WORDS-1:0]          x_wr_addr,
  input  wire signed [15:0]            x_wr_data,
  output wire [MACS*ACC-1:0]           y,
  output reg                           done
);

  localparam [1:0] IDLE = 2'd0, RUN = 2'd1, DRAIN = 2'd2, DONE = 2'd3;

  reg [1:0]            state;
  reg [LOG2ADDR-1:0]   address;
  reg                  valid_d;
  reg                  phase_start_d;
  reg [1:0]            drain;
  reg [31:0]           left;
  reg signed [15:0]    x_mem [0:WORDS-1];
  reg signed [15:0]    x_d;
  reg [MACS*BITS-1:0]  word_d;
  wire [MACS*BITS-1:0] rom_q;
  wire signed [15:0]   x_now;

  tile_rom #(.WIDTH(MACS*BITS), .DEPTH(WORDS), .ADDR(LOG2ADDR), .INIT(INIT)) rom (
    .addr(address), .q(rom_q)
  );

  always @(posedge clk) begin
    if (x_wr_en) begin
      x_mem[x_wr_addr] <= x_wr_data;
    end
  end

  assign x_now = x_mem[address[LOG2WORDS-1:0]];

  // S1: the word and the activation of the address issued in the previous cycle, and the flag
  // that says they are a real word (the drain cycles carry zeros).
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      word_d        <= {MACS*BITS{1'b0}};
      x_d           <= 16'sd0;
      valid_d       <= 1'b0;
      phase_start_d <= 1'b0;
    end else begin
      phase_start_d <= 1'b0;
      word_d        <= rom_q;
      x_d           <= x_now;
      valid_d       <= (state == RUN);
      if (state == RUN && address == {LOG2ADDR{1'b0}}) begin
        phase_start_d <= 1'b1;
      end
    end
  end

  // S2/S3: multiply and accumulate. The array runs while a word is valid or the pipeline still
  // holds words; the drain cycles multiply by zero.
  wire [MACS*ACC-1:0] acc;
  assign y = acc;

  tile_mac #(.MACS(MACS), .BITS(BITS), .ACC(ACC)) mac (
    .clk(clk),
    .rst_n(rst_n),
    .en(valid_d || (drain != 2'd0)),
    .phase_start(phase_start_d),
    .x(valid_d ? x_d : 16'sd0),
    .w(word_d),
    .y(acc)
  );

  // Marcher: S0 issues the addresses, then keeps the array running until the words drain.
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state   <= IDLE;
      address <= {LOG2ADDR{1'b0}};
      drain   <= 2'd0;
      left    <= 32'd0;
      done    <= 1'b0;
    end else begin
      done <= 1'b0;
      case (state)
        IDLE: begin
          if (start) begin
            address <= {LOG2ADDR{1'b0}};
            left    <= WORDS[31:0];
            state   <= RUN;
          end
        end
        RUN: begin
          if (left == 32'd1) begin
            state <= DRAIN;
            drain <= 2'd3;
          end else begin
            address <= address + 1'b1;
            left    <= left - 32'd1;
          end
        end
        DRAIN: begin
          if (drain == 2'd0) begin
            state <= DONE;
          end else begin
            drain <= drain - 2'd1;
          end
        end
        DONE: begin
          done  <= 1'b1;
          state <= IDLE;
        end
        default: state <= IDLE;
      endcase
    end
  end

endmodule
