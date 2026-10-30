// The token embedding of taalas-tile/1: one row of the embedding ROM, dequantized exactly as
// GraphL2's embedding node (x_i = sat(rshift(q_i * m[row, i/group], exp - frac_x))). One lane per
// cycle after the row's word is registered.
`default_nettype none

module tile_embed #(
  parameter integer HIDDEN = 32,
  parameter integer VOCAB = 128,
  parameter integer GROUP = 16,
  parameter integer MACS = 8,
  parameter integer BITS = 4,
  parameter integer CPG = 2,
  parameter integer RES = 16,
  parameter integer EXP = 21,
  parameter integer FRAC_X = 18,
  parameter         INIT0 = "embed_bank0.hex",
  parameter         INIT1 = "embed_bank1.hex",
  parameter         INIT2 = "embed_bank2.hex",
  parameter         INIT3 = "embed_bank3.hex",
  parameter         INIT_M = "embed_m.hex"
) (
  input  wire                         clk,
  input  wire                         rst_n,
  input  wire                         start,
  input  wire [23:0]                  token,
  output reg  signed [HIDDEN*RES-1:0] x,
  output reg                          done
);

  localparam integer CHUNKS = HIDDEN / MACS;
  localparam integer NG = HIDDEN / GROUP;
  localparam integer ADDR = $clog2(VOCAB * CHUNKS);
  localparam integer SH = EXP - FRAC_X;

  localparam [2:0] IDLE = 3'd0, READ = 3'd1, LANE = 3'd2, DONE = 3'd3;

  reg [2:0] state;
  reg [31:0] c;
  reg [31:0] lane;
  reg [23:0] tok;
  reg [31:0] address;
  reg [23:0] token_q;
  reg [15:0] mant [0:VOCAB*NG-1];
  wire [MACS*BITS-1:0] word;

  initial begin
    $readmemh(INIT_M, mant);
  end

  tile_rom #(.WIDTH(MACS*BITS), .ADDR(ADDR), .INIT0(INIT0), .INIT1(INIT1), .INIT2(INIT2), .INIT3(INIT3)) rom (
    .addr(address[ADDR-1:0]), .q(word)
  );

  wire signed [15:0] mval = mant[tok*NG + (c / CPG)];
  wire signed [64:0] prod = $signed(word[lane*BITS +: BITS]) * mval;
  wire signed [64:0] shifted = (SH > 0) ? ((prod + (65'sd1 <<< (SH - 1))) >>> SH) : (prod <<< (-SH));
  wire signed [64:0] hi = (65'sd1 <<< (RES - 1)) - 65'sd1;
  wire signed [64:0] lo = -(65'sd1 <<< (RES - 1));

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state   <= IDLE;
      c       <= 32'd0;
      lane    <= 32'd0;
      tok     <= 24'd0;
      token_q <= 24'd0;
      address <= 32'd0;
      x       <= {HIDDEN*RES{1'b0}};
      done    <= 1'b0;
    end else begin
      done <= 1'b0;
      case (state)
        IDLE: begin
          if (start) begin
            token_q <= token;
            tok     <= token;
            c       <= 32'd0;
            lane    <= 32'd0;
            address <= token * CHUNKS;
            state   <= READ;
          end
        end
        READ: begin
          state <= LANE;
        end
        LANE: begin
          if (shifted > hi) begin
            x[(c*MACS + lane)*RES +: RES] <= hi[RES-1:0];
          end else if (shifted < lo) begin
            x[(c*MACS + lane)*RES +: RES] <= lo[RES-1:0];
          end else begin
            x[(c*MACS + lane)*RES +: RES] <= shifted[RES-1:0];
          end
          if (lane == MACS - 1) begin
            lane <= 32'd0;
            if (c == CHUNKS - 1) begin
              state <= DONE;
            end else begin
              c <= c + 32'd1;
              address <= address + 32'd1;
              state <= READ;
            end
          end else begin
            lane <= lane + 32'd1;
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
