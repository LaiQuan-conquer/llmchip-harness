// The banked ROM of taalas-tile/1, view 1 (drafts/tile-180nm-pipeline.md §2.2): four banks of
// 2^(ADDR-2) words, each read with its own offset, the bank selected by the address's top two bits.
// The generator writes one $readmemh file per bank, which halves the mux depth against a flat
// array of the same size; the read register stays in the consumer (the engine's S1 stage).
`default_nettype none

module tile_rom #(
  parameter integer WIDTH = 512,
  parameter integer ADDR = 9,
  parameter         INIT0 = "rom_bank0.hex",
  parameter         INIT1 = "rom_bank1.hex",
  parameter         INIT2 = "rom_bank2.hex",
  parameter         INIT3 = "rom_bank3.hex"
) (
  input  wire [ADDR-1:0]     addr,
  output wire [WIDTH-1:0]    q
);

  localparam integer PER = 1 << (ADDR - 2);

  localparam integer OFFBITS = (ADDR > 2) ? (ADDR - 2) : 1;
  wire [1:0]        bank = addr[ADDR-1 -: 2];
  wire [OFFBITS-1:0] off;
  generate
    if (ADDR > 2) begin : g_off
      assign off = addr[ADDR-3:0];
    end else begin : g_flat
      assign off = 1'b0;
    end
  endgenerate

  reg [WIDTH-1:0] mem0 [0:PER-1];
  reg [WIDTH-1:0] mem1 [0:PER-1];
  reg [WIDTH-1:0] mem2 [0:PER-1];
  reg [WIDTH-1:0] mem3 [0:PER-1];

  initial begin
    $readmemh(INIT0, mem0);
    $readmemh(INIT1, mem1);
    $readmemh(INIT2, mem2);
    $readmemh(INIT3, mem3);
  end

  assign q = (bank == 2'd0) ? mem0[off] : (bank == 2'd1) ? mem1[off] : (bank == 2'd2) ? mem2[off] : mem3[off];

endmodule
