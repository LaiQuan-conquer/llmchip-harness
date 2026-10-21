// Host interface of the spatial engine: an SPI slave (mode 0) sampled with the chip
// clock; SCK must be at most clk / 8. Frames, most significant bit first:
//   0x10 START  token[15:0] pos[15:0] mode[7:0]   start a step (ignored while busy;
//                                                 mode is accepted and not used)
//   0x20 STATUS -> busy, error, done, 13'b0, token_out[15:0]
//   0x30 READ   addr[15:0] -> word[addr], word[addr+1], ... (32 bits each, while SCK runs)
//        the observation words: the residual stream after the attention and after the
//        FFN of every layer, the logits and the token of the last step; read while idle.
// `done` is set when a step ends and cleared by the next START.
`timescale 1ns/1ps
module sp_host (
  input  wire        clk,
  input  wire        rst_n,
  input  wire        sclk_i,
  input  wire        cs_n_i,
  input  wire        mosi_i,
  output wire        miso,
  output reg         start,
  output reg  [15:0] token,
  output reg  [15:0] pos,
  input  wire        busy,
  input  wire        error,
  input  wire        done,
  input  wire [15:0] token_out,
  output reg  [10:0] obs_sel,
  input  wire [31:0] obs_word
);
  reg [2:0] s_sclk, s_cs;  // synchronizers (two stages) plus the previous value
  reg [1:0] s_mosi;
  reg [47:0] sh;
  reg [5:0] bits;
  reg finished, out;
  reg reading, load;  // READ: words are being sent; load the next word on this clock
  reg [31:0] rd;
  reg [4:0] rbit;

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
      sh <= 48'd0;
      token <= 16'd0;
      pos <= 16'd0;
      reading <= 1'b0;
      load <= 1'b0;
      rd <= 32'd0;
      rbit <= 5'd0;
      obs_sel <= 11'd0;
    end else begin
      s_sclk <= {s_sclk[1:0], sclk_i};
      s_cs <= {s_cs[1:0], cs_n_i};
      s_mosi <= {s_mosi[0], mosi_i};
      start <= 1'b0;
      load <= 1'b0;
      if (load) rd <= obs_word;
      if (done) finished <= 1'b1;
      if (!cs_active) begin
        bits <= 6'd0;
        reading <= 1'b0;
        if (cs_end && bits == 6'd48 && sh[47:40] == 8'h10 && !busy) begin
          token <= sh[39:24];
          pos <= sh[23:8];
          start <= 1'b1;
          finished <= 1'b0;
        end
      end else begin
        if (rise) begin
          if (bits != 6'd63) bits <= bits + 6'd1;
          if (bits == 6'd7 && {sh[6:0], s_mosi[1]} == 8'h20) sh <= {busy, error, finished, 13'd0, token_out, 16'd0};
          else sh <= {sh[46:0], s_mosi[1]};
          if (bits == 6'd23 && sh[22:15] == 8'h30) begin
            // READ: the address is complete; the first word is loaded before the next fall.
            obs_sel <= {sh[9:0], s_mosi[1]};
            reading <= 1'b1;
            load <= 1'b1;
            rbit <= 5'd0;
          end
          if (reading) begin
            rd <= {rd[30:0], 1'b0};
            rbit <= rbit + 5'd1;
            if (rbit == 5'd31) begin
              obs_sel <= obs_sel + 11'd1;
              load <= 1'b1;
            end
          end
        end
        if (fall && reading) out <= rd[31];
        else if (fall && bits >= 6'd8) out <= sh[47];
      end
    end
  end
endmodule
