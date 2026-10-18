// External data memory port: an SPI master (mode 0, SCK = clk / 2) for a
// 23LC1024-style serial SRAM (READ 0x03 / WRITE 0x02, 24-bit byte address,
// sequential mode). One request moves one 32-bit word, most significant byte
// first, at byte address 4 * addr. A read that follows a read of the previous
// word continues the open sequential transfer instead of starting a new one.
// The requester holds addr and wdata stable until `ack`; `ack` and `rdata`
// are valid in the same cycle.
`timescale 1ns/1ps
`include "llmchip_params.vh"
/* verilator lint_off WIDTHEXPAND */
module llmchip_spimem (
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
  output reg            mosi,
  input  wire           miso
);
  localparam [1:0] M_IDLE = 2'd0, M_SHIFT = 2'd1, M_OPEN = 2'd2, M_END = 2'd3;
  reg [1:0] st;
  reg [30:0] rx;
  reg [5:0] n;   // index of the bit on the wire: 63 (first) .. 0 (last)
  reg ph, wr;
  reg [`AW-1:0] last;

  // Outgoing frame: command, 24-bit byte address, data.
  wire [63:0] frame = {we ? 8'h02 : 8'h03, 22'd0 | addr, 2'b00, wdata};
  assign ack = (st == M_SHIFT) && ph && (n == 6'd0);
  assign rdata = {rx, miso};

  always @(posedge clk) begin
    if (!rst_n) begin
      st <= M_IDLE;
      sclk <= 1'b0;
      cs_n <= 1'b1;
      mosi <= 1'b0;
      rx <= 0;
      n <= 6'd0;
      ph <= 1'b0;
      wr <= 1'b0;
      last <= 0;
    end else begin
      case (st)
        M_IDLE: if (req) begin
          cs_n <= 1'b0;
          wr <= we;
          last <= addr;
          n <= 6'd63;
          ph <= 1'b0;
          st <= M_SHIFT;
        end
        M_SHIFT: begin
          if (!ph) begin
            sclk <= 1'b0;
            mosi <= frame[n];
            ph <= 1'b1;
          end else begin
            sclk <= 1'b1;
            rx <= {rx[29:0], miso};
            ph <= 1'b0;
            if (n == 6'd0) st <= wr ? M_END : M_OPEN;
            else n <= n - 6'd1;
          end
        end
        // A read leaves SCK high and CS low: the next word of the sequential
        // transfer follows with the next falling edge.
        M_OPEN: begin
          if (req && !we && addr == last + 1'b1) begin
            last <= addr;
            n <= 6'd31;
            ph <= 1'b0;
            st <= M_SHIFT;
          end else if (req || !hold) st <= M_END;
        end
        // SCK low first, then CS high one cycle later.
        M_END: begin
          if (sclk) sclk <= 1'b0;
          else begin
            cs_n <= 1'b1;
            st <= M_IDLE;
          end
        end
      endcase
    end
  end
endmodule
/* verilator lint_on WIDTHEXPAND */
