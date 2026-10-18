// Testbench model of a 23LC1024-style SPI SRAM (mode 0, sequential mode):
// READ 0x03 / WRITE 0x02, 24-bit byte address. bd_* reads a 32-bit word
// (most significant byte first) for the testbench without using the bus.
`timescale 1ns/1ps
module spi_sram_model #(parameter integer BYTES = 131072) (
  input  wire        sclk,
  input  wire        cs_n,
  input  wire        mosi,
  output reg         miso,
  input  wire [31:0] bd_addr,
  output wire [31:0] bd_data
);
  reg [7:0] mem [0:BYTES-1];
  reg [7:0] cmd, sh;
  reg [23:0] addr, raddr;
  reg [31:0] bits;
  integer k;
  initial begin
    for (k = 0; k < BYTES; k = k + 1) mem[k] = 8'd0;
    miso = 1'b0;
  end

  always @(posedge sclk or posedge cs_n) begin
    if (cs_n) bits <= 0;
    else begin
      bits <= bits + 1;
      if (bits < 8) cmd <= {cmd[6:0], mosi};
      else if (bits < 32) addr <= {addr[22:0], mosi};
      else if (cmd == 8'h02) begin
        sh <= {sh[6:0], mosi};
        if (bits[2:0] == 3'd7) begin
          mem[addr % BYTES] <= {sh[6:0], mosi};
          addr <= addr + 1;
        end
      end
    end
  end

  // Read data changes on the falling edge after the last address bit.
  always @(negedge sclk) begin
    if (!cs_n && cmd == 8'h03 && bits >= 32) begin
      if (bits == 32) begin
        miso <= mem[addr % BYTES][7];
        raddr <= addr;
      end else begin
        miso <= mem[(bits[2:0] == 3'd0 ? raddr + 1 : raddr) % BYTES][7 - bits[2:0]];
        if (bits[2:0] == 3'd0) raddr <= raddr + 1;
      end
    end
  end

  wire [31:0] b = bd_addr * 4;
  assign bd_data = {mem[b % BYTES], mem[(b + 1) % BYTES], mem[(b + 2) % BYTES], mem[(b + 3) % BYTES]};
endmodule
