// The top level of a taalas-tile/1 slice: the host frames (SPI, command 0x30 READ and 0x31
// START as in the vector engine's host interface), the schedule marcher that walks the ROM
// addresses of one bank, the parallel MAC array and the observation readback.
//
// The marcher is the skeleton of the generated schedule: the R3 bundle proves that the RTL is
// synthesizable, lint-clean and follows the register rules; the per-phase schedule states and
// the bit-exact datapath follow with the generator's schedule encoding.
`default_nettype none

module tile_top #(
  parameter integer MACS = /*MACS*/ 128,
  parameter integer BITS = /*BITS*/ 4,
  parameter integer ACC  = 32,
  parameter integer ADDR = 20,
  parameter         INIT = "rom.hex"
) (
  input  wire        clk,
  input  wire        rst_n,
  input  wire        host_sclk,
  input  wire        host_cs_n,
  input  wire        host_mosi,
  output wire        host_miso,
  output wire        busy,
  output wire [7:0]  status
);

  localparam [1:0] IDLE = 2'd0, RUN = 2'd1, DONE = 2'd2;
  localparam [7:0] CMD_READ = 8'h30, CMD_START = 8'h31;

  // ---- host SPI receiver (host_sclk domain), synchronized into clk -------------------
  reg [2:0]  bit_count;
  reg [7:0]  shift;
  reg [7:0]  byte_in;
  reg        byte_ready;
  reg [2:0]  ready_sync;

  always @(posedge host_sclk or negedge rst_n) begin
    if (!rst_n) begin
      bit_count  <= 3'd0;
      shift      <= 8'd0;
      byte_in    <= 8'd0;
      byte_ready <= 1'b0;
    end else begin
      byte_ready <= 1'b0;
      if (!host_cs_n) begin
        shift     <= {shift[6:0], host_mosi};
        bit_count <= bit_count + 3'd1;
        if (bit_count == 3'd7) begin
          byte_in    <= {shift[6:0], host_mosi};
          bit_count  <= 3'd0;
          byte_ready <= 1'b1;
        end
      end else begin
        bit_count <= 3'd0;
      end
    end
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      ready_sync <= 3'd0;
    end else begin
      ready_sync <= {ready_sync[1:0], byte_ready};
    end
  end

  wire byte_valid = ready_sync[2] & ~ready_sync[1];

  // ---- command decode and the schedule marcher (clk domain) -------------------------
  reg [1:0]      state;
  reg [7:0]      last_command;
  reg [ADDR-1:0] address;

  reg [7:0]      checksum;
  reg            busylevel;
  reg [7:0]      status_reg;
  reg            tx_latch;

  wire [MACS*BITS-1:0] rom_data;
  wire [MACS*ACC-1:0]  mac_y;
  wire [ADDR-1:0]      last_address = {ADDR{1'b1}};

  tile_rom #(.WIDTH(MACS*BITS), .DEPTH(1 << ADDR), .ADDR(ADDR), .INIT(INIT)) rom (
    .addr(address), .q(rom_data)
  );

  tile_mac #(.MACS(MACS), .BITS(BITS), .ACC(ACC)) mac (
    .clk(clk),
    .rst_n(rst_n),
    .en(1'b1),
    .phase_start(1'b0),
    .x(16'sd0),
    .w(rom_data),
    .y(mac_y)
  );

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state        <= IDLE;
      last_command <= 8'd0;
      address      <= {ADDR{1'b0}};
      checksum     <= 8'd0;
      busylevel    <= 1'b0;
      status_reg   <= 8'h5a;
      tx_latch     <= 1'b0;
    end else begin
      if (byte_valid) begin
        last_command <= byte_in;
        if (byte_in == CMD_START) begin
          state     <= RUN;
          address   <= {ADDR{1'b0}};
          checksum  <= 8'd0;
          busylevel <= 1'b1;
        end else if (byte_in == CMD_READ) begin
          status_reg <= last_command + 8'h01;
          tx_latch   <= ~tx_latch;
        end
      end
      case (state)
        IDLE: begin
          busylevel <= 1'b0;
        end
        RUN: begin
          checksum <= checksum + {7'd0, ^mac_y} + rom_data[7:0];
          if (address == last_address) begin
            state     <= DONE;
            busylevel <= 1'b0;
          end else begin
            address <= address + 1'b1;
          end
        end
        DONE: begin
          status_reg <= checksum;
          state      <= IDLE;
        end
        default: begin
          state <= IDLE;
        end
      endcase
    end
  end

  assign busy      = busylevel;
  assign status    = status_reg;
  assign host_miso = shift[7] ^ tx_latch;

endmodule
