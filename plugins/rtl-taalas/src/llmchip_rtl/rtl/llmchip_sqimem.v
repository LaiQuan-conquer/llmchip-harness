// External data memory port, quad SPI (SQI): a master for a 23LC1024-style serial
// SRAM in SQI mode (4 data lines SIO[3:0], SCK = clk / 2). One request moves one
// 32-bit word, most significant nibble first, at byte address 4 * addr:
//   READ  0x03: command (2 SCK), 24-bit address (6 SCK), 2 dummy SCK, data (8 SCK)
//   WRITE 0x02: command (2 SCK), 24-bit address (6 SCK), data (8 SCK)
// A read that follows a read of the previous word continues the open sequential
// transfer (8 SCK). After reset the port puts the SRAM into SQI mode: RSTIO (0xFF
// on all four lines, 2 SCK; ignored by an SRAM still in SPI mode), then EQIO (0x38
// on SIO0, 8 SCK, SPI mode). The timing of every access mirrors llmchip_spimem.v,
// with frames of 18 / 16 / 8 SCK instead of 64 / 64 / 32 (perf.py, SqiPort).
// The requester holds addr and wdata stable until `ack`; `ack` and `rdata` are
// valid in the same cycle.
`timescale 1ns/1ps
`include "llmchip_params.vh"
/* verilator lint_off WIDTHEXPAND */
module llmchip_sqimem (
  input  wire           clk,
  input  wire           rst_n,
  input  wire           hold,    // keep a sequential read open (the core is busy)
  input  wire           req,
  input  wire           we,
  input  wire [`AW-1:0] addr,
  input  wire [31:0]    wdata,
  output wire           ack,
  output wire [31:0]    rdata,
  output reg            sclk,
  output reg            cs_n,
  output reg  [3:0]     sio_o,
  output reg  [3:0]     sio_oe,
  input  wire [3:0]     sio_i
);
  localparam [2:0] M_IDLE = 3'd0, M_SHIFT = 3'd1, M_OPEN = 3'd2, M_END = 3'd3, M_INIT = 3'd4;
  reg [2:0] st;
  reg [27:0] rx;
  reg [4:0] n;    // index of the nibble on the bus: 17 / 15 (first) .. 0 (last data nibble)
  reg ph, wr;
  reg [`AW-1:0] last;
  reg [4:0] ini;  // reset sequence: RSTIO nibbles, gap, EQIO bits, gap

  // Outgoing frame: command, 24-bit byte address, data (16 nibbles).
  wire [63:0] frame = {we ? 8'h02 : 8'h03, 22'd0 | addr, 2'b00, wdata};
  // Nibble n of a frame: reads have 18 (two dummy nibbles before the data), writes 16.
  wire [4:0] k = (!wr && n >= 5'd10) ? n - 5'd2 : n;
  wire [3:0] nib = frame[{k[3:0], 2'b00} +: 4];
  wire drive = wr || n >= 5'd10;  // the master drives command, address and write data
  assign ack = (st == M_SHIFT) && ph && (n == 5'd0);
  assign rdata = {rx, sio_i};

  // EQIO (0x38) bit by bit on SIO0, SPI mode: ini 5..12 carry bits 7..0.
  wire [7:0] eqio = 8'h38;
  wire [4:0] ebit = 5'd12 - ini;

  always @(posedge clk) begin
    if (!rst_n) begin
      st <= M_INIT;
      sclk <= 1'b0;
      cs_n <= 1'b1;
      sio_o <= 4'd0;
      sio_oe <= 4'd0;
      rx <= 0;
      n <= 5'd0;
      ph <= 1'b0;
      wr <= 1'b0;
      last <= 0;
      ini <= 5'd0;
    end else begin
      case (st)
        // ini 0: CS low; 1..2: RSTIO nibbles (1111); 3: CS high; 4: CS low;
        // 5..12: EQIO bits; 13: CS high, then idle. Each SCK cycle is two clocks (ph).
        M_INIT: begin
          if (ini == 5'd0 || ini == 5'd4) begin
            cs_n <= 1'b0;
            ini <= ini + 5'd1;
          end else if (ini == 5'd3 || ini == 5'd13) begin
            sclk <= 1'b0;
            sio_oe <= 4'd0;
            cs_n <= 1'b1;
            if (ini == 5'd13) st <= M_IDLE;
            ini <= ini + 5'd1;
          end else if (!ph) begin
            sclk <= 1'b0;
            if (ini < 5'd3) begin
              sio_o <= 4'hF;
              sio_oe <= 4'hF;
            end else begin
              sio_o <= {3'b000, eqio[ebit[2:0]]};
              sio_oe <= 4'b0001;
            end
            ph <= 1'b1;
          end else begin
            sclk <= 1'b1;
            ph <= 1'b0;
            ini <= ini + 5'd1;
          end
        end
        M_IDLE: if (req) begin
          cs_n <= 1'b0;
          wr <= we;
          last <= addr;
          n <= we ? 5'd15 : 5'd17;
          ph <= 1'b0;
          st <= M_SHIFT;
        end
        M_SHIFT: begin
          if (!ph) begin
            sclk <= 1'b0;
            sio_o <= nib;
            sio_oe <= drive ? 4'hF : 4'h0;
            ph <= 1'b1;
          end else begin
            sclk <= 1'b1;
            rx <= {rx[23:0], sio_i};
            ph <= 1'b0;
            if (n == 5'd0) st <= wr ? M_END : M_OPEN;
            else n <= n - 5'd1;
          end
        end
        // A read leaves SCK high and CS low: the next word of the sequential
        // transfer follows with the next falling edge.
        M_OPEN: begin
          if (req && !we && addr == last + 1'b1) begin
            last <= addr;
            n <= 5'd7;
            ph <= 1'b0;
            st <= M_SHIFT;
          end else if (req || !hold) st <= M_END;
        end
        // SCK low first (releasing the bus), then CS high one cycle later.
        M_END: begin
          if (sclk) begin
            sclk <= 1'b0;
            sio_oe <= 4'd0;
          end else begin
            cs_n <= 1'b1;
            st <= M_IDLE;
          end
        end
        default: st <= M_IDLE;
      endcase
    end
  end
endmodule
/* verilator lint_on WIDTHEXPAND */
