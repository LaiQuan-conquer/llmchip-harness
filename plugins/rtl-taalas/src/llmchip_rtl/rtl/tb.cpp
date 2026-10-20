// Verilator testbench of the chip (tb_top: chip + SPI SRAM model). Plays the
// host over the host SPI interface: runs the generation loop of the numeric
// spec (prompt tokens forced, then greedy tokens; BOS stops), reads the
// observation points back from their trace buffers in the external memory,
// and writes a binary log that the simulation plugin compares with L2 / the
// instruction-level simulator.
//
// usage: Vsim [+prog=FILE] [+cov=FILE] [+vcd=FILE +vcd_pos=P +vcd_from=C0 +vcd_to=C1] TRACEMAP STIMULUS OUTPUT
// TRACEMAP lines: <op> <point> <layer> <n> <buffer address> <debug>
// stimulus lines:
//   MODE <m>                 mode of the following steps (1: observation points, 3: also debug traces)
//   RUN <id> <steps> <reset_cycles> <n> <t0> ... <tn-1>
//       reset_cycles > 0: during the step at position 1, reset after that many
//       cycles, then restart the run from position 0 (directed reset test)
//   ILLEGAL <id> <token> <pos>  a step with an illegal input; must end with `error`
// Every step also sends a START with a different token and position while
// busy, which the chip must ignore.
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "Vtb_top.h"
#include "verilated.h"
#if VM_COVERAGE
#include "verilated_cov.h"
#endif
#if VM_TRACE
#include "verilated_vcd_c.h"
static VerilatedVcdC* vcd = nullptr;
static int64_t vcd_pos = -1;
static uint64_t vcd_from = 0, vcd_to = 0;
static bool vcd_on = false;
#endif

static Vtb_top* top;
static uint64_t cycle = 0;
static uint64_t busy_cycles = 0;  // cycles with the core busy (what perf.py predicts)
static FILE* out;
static int mode = 1;
static const int H = 4;  // host SPI half period in clock cycles (SCK = clk / 8)

static void tick() {
  if (top->busy) busy_cycles++;
  top->clk = 0;
  top->eval();
#if VM_TRACE
  if (vcd_on) vcd->dump(2 * cycle);
#endif
  top->clk = 1;
  top->eval();
#if VM_TRACE
  if (vcd_on) vcd->dump(2 * cycle + 1);
#endif
  cycle++;
}

static void put(int32_t v) { fwrite(&v, 4, 1, out); }

struct TraceEntry {
  int32_t op, point, layer, n, addr, debug;
};
static std::vector<TraceEntry> traces;

// One host SPI frame; returns the bits read on MISO.
static uint64_t frame(const std::vector<int>& bits) {
  uint64_t in = 0;
  top->host_cs_n = 0;
  for (int k = 0; k < H; k++) tick();
  for (int b : bits) {
    top->host_sclk = 0;
    top->host_mosi = b;
    for (int k = 0; k < H; k++) tick();
    in = (in << 1) | (top->host_miso & 1);
    top->host_sclk = 1;
    for (int k = 0; k < H; k++) tick();
  }
  top->host_sclk = 0;
  for (int k = 0; k < H; k++) tick();
  top->host_cs_n = 1;
  for (int k = 0; k < 2 * H; k++) tick();
  return in;
}

static std::vector<int> bits_of(uint64_t v, int n) {
  std::vector<int> b;
  for (int k = n - 1; k >= 0; k--) b.push_back((v >> k) & 1);
  return b;
}

static void host_start(uint32_t token, uint32_t pos, int m) {
  frame(bits_of((0x10ULL << 40) | ((uint64_t)(token & 0xffff) << 24) | ((uint64_t)(pos & 0xffff) << 8) | (m & 0xff), 48));
}

static uint32_t host_status() { return (uint32_t)frame(bits_of(0x20ULL << 32, 40)); }

static int32_t backdoor(uint32_t addr) {
  top->bd_addr = addr;
  top->eval();
  return (int32_t)top->bd_data;
}

struct Group {
  int32_t point, layer;
  uint64_t start;
  std::vector<int32_t> values;
};

struct StepResult {
  int32_t token_out = 0, error = 0, timeout = 0;
  uint64_t cycles = 0, busy = 0;
  std::vector<Group> groups;
};

static void reset_chip() {
  top->rst_n = 0;
  for (int k = 0; k < 4; k++) tick();
  top->rst_n = 1;
  for (int k = 0; k < 4; k++) tick();
}

// Runs one step. reset_after > 0 resets the chip after that many cycles and returns early.
static StepResult step(uint32_t token, uint32_t pos, uint64_t reset_after) {
  StepResult r;
  uint64_t begin = cycle, busy_begin = busy_cycles;
  std::map<int32_t, uint64_t> starts;  // TRACE op -> cycle its copy started
  host_start(token, pos, mode);
  for (int k = 0; k < 32 && !top->busy; k++) tick();
  const uint64_t limit = 4000000000ULL;
  bool busy_start_sent = false;
  while (top->busy) {
    uint64_t n = cycle - begin;
#if VM_TRACE
    vcd_on = vcd && (int64_t)pos == vcd_pos && n >= vcd_from && n <= vcd_to;
#endif
    if (reset_after && n >= reset_after) {
      reset_chip();
      r.timeout = -1;
      return r;
    }
    if (!busy_start_sent && n >= 1000) {
      // A START with a different token and position while busy must be ignored.
      host_start(token ^ 1, pos + 1, mode);
      busy_start_sent = true;
      continue;
    }
    if (n > limit) {
      r.timeout = 1;
      break;
    }
    if (top->trace_start) starts[(int32_t)top->pc] = n;
    tick();
  }
#if VM_TRACE
  vcd_on = false;
#endif
  uint32_t status = host_status();
  r.token_out = (int32_t)(status & 0xffff);
  r.error = (status >> 30) & 1;
  r.cycles = cycle - begin;
  r.busy = busy_cycles - busy_begin;
  if (!r.error && !r.timeout) {
    for (const auto& e : traces) {
      if (!(e.debug ? (mode & 2) : (mode & 1))) continue;
      Group g{e.point, e.layer, starts.count(e.op) ? starts[e.op] : 0, {}};
      for (int32_t k = 0; k < e.n; k++) g.values.push_back(backdoor(e.addr + k));
      r.groups.push_back(g);
    }
  }
  return r;
}

static void record(int32_t id, int32_t pos, int32_t token_in, int32_t next, const StepResult& r) {
  put(0x53544550);
  put(id);
  put(pos);
  put(token_in);
  put(r.token_out);
  put(next);
  put((int32_t)r.cycles);
  put(r.error);
  put(r.timeout);
  put((int32_t)r.busy);
  put((int32_t)r.groups.size());
  for (const auto& g : r.groups) {
    put(g.point);
    put(g.layer);
    put((int32_t)g.values.size());
    put((int32_t)g.start);
    for (int32_t v : g.values) put(v);
  }
}

int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  std::vector<std::string> args;
  for (int i = 1; i < argc; i++)
    if (argv[i][0] != '+') args.push_back(argv[i]);
  if (args.size() != 3) {
    fprintf(stderr, "usage: Vsim [+prog=FILE] [+cov=FILE] TRACEMAP STIMULUS OUTPUT\n");
    return 2;
  }
  {
    std::ifstream tm(args[0]);
    TraceEntry e;
    while (tm >> e.op >> e.point >> e.layer >> e.n >> e.addr >> e.debug) traces.push_back(e);
  }
  top = new Vtb_top;
#if VM_TRACE
  if (const char* file = Verilated::commandArgsPlusMatch("vcd=")) {
    std::string arg(file);
    if (arg.size() > 5) {
      auto plus = [](const char* key) -> uint64_t {
        std::string m = Verilated::commandArgsPlusMatch(key);
        return m.empty() ? 0 : std::stoull(m.substr(m.find('=') + 1));
      };
      vcd_pos = (int64_t)plus("vcd_pos=");
      vcd_from = plus("vcd_from=");
      vcd_to = plus("vcd_to=");
      Verilated::traceEverOn(true);
      vcd = new VerilatedVcdC;
      top->trace(vcd, 99);
      vcd->open(arg.substr(5).c_str());
    }
  }
#endif
  out = fopen(args[2].c_str(), "wb");
  top->host_cs_n = 1;
  top->host_sclk = 0;
  top->host_mosi = 0;
  top->bd_addr = 0;
  reset_chip();

  std::ifstream in(args[1]);
  std::string line;
  while (std::getline(in, line)) {
    std::istringstream ss(line);
    std::string kind;
    ss >> kind;
    if (kind == "MODE") {
      ss >> mode;
    } else if (kind == "RUN") {
      int32_t id, steps, n;
      uint64_t reset_cycles;
      ss >> id >> steps >> reset_cycles >> n;
      std::vector<int32_t> prompt(n);
      for (auto& t : prompt) ss >> t;
      if (reset_cycles) {
        // Interrupted attempt: positions 0 and 1, reset in the middle of position 1.
        step(prompt[0], 0, 0);
        step(n > 1 ? prompt[1] : prompt[0], 1, reset_cycles);
      }
      int32_t token = prompt[0];
      for (int32_t pos = 0; pos < steps; pos++) {
        StepResult r = step(token, pos, 0);
        int32_t next = pos < n - 1 ? prompt[pos + 1] : r.token_out;
        record(id, pos, token, next, r);
        if (r.error || r.timeout) break;
        if (pos >= n - 1 && next == 1) break;
        token = next;
      }
    } else if (kind == "ILLEGAL") {
      int32_t id, token, pos;
      ss >> id >> token >> pos;
      StepResult r = step(token, pos, 0);
      record(id, pos, token, -1, r);
    }
  }
  fclose(out);
#if VM_COVERAGE
  std::string cov = Verilated::commandArgsPlusMatch("cov=");
  Verilated::threadContextp()->coveragep()->write(cov.size() > 5 ? cov.substr(5).c_str() : "coverage.dat");
#endif
#if VM_TRACE
  if (vcd) vcd->close();
#endif
  top->final();
  delete top;
  return 0;
}
