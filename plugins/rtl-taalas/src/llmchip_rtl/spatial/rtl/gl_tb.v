// Gate-level testbench of the spatial engine for Icarus Verilog (event-driven, so the clock
// tree and the PDK's sequential UDP cell models evaluate exactly as in a simulator with
// delays; zero-delay unless SDF_FILE is defined). The padded chip top is driven over its pins
// like tb.cpp drives the RTL: the host SPI interface, and the observation points read with
// the host READ command. After reset it waits for the zero pass that initializes the KV
// rings (busy high); an X that survived it would reach the observation points and differ
// from L2.
//
// plusargs: +stim=FILE (lines "<run> <steps> <n> <t0> ... <tn-1>") +map=FILE
// (trace.map) +out=FILE. Output lines:
//   S <run> <pos> <token_in> <token_out> <next> <cycles> <error>
//   G <point> <layer> <n> <v0> ... (signed 32-bit values)
`timescale 1ns/1ps
`ifndef PERIOD
`define PERIOD 20
`endif
module gl_tb;
  reg clk = 1'b0, rst_n = 1'b0, sclk = 1'b0, cs_n = 1'b1, mosi = 1'b0;
  wire miso, busy;
  always #(`PERIOD / 2.0) clk = ~clk;
  `GATE_TOP u_chip (.clk(clk), .rst_n(rst_n), .host_sclk(sclk), .host_cs_n(cs_n), .host_mosi(mosi), .host_miso(miso),
                    .busy(busy));
`ifdef SDF_FILE
  initial $sdf_annotate(`SDF_FILE, u_chip);
`endif

  reg [47:0] rx;
  reg [31:0] wordrx;
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

  // Host READ: count words from addr into words[0 .. count-1], in one frame.
  reg [31:0] words [0:2047];
  integer b, w;
  task read_words(input integer addr, input integer count);
    reg [23:0] head;
    begin
      head = {8'h30, addr[15:0]};
      cs_n = 1'b0;
      repeat (4) @(posedge clk);
      for (b = 0; b < 24 + 32 * count; b = b + 1) begin
        sclk = 1'b0;
        mosi = b < 24 ? head[23 - b] : 1'b0;
        repeat (4) @(posedge clk);
        wordrx = {wordrx[30:0], miso};
        if (b >= 24 && (b - 24) % 32 == 31) words[(b - 24) / 32] = wordrx;
        sclk = 1'b1;
        repeat (4) @(posedge clk);
      end
      sclk = 1'b0;
      repeat (4) @(posedge clk);
      cs_n = 1'b1;
      repeat (8) @(posedge clk);
    end
  endtask

  integer nt, t_point [0:255], t_layer [0:255], t_n [0:255], t_addr [0:255];
  integer fin, fmap, fout, r, op, point, layer, n, addr, dbg, first, total;
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
    first = 1 << 30;
    total = 0;
    while ($fscanf(fmap, "%d %d %d %d %d %d", op, point, layer, n, addr, dbg) == 6) begin
      t_point[nt] = point; t_layer[nt] = layer; t_n[nt] = n; t_addr[nt] = addr;
      if (addr < first) first = addr;
      if (addr + n > total) total = addr + n;
      nt = nt + 1;
    end
    fin = $fopen(stim_file, "r");
    fout = $fopen(out_file, "w");
    repeat (8) @(posedge clk);
    rst_n = 1'b1;
    repeat (8) @(posedge clk);
    while (busy !== 1'b0) @(posedge clk);  // the zero pass of the KV rings
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
          read_words(first, total - first);
          for (j = 0; j < nt; j = j + 1) begin
            $fwrite(fout, "G %0d %0d %0d", t_point[j], t_layer[j], t_n[j]);
            for (i = 0; i < t_n[j]; i = i + 1) $fwrite(fout, " %0d", $signed(words[t_addr[j] - first + i]));
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
