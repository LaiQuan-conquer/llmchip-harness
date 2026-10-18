// Verilator testbench of llmchip_core. Plays the host: runs the generation
// loop of the numeric spec (prompt tokens forced, then greedy tokens; BOS
// stops), records every observation-point stream with its cycle, and writes a
// binary log that the simulation plugin compares with L2 / the ISA simulator.
//
// usage: Vllmchip_core [+prog=FILE] [+cov=FILE] STIMULUS OUTPUT
// stimulus lines:
//   RUN <id> <steps> <reset_cycles> <n> <t0> ... <tn-1>
//       reset_cycles > 0: during the step at position 1, reset after that many
//       cycles, then restart the run from position 0 (directed reset test)
//   ILLEGAL <id> <token> <pos>  a step with an illegal input (position beyond
//       the context or token beyond the vocabulary); must end with `error`
// waveform (builds with --trace): +vcd=FILE +vcd_pos=P +vcd_from=C0 +vcd_to=C1
//   dumps cycles C0..C1 (relative to the step start) of the step at position P
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "Vllmchip_core.h"
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

static Vllmchip_core* top;
static uint64_t cycle = 0;
static FILE* out;

static void tick() {
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

struct Group {
  int32_t point, layer;
  uint64_t start;
  std::vector<int32_t> values;
};

struct StepResult {
  int32_t token_out = 0, error = 0, timeout = 0;
  uint64_t cycles = 0;
  std::vector<Group> groups;
};

// Runs one step. reset_after > 0 asserts reset after that many cycles and returns early.
static StepResult step(uint32_t token, uint32_t pos, uint64_t reset_after) {
  StepResult r;
  uint64_t begin = cycle;
  top->token_in = token;
  top->pos_in = pos;
  top->start = 1;
  tick();
  top->start = 0;
  const uint64_t limit = 400000000ULL;
  while (!top->done) {
    uint64_t n = cycle - begin;
#if VM_TRACE
    vcd_on = vcd && (int64_t)pos == vcd_pos && n >= vcd_from && n <= vcd_to;
#endif
    // A start pulse with different inputs while busy must be ignored.
    if (n == 10) {
      top->start = 1;
      top->token_in = token ^ 1;
      top->pos_in = pos + 1;
    }
    if (n == 11) {
      top->start = 0;
      top->token_in = token;
      top->pos_in = pos;
    }
    if (reset_after && n == reset_after) {
      top->rst_n = 0;
      tick();
      tick();
      top->rst_n = 1;
      tick();
      r.timeout = -1;
      return r;
    }
    if (n > limit) {
      r.timeout = 1;
      break;
    }
    if (top->obs_valid) {
      if (r.groups.empty() || r.groups.back().point != top->obs_point || r.groups.back().layer != (int32_t)top->obs_layer || top->obs_index == 0) {
        r.groups.push_back({(int32_t)top->obs_point, (int32_t)top->obs_layer, cycle - begin, {}});
      }
      r.groups.back().values.push_back((int32_t)top->obs_data);
    }
    tick();
  }
#if VM_TRACE
  vcd_on = false;
#endif
  r.token_out = (int32_t)top->token_out;
  r.error = top->error;
  r.cycles = cycle - begin;
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
  if (args.size() != 2) {
    fprintf(stderr, "usage: Vllmchip_core [+prog=FILE] STIMULUS OUTPUT\n");
    return 2;
  }
  top = new Vllmchip_core;
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
  out = fopen(args[1].c_str(), "wb");
  top->rst_n = 0;
  top->start = 0;
  tick();
  tick();
  top->rst_n = 1;
  tick();

  std::ifstream in(args[0]);
  std::string line;
  while (std::getline(in, line)) {
    std::istringstream ss(line);
    std::string kind;
    ss >> kind;
    if (kind == "RUN") {
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
