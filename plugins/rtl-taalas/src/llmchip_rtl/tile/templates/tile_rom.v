// Synchronous single-port ROM of taalas-tile/1, view 1: behavioural model with $readmemh.
// The generator writes one instance per bank, with INIT pointing at the bank's hex file.
module tile_rom #(
  parameter integer WIDTH = 512,
  parameter integer DEPTH = 1024,
  parameter integer ADDR = 10,
  parameter         INIT = "rom.hex"
) (
  input  wire                clk,
  input  wire                en,
  input  wire [ADDR-1:0]     addr,
  output reg  [WIDTH-1:0]    dout
);

  reg [WIDTH-1:0] mem [0:DEPTH-1];

  initial begin
    $readmemh(INIT, mem);
  end

  always @(posedge clk) begin
    if (en) begin
      dout <= mem[addr];
    end
  end

endmodule
