// The combinational ROM of taalas-tile/1, view 1: a behavioural model with $readmemh. The register
// that makes S1 of the pipeline is the consumer's (tile_engine's word register), so the ROM is one
// memory and one mux tree.
module tile_rom #(
  parameter integer WIDTH = 512,
  parameter integer DEPTH = 1024,
  parameter integer ADDR = 10,
  parameter         INIT = "rom.hex"
) (
  input  wire [ADDR-1:0]     addr,
  output wire [WIDTH-1:0]    q
);

  reg [WIDTH-1:0] mem [0:DEPTH-1];

  initial begin
    $readmemh(INIT, mem);
  end

  assign q = mem[addr];

endmodule
