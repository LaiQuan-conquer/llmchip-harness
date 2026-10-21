// Bit-serial arithmetic of the spatial engine (taalas-spatial/1).
//
// Values travel LSB first, one bit per clock, in two's complement; a stream
// keeps repeating its sign bit after its width (infinite sign extension), so
// sums of streams are exact as long as the stream runs long enough. The weights
// of a layer are not stored: they are the wiring of these adders (generated).

`timescale 1ns/1ps

// a + b, or a - b with SUB (a + ~b + 1). `clr` (reset, or the start of a run) loads the
// carry for the first, least significant, bit: 0, or 1 for a subtraction.
module sp_sadd #(parameter SUB = 0) (
  input  wire clk,
  input  wire clr,
  input  wire a,
  input  wire b,
  output wire s
);
  reg c;
  wire bb = SUB ? ~b : b;
  assign s = a ^ bb ^ c;
  always @(posedge clk) begin
    if (clr) c <= (SUB != 0);
    else c <= (a & bb) | (a & c) | (bb & c);
  end
endmodule

// A tapped delay line: t[j-1] is the stream delayed by j bits (multiplied by 2^j). One line
// per group serves every digit of the group's scale mantissa. `clr` (reset, or the start of
// a run) empties it.
module sp_taps #(parameter N = 1) (
  input  wire         clk,
  input  wire         clr,
  input  wire         a,
  output reg  [N-1:0] t
);
  generate
    if (N == 1) begin : one
      always @(posedge clk) begin
        if (clr) t <= 1'b0;
        else t <= a;
      end
    end else begin : many
      always @(posedge clk) begin
        if (clr) t <= {N{1'b0}};
        else t <= {t[N-2:0], a};
      end
    end
  endgenerate
endmodule

// The output of a row: drops the low S bits with the rounding mode of the numeric spec
// (half-up, half-even or truncate, applied as a carry into bit S), collects the W result
// bits, and narrows them (saturate or wrap) once the stream has shown its true sign. The
// phases come from the module (shared by its rows): lo = bits below the half bit, hb = the
// half bit (bit S-1), rb = bit S, col = bits S..S+W-1, hi = the bits above, last = the final bit.
// y holds the result until the next run.
module sp_out #(parameter W = 16, ROUND = 0, WRAP = 0) (
  input  wire         clk,
  input  wire         rst_n,
  input  wire         first,
  input  wire         lo,
  input  wire         hb,
  input  wire         rb,
  input  wire         col,
  input  wire         hi,
  input  wire         last,
  input  wire         a,
  output reg  [W-1:0] y
);
  reg sticky, half, c, ovf;
  // The rounding carry enters at bit S: half-up adds the half, half-even only when the
  // result would be odd or bits below the half are set, truncate never.
  wire up = (ROUND == 0) ? half : (ROUND == 1) ? (half & (sticky | a)) : 1'b0;
  wire ci = rb ? up : c;
  wire o = a ^ ci;
  always @(posedge clk) begin
    if (!rst_n) begin
      sticky <= 1'b0; half <= 1'b0; c <= 1'b0; ovf <= 1'b0; y <= {W{1'b0}};
    end else begin
      if (first) ovf <= 1'b0;
      if (lo) sticky <= (first ? 1'b0 : sticky) | a;
      if (hb) half <= a;
      if (rb || col || hi) c <= a & ci;
      if (col) y <= {o, y[W-1:1]};
      if (hi && o != y[W-1]) ovf <= 1'b1;
      // The final bit is the true sign of the rounded value.
      if (last && WRAP == 0 && (ovf || o != y[W-1])) y <= o ? {1'b1, {(W-1){1'b0}}} : {1'b0, {(W-1){1'b1}}};
    end
  end
endmodule

// A signed multiplier (one module, so synthesis maps it once for all its instances).
module sp_mul #(parameter AW = 18, BW = 18) (
  input  wire signed [AW-1:0]    a,
  input  wire signed [BW-1:0]    b,
  output wire signed [AW+BW-1:0] p
);
  assign p = a * b;
endmodule
