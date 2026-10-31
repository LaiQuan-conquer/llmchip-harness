// Pin-level host testbench for a generated taalas-tile/1 chip (llmchip-vchip, B6). The chip's
// host side is parallel, so each STEP drives start/token/length and counts the busy pin.
//   RESET      -> OK
//   STEP <tok> <pos> -> STEP <next_token> <busy_cycles>
//   QUIT
#include <cstdint>
#include <cstdio>
#include <iostream>
#include <string>

#include "Vtb_top.h"
#include "verilated.h"

static Vtb_top* top;
static uint64_t cycle = 0;

static void tick() {
  top->clk = 0;
  top->eval();
  top->clk = 1;
  top->eval();
  cycle++;
}

int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  top = new Vtb_top;
  top->rst_n = 0;
  top->start = 0;
  top->token = 0;
  top->length = 1;
  for (int i = 0; i < 8; i++) tick();
  top->rst_n = 1;
  for (int i = 0; i < 4; i++) tick();
  std::string cmd;
  while (std::cin >> cmd) {
    if (cmd == "RESET") {
      top->rst_n = 0;
      for (int i = 0; i < 8; i++) tick();
      top->rst_n = 1;
      for (int i = 0; i < 4; i++) tick();
      std::cout << "OK" << std::endl;
    } else if (cmd == "STEP") {
      uint32_t token = 0, pos = 0;
      std::cin >> token >> pos;
      top->token = token & 0xffffffu;
      top->position = pos & 0xffffu;
      top->length = 1;
      top->start = 1;
      tick();
      top->start = 0;
      uint64_t busy = 0;
      uint64_t limit = 200000000ULL;
      bool done = false;
      while (limit--) {
        if (top->busy) busy++;
        if (top->done) {
          done = true;
          break;
        }
        tick();
      }
      std::cout << "STEP " << (top->next_token & 0xffffffu) << " " << busy;
      if (!done) std::cout << " TIMEOUT";
      std::cout << std::endl;
    } else if (cmd == "QUIT") {
      break;
    } else {
      std::cout << "ERR " << cmd << std::endl;
    }
  }
  top->final();
  delete top;
  return 0;
}
