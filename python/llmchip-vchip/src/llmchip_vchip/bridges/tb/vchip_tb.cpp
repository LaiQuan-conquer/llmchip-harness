// Pin-level host testbench of the virtual-chip test harness (llmchip-vchip), for chips with the
// Taalas-like host interface: an SPI slave (mode 0, SCK = clk / 8) on host_sclk / host_cs_n /
// host_mosi / host_miso, and a busy output pin. The testbench owns the measurement: it counts
// the clock cycles itself, on the pins; nothing is taken from the chip under test.
//
//   START  0x10 token[15:0] pos[15:0] mode[7:0]                 (48 bits)
//   STATUS 0x20 -> busy, error, done, 13'b0, token[15:0]           (40 bits)
//   READ   0x30 addr[15:0] -> 32 bits per observation word         (24 + 32 n bits)
//
// Commands, one per line on stdin; one answer line each on stdout:
//   RESET                    -> OK <cycles until the chip reports idle> <timeout>
//   STEP <token> <pos> <inj> -> STEP <token_out> <error> <busy> <total> <polls> <injected> <timeout> <stale>
//        START, then STATUS polled until done or error. inj = 1 sends a second START with a
//        different token and position right after the first (it must be ignored).
//        busy: cycles with the busy pin high; total: cycles from the START frame's CS fall to
//        the end of the last STATUS frame (the token's time at the host); injected: 1 only if
//        the busy pin was high when the extra START's CS rose (the edge the chip acts on);
//        stale: the chip reported done although its busy pin never rose, or STATUS said done
//        while the busy pin was still high (STATUS and the pin disagree).
//   READ <addr> <n>          -> WORDS <cycles> <w0> ... <wn-1>   (only while idle)
//   QUIT
#include <cstdint>
#include <cstdio>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "Vtb_top.h"
#include "verilated.h"

static Vtb_top* top;
static uint64_t cycle = 0;
static uint64_t busy_cycles = 0;
static const int H = 4;  // SCK half period in clock cycles (SCK = clk / 8)
static const uint64_t STEP_LIMIT = 50000000ULL;

static void tick() {
  if (top->busy) busy_cycles++;
  top->clk = 0;
  top->eval();
  top->clk = 1;
  top->eval();
  cycle++;
}

// One frame: CS low, the bits MSB first (MOSI changes while SCK is low, MISO sampled at the
// rising edge), CS high. Returns the bits read on MISO.
static bool busy_at_cs_rise = false;  // the busy pin when the last frame's CS rose

static uint64_t frame(const std::vector<int>& bits, std::vector<uint32_t>* words = nullptr, int skip = 0) {
  uint64_t in = 0;
  top->host_cs_n = 0;
  for (int k = 0; k < H; k++) tick();
  for (size_t b = 0; b < bits.size(); b++) {
    top->host_sclk = 0;
    top->host_mosi = bits[b];
    for (int k = 0; k < H; k++) tick();
    in = (in << 1) | (top->host_miso & 1);
    top->host_sclk = 1;
    for (int k = 0; k < H; k++) tick();
    if (words && (int)b >= skip && ((int)b - skip) % 32 == 31) words->push_back((uint32_t)(in & 0xffffffffULL));
  }
  top->host_sclk = 0;
  for (int k = 0; k < H; k++) tick();
  busy_at_cs_rise = top->busy;
  top->host_cs_n = 1;
  for (int k = 0; k < 2 * H; k++) tick();
  return in;
}

static std::vector<int> bits_of(uint64_t v, int n) {
  std::vector<int> b;
  for (int k = n - 1; k >= 0; k--) b.push_back((v >> k) & 1);
  return b;
}

static void start(uint32_t token, uint32_t pos) {
  frame(bits_of((0x10ULL << 40) | ((uint64_t)(token & 0xffff) << 24) | ((uint64_t)(pos & 0xffff) << 8) | 1, 48));
}

static uint32_t status() { return (uint32_t)frame(bits_of(0x20ULL << 32, 40)); }

static void reset() {
  top->rst_n = 0;
  for (int k = 0; k < 8; k++) tick();
  top->rst_n = 1;
  uint64_t begin = cycle;
  for (int k = 0; k < 16; k++) tick();
  // The chip may initialize its state after reset (busy high); wait until STATUS says idle.
  int timeout = 0;
  while ((status() >> 31) & 1) {
    if (cycle - begin > STEP_LIMIT) {
      timeout = 1;
      break;
    }
  }
  printf("OK %llu %d\n", (unsigned long long)(cycle - begin), timeout);
}

static void step(uint32_t token, uint32_t pos, int inject) {
  uint64_t begin = cycle, busy_begin = busy_cycles;
  int polls = 0, injected = 0, timeout = 0, stale = 0;
  uint32_t st = 0;
  start(token, pos);
  if (inject) {
    start(token ^ 1, (pos + 1) & 0xffff);
    injected = busy_at_cs_rise ? 1 : 0;
  }
  for (;;) {
    st = status();
    polls++;
    bool error = (st >> 30) & 1, done = (st >> 29) & 1;
    if (done || error) {
      // The chip finished: its busy pin must have risen during the step and be low now.
      if (done && (busy_cycles == busy_begin || top->busy)) stale = 1;
      break;
    }
    if (cycle - begin > STEP_LIMIT) {
      timeout = 1;
      break;
    }
  }
  printf("STEP %u %u %llu %llu %d %d %d %d\n", st & 0xffff, (st >> 30) & 1,
         (unsigned long long)(busy_cycles - busy_begin), (unsigned long long)(cycle - begin), polls, injected, timeout,
         stale);
}

static void read(uint32_t addr, int n) {
  uint64_t begin = cycle;
  std::vector<int> bits = bits_of((0x30ULL << 16) | (addr & 0xffff), 24);
  for (int k = 0; k < 32 * n; k++) bits.push_back(0);
  std::vector<uint32_t> words;
  frame(bits, &words, 24);
  printf("WORDS %llu", (unsigned long long)(cycle - begin));
  for (uint32_t w : words) printf(" %d", (int32_t)w);
  printf("\n");
}

int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  top = new Vtb_top;
  top->host_cs_n = 1;
  top->host_sclk = 0;
  top->host_mosi = 0;
  top->rst_n = 1;
  std::string line;
  while (std::getline(std::cin, line)) {
    std::istringstream ss(line);
    std::string cmd;
    ss >> cmd;
    if (cmd == "RESET") {
      reset();
    } else if (cmd == "STEP") {
      uint32_t token, pos;
      int inject = 0;
      ss >> token >> pos >> inject;
      step(token, pos, inject);
    } else if (cmd == "READ") {
      uint32_t addr;
      int n;
      ss >> addr >> n;
      read(addr, n);
    } else if (cmd == "QUIT") {
      break;
    } else {
      printf("ERR unknown command\n");
    }
    fflush(stdout);
  }
  top->final();
  delete top;
  return 0;
}
