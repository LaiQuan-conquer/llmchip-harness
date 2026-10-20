// Functional models of the gf180mcu_fd_io cells for gate-level simulation (L4):
// input and output buffers, supplies, corners and fillers without behavior.
`timescale 1ns/1ps
module gf180mcu_fd_io__in_c (input PAD, output Y, input PU, input PD);
  assign Y = PAD;
endmodule
module gf180mcu_fd_io__bi_t (inout PAD, input A, input OE, input IE, input CS, input SL, input PU, input PD, input PDRV0, input PDRV1, output Y);
  // Off-chip side: driven while enabled, released (high impedance) otherwise.
  assign PAD = OE ? A : 1'bz;
  // On-chip side: the pad's value while its input is enabled (bidirectional pins), else low.
  assign Y = IE ? PAD : 1'b0;
endmodule
module gf180mcu_fd_io__dvdd (); endmodule
module gf180mcu_fd_io__dvss (); endmodule
module gf180mcu_fd_io__cor (); endmodule
module gf180mcu_fd_io__fill10 (); endmodule
module gf180mcu_fd_io__fill5 (); endmodule
module gf180mcu_fd_io__fill1 (); endmodule
module gf180mcu_fd_io__fillnc (); endmodule
