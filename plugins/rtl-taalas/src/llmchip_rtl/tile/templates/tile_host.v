// The host interface of a taalas-tile/1 chip (drafts/Qwen05B_Taalas_Refactor_Plan_zh.md §12.1 B1):
// SPI frames that carry the token to run and read back an observation word.
//
//   0x31 START            one token, with the token last written
//   0x32 TOKEN  b0 b1 b2  24-bit token id, little endian
//   0x33 LENGTH b0 b1     tokens to generate, little endian
//   0x34 ADDR   b0 b1     observation word index
//   0x3F CLEAR            clears the error flag
//   0x30 READ             the next byte transfer carries the status byte
//
// Any other command byte sets `error` and changes nothing; START while `busy` is set is refused the
// same way, so a host cannot restart a running token. The observation itself is a parallel word
// (`obs_word` in, addressed by `read_addr`) and is also available on the status path.
`default_nettype none

module tile_host #(
  parameter [7:0] CMD_START = 8'h31,
  parameter [7:0] CMD_TOKEN = 8'h32,
  parameter [7:0] CMD_LENGTH = 8'h33,
  parameter [7:0] CMD_ADDR = 8'h34,
  parameter [7:0] CMD_READ = 8'h30,
  parameter [7:0] CMD_CLEAR = 8'h3f
) (
  input  wire        clk,
  input  wire        rst_n,
  input  wire        host_sclk,
  input  wire        host_cs_n,
  input  wire        host_mosi,
  output wire        host_miso,
  input  wire        busy,
  input  wire        done,
  output reg         start,
  output reg  [23:0] token,
  output reg  [15:0] length,
  output reg  [15:0] read_addr,
  input  wire [15:0] obs_word,
  output wire [15:0] obs_data,
  output reg         error,
  output wire [7:0]  status
);

  // ---- byte receiver (host_sclk) ------------------------------------------------------
  reg [2:0] bit_count;
  reg [6:0] shift;
  reg [7:0] byte_in;
  reg       byte_ready;
  reg [2:0] ready_sync;
  reg       ready_seen;

  always @(posedge host_sclk or negedge rst_n) begin
    if (!rst_n) begin
      bit_count  <= 3'd0;
      shift      <= 7'd0;
      byte_in    <= 8'd0;
      byte_ready <= 1'b0;
    end else begin
      byte_ready <= 1'b0;
      if (host_cs_n) begin
        bit_count <= 3'd0;
      end else begin
        shift    <= {shift[5:0], host_mosi};
        bit_count <= bit_count + 3'd1;
        if (bit_count == 3'd7) begin
          byte_in    <= {shift[6:0], host_mosi};
          bit_count  <= 3'd0;
          byte_ready <= 1'b1;
        end
      end
    end
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      ready_sync <= 3'd0;
      ready_seen <= 1'b0;
    end else begin
      ready_sync <= {ready_sync[1:0], byte_ready};
      ready_seen <= ready_sync[1];
    end
  end

  // One pulse per received byte: byte_ready stays high until the next host clock edge, so a
  // level edge detector would fire again when it clears.
  wire byte_valid = ready_sync[1] & ~ready_seen;

  // ---- frame decoder (clk) ------------------------------------------------------------
  localparam [2:0] F_CMD = 3'd0, F_TOK0 = 3'd1, F_TOK1 = 3'd2, F_TOK2 = 3'd3,
                   F_LEN0 = 3'd4, F_LEN1 = 3'd5, F_ADDR0 = 3'd6, F_ADDR1 = 3'd7;

  reg [2:0] fstate;
  reg       done_seen;
  reg       read_pending;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      fstate    <= F_CMD;
      start     <= 1'b0;
      token     <= 24'd0;
      length    <= 16'd0;
      read_addr <= 16'd0;
      error     <= 1'b0;
      done_seen <= 1'b0;
      read_pending <= 1'b0;
    end else begin
      start <= 1'b0;
      if (done) begin
        done_seen <= 1'b1;
      end
      if (byte_valid && read_pending) begin
        read_pending <= 1'b0;
      end else if (byte_valid) begin
        case (fstate)
          F_CMD: begin
            if (byte_in == CMD_START) begin
              if (busy) begin
                error <= 1'b1;
              end else begin
                start     <= 1'b1;
                done_seen <= 1'b0;
              end
            end else if (byte_in == CMD_TOKEN) begin
              fstate <= F_TOK0;
            end else if (byte_in == CMD_LENGTH) begin
              fstate <= F_LEN0;
            end else if (byte_in == CMD_ADDR) begin
              fstate <= F_ADDR0;
            end else if (byte_in == CMD_READ) begin
              read_pending <= 1'b1;
            end else if (byte_in == CMD_CLEAR) begin
              error <= 1'b0;
            end else begin
              error <= 1'b1;
            end
          end
          F_TOK0: begin
            token[7:0] <= byte_in;
            fstate <= F_TOK1;
          end
          F_TOK1: begin
            token[15:8] <= byte_in;
            fstate <= F_TOK2;
          end
          F_TOK2: begin
            token[23:16] <= byte_in;
            fstate <= F_CMD;
          end
          F_LEN0: begin
            length[7:0] <= byte_in;
            fstate <= F_LEN1;
          end
          F_LEN1: begin
            length[15:8] <= byte_in;
            fstate <= F_CMD;
          end
          F_ADDR0: begin
            read_addr[7:0] <= byte_in;
            fstate <= F_ADDR1;
          end
          F_ADDR1: begin
            read_addr[15:8] <= byte_in;
            fstate <= F_CMD;
          end
          default: fstate <= F_CMD;
        endcase
      end
    end
  end

  // The status byte is shifted out MSB first while the host clocks a transfer; the host reads a
  // byte after sending READ. `status` is sampled combinationally from the clk domain: it is a
  // slow status word, and a bit that changes inside a transfer is a known limitation of the
  // bring-up interface.
  assign host_miso = status[3'd7 - bit_count];
  assign obs_data = obs_word;
  assign status = {error, busy, done_seen, 5'b0};

endmodule
