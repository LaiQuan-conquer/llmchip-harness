// A read-only lookup table of taalas-tile/1 (the rsqrt / exp / sigmoid tables of the numeric spec).
// The generator writes the table's contents as an $readmemh file and instantiates one table per
// function in the engine.
module tile_lut #(
  parameter integer WIDTH = 16,
  parameter integer DEPTH = 192,
  parameter integer ADDR = 8,
  parameter         INIT = "lut.hex"
) (
  input  wire             clk,
  input  wire             rst_n,
  input  wire             en,
  input  wire [ADDR-1:0]  index,
  output reg  [WIDTH-1:0] value
);

  reg [WIDTH-1:0] mem [0:DEPTH-1];

  initial begin
    $readmemh(INIT, mem);
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      value <= {WIDTH{1'b0}};
    end else if (en) begin
      value <= mem[index];
    end
  end

endmodule
