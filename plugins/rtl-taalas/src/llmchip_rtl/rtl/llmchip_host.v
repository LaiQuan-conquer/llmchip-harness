// Host interface: an SPI slave (mode 0) sampled with the chip clock; SCK must
// be at most clk / 8. Frames, most significant bit first:
//   0x10 START  token[15:0] pos[15:0] mode[7:0]   start a step (ignored while busy)
//   0x20 STATUS -> busy, error, done, 13'b0, token_out[15:0]
// `done` is set when a step ends and cleared by the next START. One shift
// register receives the frame and, after a STATUS command, sends the status.
`timescale 1ns/1ps
module llmchip_host (
  input  wire        clk,
  input  wire        rst_n,
  input  wire        sclk_i,
  input  wire        cs_n_i,
  input  wire        mosi_i,
  output wire        miso,
  output reg         start,
  output reg  [15:0] token,
  output reg  [15:0] pos,
  output reg  [1:0]  mode,
  input  wire        busy,
  input  wire        error,
  input  wire        done,
  input  wire [15:0] token_out
);
  reg [2:0] s_sclk, s_cs;  // synchronizers (two stages) plus the previous value
  reg [1:0] s_mosi;
  reg [47:0] sh;
  reg [5:0] bits;
  reg finished, out;

  wire rise = s_sclk[1] && !s_sclk[2];
  wire fall = !s_sclk[1] && s_sclk[2];
  wire cs_active = !s_cs[1];
  wire cs_end = s_cs[1] && !s_cs[2];
  assign miso = out;

  always @(posedge clk) begin
    if (!rst_n) begin
      s_sclk <= 3'b000;
      s_cs <= 3'b111;
      s_mosi <= 2'b00;
      out <= 1'b0;
      start <= 1'b0;
      bits <= 6'd0;
      finished <= 1'b0;
      mode <= 2'b00;
      sh <= 48'd0;
      token <= 16'd0;
      pos <= 16'd0;
    end else begin
      s_sclk <= {s_sclk[1:0], sclk_i};
      s_cs <= {s_cs[1:0], cs_n_i};
      s_mosi <= {s_mosi[0], mosi_i};
      start <= 1'b0;
      if (done) finished <= 1'b1;
      if (!cs_active) begin
        bits <= 6'd0;
        if (cs_end && bits == 6'd48 && sh[47:40] == 8'h10 && !busy) begin
          token <= sh[39:24];
          pos <= sh[23:8];
          mode <= sh[1:0];
          start <= 1'b1;
          finished <= 1'b0;
        end
      end else begin
        if (rise) begin
          if (bits != 6'd63) bits <= bits + 6'd1;
          if (bits == 6'd7 && {sh[6:0], s_mosi[1]} == 8'h20) sh <= {busy, error, finished, 13'd0, token_out, 16'd0};
          else sh <= {sh[46:0], s_mosi[1]};
        end
        if (fall && bits >= 6'd8) out <= sh[47];
      end
    end
  end
endmodule
