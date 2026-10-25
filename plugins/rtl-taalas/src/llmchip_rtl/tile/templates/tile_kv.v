// The KV cache of one engine of taalas-tile/1 (requirements FR-ARC, FR-RTL-01): keys after RoPE and
// values of every position fed so far, per KV head, written once per token and read by the
// attention pass. One write port and one read port; the read register has a reset.
`default_nettype none

module tile_kv #(
  parameter integer HD = 8,
  parameter integer LOG2CONTEXT = 6,
  parameter integer ADDR = 16
) (
  input  wire                       clk,
  input  wire                       rst_n,
  input  wire                       wr_en,
  input  wire [15:0]                wr_head,
  input  wire [15:0]                wr_pos,
  input  wire signed [HD*16-1:0]    wr_data,
  input  wire                       rd_en,
  input  wire [15:0]                rd_head,
  input  wire [15:0]                rd_pos,
  output reg  signed [HD*16-1:0]    rd_data,
  output reg                        rd_valid
);

  // One row per (head, position); a row is HD int16 values, and ADDR covers KV_HEADS x CONTEXT rows.
  reg [HD*16-1:0] rows [0:(1 << ADDR)-1];

  wire [ADDR-1:0] wr_index = (wr_head << LOG2CONTEXT) + wr_pos;
  wire [ADDR-1:0] rd_index = (rd_head << LOG2CONTEXT) + rd_pos;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      rd_data  <= {HD*16{1'b0}};
      rd_valid <= 1'b0;
    end else begin
      rd_valid <= 1'b0;
      if (wr_en) begin
        rows[wr_index] <= wr_data;
      end
      if (rd_en) begin
        rd_data  <= rows[rd_index];
        rd_valid <= 1'b1;
      end
    end
  end

endmodule
