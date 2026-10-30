// Host frames of tile_host: TOKEN, LENGTH, ADDR, START, the illegal-frame and start-while-busy
// refusals, CLEAR, and the status byte read back over MISO. tests/test_tile_host.py runs it.
`timescale 1ns/1ps
`default_nettype none

module tile_host_tb;
  reg clk = 1'b0;
  reg rst_n = 1'b0;
  always #5 clk = ~clk;

  reg host_sclk = 1'b0;
  reg host_cs_n = 1'b1;
  reg host_mosi = 1'b0;
  wire host_miso;
  reg busy = 1'b0;
  reg done = 1'b0;
  reg [15:0] obs_word = 16'hABCD;

  wire start;
  wire [23:0] token;
  wire [15:0] length;
  wire [15:0] read_addr;
  wire [15:0] obs_data;
  wire error;
  wire [7:0] status;

  tile_host dut (
    .clk(clk), .rst_n(rst_n), .host_sclk(host_sclk), .host_cs_n(host_cs_n),
    .host_mosi(host_mosi), .host_miso(host_miso), .busy(busy), .done(done),
    .start(start), .token(token), .length(length), .read_addr(read_addr),
    .obs_word(obs_word), .obs_data(obs_data), .error(error), .status(status)
  );

  integer errors = 0;
  integer starts = 0;
  reg [7:0] got;

  always @(posedge clk) if (start) starts = starts + 1;

  task send_bit(input b);
    begin
      host_mosi = b;
      #4 host_sclk = 1'b1;
      #6 host_sclk = 1'b0;
    end
  endtask

  task send_byte(input [7:0] b);
    integer k;
    begin
      host_cs_n = 1'b0;
      #2;
      for (k = 7; k >= 0; k = k - 1) send_bit(b[k]);
      host_cs_n = 1'b1;
      #40;
    end
  endtask

  task read_byte(output [7:0] b);
    integer k;
    begin
      host_cs_n = 1'b0;
      #2;
      for (k = 7; k >= 0; k = k - 1) begin
        #2 b[k] = host_miso;
        host_sclk = 1'b1;
        #6 host_sclk = 1'b0;
      end
      host_cs_n = 1'b1;
      #40;
    end
  endtask

  initial begin
    repeat (3) @(posedge clk);
    rst_n = 1'b1;
    repeat (4) @(posedge clk);

    if (token !== 24'd0 || length !== 16'd0 || read_addr !== 16'd0 || error !== 1'b0) begin
      $display("FAIL power-on state");
      errors = errors + 1;
    end

    send_byte(8'h34); send_byte(8'h10); send_byte(8'h00);
    repeat (8) @(posedge clk);
    if (read_addr !== 16'h0010) begin
      $display("FAIL read_addr %0d", read_addr);
      errors = errors + 1;
    end
    if (obs_data !== 16'hABCD) begin
      $display("FAIL obs_data %0h", obs_data);
      errors = errors + 1;
    end

    send_byte(8'h32); send_byte(8'h11); send_byte(8'h22); send_byte(8'h33);
    repeat (8) @(posedge clk);
    if (token !== 24'h332211) begin
      $display("FAIL token %0h", token);
      errors = errors + 1;
    end

    send_byte(8'h33); send_byte(8'h08); send_byte(8'h00);
    repeat (8) @(posedge clk);
    if (length !== 16'h0008) begin
      $display("FAIL length %0d", length);
      errors = errors + 1;
    end

    send_byte(8'h31);
    repeat (8) @(posedge clk);
    if (starts !== 1) begin
      $display("FAIL START did not pulse once (%0d)", starts);
      errors = errors + 1;
    end

    busy = 1'b1;
    send_byte(8'h31);
    repeat (8) @(posedge clk);
    if (starts !== 1) begin
      $display("FAIL START while busy pulsed the schedule");
      errors = errors + 1;
    end
    if (error !== 1'b1) begin
      $display("FAIL START while busy did not set the error flag");
      errors = errors + 1;
    end
    send_byte(8'h30);
    repeat (8) @(posedge clk);
    read_byte(got);
    if (got[7] !== 1'b1 || got[6] !== 1'b1) begin
      $display("FAIL status %0h (error and busy expected)", got);
      errors = errors + 1;
    end

    busy = 1'b0;
    send_byte(8'h99);
    repeat (8) @(posedge clk);
    if (error !== 1'b1) begin
      $display("FAIL an illegal frame did not set the error flag");
      errors = errors + 1;
    end
    send_byte(8'h3f);
    repeat (8) @(posedge clk);
    if (error !== 1'b0) begin
      $display("FAIL CLEAR did not clear the error flag");
      errors = errors + 1;
    end

    done = 1'b1;
    @(posedge clk);
    done = 1'b0;
    send_byte(8'h30);
    repeat (8) @(posedge clk);
    read_byte(got);
    if (got[5] !== 1'b1) begin
      $display("FAIL status %0h (done expected)", got);
      errors = errors + 1;
    end

    send_byte(8'h31);
    repeat (8) @(posedge clk);
    if (starts !== 2) begin
      $display("FAIL START after idle did not pulse (%0d)", starts);
      errors = errors + 1;
    end
    send_byte(8'h30);
    repeat (8) @(posedge clk);
    read_byte(got);
    if (got[5] !== 1'b0) begin
      $display("FAIL START did not clear done_seen (status %0h)", got);
      errors = errors + 1;
    end

    if (errors == 0) $display("PASS");
    else $display("FAIL %0d errors", errors);
    $finish;
  end

endmodule
