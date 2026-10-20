// Testbench model of a 23LC1024-style serial SRAM that powers up in SPI mode and
// enters SQI mode with EQIO (0x38, SPI); RSTIO (0xFF, SQI) returns to SPI mode.
// Commands take effect when CS rises. In SQI mode (4 data lines, sequential mode):
//   READ 0x03: 2 command nibbles, 6 address nibbles, 2 dummy nibbles, then data
//              nibbles driven on the falling edges (most significant nibble first)
//   WRITE 0x02: 2 command nibbles, 6 address nibbles, then data nibbles
// bd_* reads a 32-bit word (most significant byte first) without using the bus.
`timescale 1ns/1ps
module sqi_sram_model #(parameter integer BYTES = 131072) (
  input  wire        sclk,
  input  wire        cs_n,
  input  wire [3:0]  sio_i,
  output reg  [3:0]  sio_o,
  output reg         sio_oe,
  input  wire [31:0] bd_addr,
  output wire [31:0] bd_data
);
  reg [7:0] mem [0:BYTES-1];
  reg [7:0] cmd;
  reg [3:0] hi;
  reg [23:0] addr;
  reg [31:0] cnt;  // rising SCK edges since CS fell
  reg quad;
  integer k;
  initial begin
    for (k = 0; k < BYTES; k = k + 1) mem[k] = 8'd0;
    sio_o = 4'd0;
    sio_oe = 1'b0;
    quad = 1'b0;
    cnt = 0;
    cmd = 8'd0;
  end

  always @(posedge sclk or posedge cs_n) begin
    if (cs_n) begin
      if (!quad && cnt == 8 && cmd == 8'h38) quad <= 1'b1;
      if (quad && cnt == 2 && cmd == 8'hFF) quad <= 1'b0;
      cnt <= 0;
    end else begin
      cnt <= cnt + 1;
      if (!quad) begin
        if (cnt < 8) cmd <= {cmd[6:0], sio_i[0]};
      end else if (cnt < 2) cmd <= {cmd[3:0], sio_i};
      else if (cnt < 8) addr <= {addr[19:0], sio_i};
      else if (cmd == 8'h02) begin
        if (!cnt[0]) hi <= sio_i;
        else begin
          mem[addr % BYTES] <= {hi, sio_i};
          addr <= addr + 1;
        end
      end
    end
  end

  // Read data: the nibble of the current byte changes on each falling edge after the dummy nibbles.
  wire [31:0] nib = cnt - 10;
  always @(negedge sclk or posedge cs_n) begin
    if (cs_n) sio_oe <= 1'b0;
    else if (quad && cmd == 8'h03 && cnt >= 10) begin
      sio_oe <= 1'b1;
      sio_o <= nib[0] ? mem[(addr + (nib >> 1)) % BYTES][3:0] : mem[(addr + (nib >> 1)) % BYTES][7:4];
    end
  end

  wire [31:0] b = bd_addr * 4;
  assign bd_data = {mem[b % BYTES], mem[(b + 1) % BYTES], mem[(b + 2) % BYTES], mem[(b + 3) % BYTES]};
endmodule
