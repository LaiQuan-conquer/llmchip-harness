// Gate-level testbench for Icarus Verilog (event-driven, so the clock tree and
// the PDK's sequential UDP cell models evaluate exactly as in a simulator with
// delays; zero-delay unless SDF_FILE is defined). The padded chip top is driven
// over its pins like tb.cpp drives the RTL: the host SPI interface, an SPI
// SRAM model on the memory pins, observation points read back from the SRAM.
//
// plusargs: +stim=FILE (lines "<run> <steps> <n> <t0> ... <tn-1>") +map=FILE
// (trace.map) +out=FILE. Output lines:
//   S <run> <pos> <token_in> <token_out> <next> <cycles> <error>
//   G <point> <layer> <n> <v0> ... (signed 32-bit values)
`timescale 1ns/1ps
`include "llmchip_params.vh"
`ifndef PERIOD
`define PERIOD 20
`endif
module gl_tb;
  reg clk = 1'b0, rst_n = 1'b0, sclk = 1'b0, cs_n = 1'b1, mosi = 1'b0;
  wire miso, busy, kv_sclk, kv_cs_n;
  always #(`PERIOD / 2.0) clk = ~clk;
`ifdef MEM_SQI
  // Quad SPI: four bidirectional lines with board pull-ups, driven by the chip's pads or the SRAM.
  wire [3:0] kv_sio, s_o;
  wire s_oe;
  `GATE_TOP u_chip (.clk(clk), .rst_n(rst_n), .host_sclk(sclk), .host_cs_n(cs_n), .host_mosi(mosi), .host_miso(miso),
                    .busy(busy), .kv_sclk(kv_sclk), .kv_cs_n(kv_cs_n), .kv_sio0(kv_sio[0]), .kv_sio1(kv_sio[1]),
                    .kv_sio2(kv_sio[2]), .kv_sio3(kv_sio[3]));
  pullup (kv_sio[0]);
  pullup (kv_sio[1]);
  pullup (kv_sio[2]);
  pullup (kv_sio[3]);
  assign kv_sio = s_oe ? s_o : 4'bzzzz;
  sqi_sram_model #(.BYTES(`SRAM_BYTES)) u_sram (.sclk(kv_sclk), .cs_n(kv_cs_n), .sio_i(kv_sio), .sio_o(s_o), .sio_oe(s_oe), .bd_addr(32'd0), .bd_data());
`else
  wire kv_mosi, kv_miso;
  `GATE_TOP u_chip (.clk(clk), .rst_n(rst_n), .host_sclk(sclk), .host_cs_n(cs_n), .host_mosi(mosi), .host_miso(miso),
                    .busy(busy), .kv_sclk(kv_sclk), .kv_cs_n(kv_cs_n), .kv_mosi(kv_mosi), .kv_miso(kv_miso));
  pulldown (kv_mosi);
  spi_sram_model #(.BYTES(`SRAM_BYTES)) u_sram (.sclk(kv_sclk), .cs_n(kv_cs_n), .mosi(kv_mosi), .miso(kv_miso), .bd_addr(32'd0), .bd_data());
`endif
  // Board pull-up: the chip releases the memory bus while idle.
  pullup (kv_cs_n);
  pulldown (kv_sclk);
`ifdef SDF_FILE
  initial $sdf_annotate(`SDF_FILE, u_chip);
`endif

  reg [47:0] rx;
  integer k;
  task frame(input integer n, input [47:0] bits);
    begin
      cs_n = 1'b0;
      repeat (4) @(posedge clk);
      for (k = n - 1; k >= 0; k = k - 1) begin
        sclk = 1'b0;
        mosi = bits[k];
        repeat (4) @(posedge clk);
        rx = {rx[46:0], miso};
        sclk = 1'b1;
        repeat (4) @(posedge clk);
      end
      sclk = 1'b0;
      repeat (4) @(posedge clk);
      cs_n = 1'b1;
      repeat (8) @(posedge clk);
    end
  endtask

  function [31:0] word(input integer addr);
    word = {u_sram.mem[4 * addr], u_sram.mem[4 * addr + 1], u_sram.mem[4 * addr + 2], u_sram.mem[4 * addr + 3]};
  endfunction

  // Trace map (observation points only).
  integer nt, t_point [0:255], t_layer [0:255], t_n [0:255], t_addr [0:255];
  integer fin, fmap, fout, r, op, point, layer, n, addr, dbg;
  integer run, steps, plen, pos, token, next, cycles, i, j;
  integer prompt [0:4095];
  reg [8*256-1:0] stim_file, map_file, out_file;
  reg [31:0] status;

  reg [8*256-1:0] vcd_file;
  initial if ($value$plusargs("vcd=%s", vcd_file)) begin
    $dumpfile(vcd_file);
    $dumpvars(1, gl_tb);
    $dumpvars(1, gl_tb.u_chip);
  end

  initial begin
    if (!$value$plusargs("stim=%s", stim_file) || !$value$plusargs("map=%s", map_file) || !$value$plusargs("out=%s", out_file)) begin
      $display("usage: +stim=FILE +map=FILE +out=FILE");
      $finish;
    end
    fmap = $fopen(map_file, "r");
    nt = 0;
    while ($fscanf(fmap, "%d %d %d %d %d %d", op, point, layer, n, addr, dbg) == 6) begin
      if (dbg == 0) begin
        t_point[nt] = point; t_layer[nt] = layer; t_n[nt] = n; t_addr[nt] = addr;
        nt = nt + 1;
      end
    end
    fin = $fopen(stim_file, "r");
    fout = $fopen(out_file, "w");
    repeat (8) @(posedge clk);
    rst_n = 1'b1;
    repeat (8) @(posedge clk);
    while ($fscanf(fin, "%d %d %d", run, steps, plen) == 3) begin
      for (i = 0; i < plen; i = i + 1) r = $fscanf(fin, "%d", prompt[i]);
      token = prompt[0];
      for (pos = 0; pos < steps; pos = pos + 1) begin
        frame(48, {8'h10, token[15:0], pos[15:0], 8'd1});
        cycles = 0;
        while (busy !== 1'b1 && cycles < 64) begin @(posedge clk); cycles = cycles + 1; end
        while (busy === 1'b1) begin @(posedge clk); cycles = cycles + 1; end
        frame(40, {8'h20, 32'd0});
        status = rx[31:0];
        next = (pos < plen - 1) ? prompt[pos + 1] : status[15:0];
        $fwrite(fout, "S %0d %0d %0d %0d %0d %0d %0d\n", run, pos, token, status[15:0], next, cycles, status[30]);
        if (!status[30]) begin
          for (j = 0; j < nt; j = j + 1) begin
            $fwrite(fout, "G %0d %0d %0d", t_point[j], t_layer[j], t_n[j]);
            for (i = 0; i < t_n[j]; i = i + 1) $fwrite(fout, " %0d", $signed(word(t_addr[j] + i)));
            $fwrite(fout, "\n");
          end
        end
        if (status[30]) pos = steps;
        else if (pos >= plen - 1 && next == 1) pos = steps;
        token = next;
      end
    end
    $fclose(fout);
    $finish;
  end
endmodule
