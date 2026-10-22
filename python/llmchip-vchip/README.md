# llmchip-vchip: the test harness for virtual chips

A model-independent host that loads any **virtual chip**, feeds it prompts, drives generation, records tokens and cycles, measures quality and speed, and writes a report. It answers "is the model good, and is the design logic right" before tapeout, which silicon cannot answer.

- A **virtual chip** is a model plus a backend that executes it **one step at a time**: given the token at a position, it returns the next token and the cycles the step took ([interface.py](src/llmchip_vchip/interface.py)).
- The **harness** owns everything around the step: tokenizer, generation loop, stop rules, host protocol, metrics, policy and reports. Every backend is driven by the same loop, so reference, L2 and hardware chips are compared on the same terms.

Design: `drafts/virtual_chip_workbench_design_zh.md` (owner draft). On its own (`vchip …`) it is outside the kernel: it reads chip directories, writes run directories and judges by each chip's policy. Inside the kernel it is the bundled plugin `vchip` (below): runs and reports are artifacts, and the flow's checks judge.

## Commands

From the repository root (`yarn vchip …` runs the same thing):

```bash
uv run --frozen vchip new reference --model tests/fixtures/models/stories260K/stories260K.bin --id s260k-ref --context 64
uv run --frozen vchip new numeric   --model tests/fixtures/models/stories260K/stories260K.bin --id s260k-l2  --context 64
uv run --frozen vchip list
uv run --frozen vchip run s260k-l2 --prompt "Once upon a time" --max-tokens 32
uv run --frozen vchip conform s260k-l2
uv run --frozen vchip bench s260k-l2 --reference s260k-ref --golden s260k-l2
uv run --frozen vchip compare s260k-ref s260k-l2
```

Chips are found in `--chips DIR` (repeatable), else `$LLMCHIP_VCHIP_CHIPS`, else `<LLMCHIP_HOME or ~/.llmchip>/vchip/chips`. Runs go to `--out DIR`, else `<LLMCHIP_HOME>/vchip/runs`. `bench`, `compare` and `conform` exit 0 when the verdict is pass (or the chip has no policy), 1 when it is fail or incomplete, 2 on a usage error.

`vchip new` makes the two baseline chips from any model the S1 graph recovery supports (a Hugging Face directory or a llama2.c checkpoint): `reference` (float, the quality baseline) and `numeric` (the L2 integer model of a graph numeric spec built on the spot, int8 by default, the bit-exact golden for hardware made from that spec). The model files are referenced by absolute path, not copied.

## Taalas-like hardware (L3): RTL under a pin-level testbench

```bash
uv run --frozen vchip new rtl --proxy --id spatial-5k     # spatial-5k (rtl), spatial-5k-l2 (golden), spatial-5k-ref
uv run --frozen vchip conform spatial-5k                  # T-01..T-19 on the simulated chip (--quick: without T-18/T-19)
uv run --frozen vchip bench spatial-5k                    # its policy: bit-exact with L2, trusted cycles, > 1000 tok/s
```

`vchip new rtl` quantizes one model (a llama2.c checkpoint, or `--proxy` for the 5K proxy: untrained, fixed seed, its text means nothing) into the S2 numeric spec, generates the `taalas-spatial/1` circuits from it (`llmchip_rtl`, the S4 generator), and writes three chips: the RTL chip, the L2 golden of the same spec, and the float reference, with the RTL chip's policy naming both.

The `rtl` backend builds the bundle's chip function (`design.json` `top` and `sources`) inside the harness's own testbench top ([bridges/tb/vchip_top.v](src/llmchip_vchip/bridges/tb/vchip_top.v): the chip's pins and nothing else; the bundle's own testbench files are not used, so the chip under test cannot shape what is observed) and testbench ([bridges/tb/vchip_tb.cpp](src/llmchip_vchip/bridges/tb/vchip_tb.cpp)) under Verilator in the pinned EDA image (once; cached under `<chip>/build/`), then runs it through the bridge ([bridges/rtl_bridge.py](src/llmchip_vchip/bridges/rtl_bridge.py)), a `process` chip whose simulator runs in its own container without network. The testbench drives only the chip's pins:

- START (48 bits), then STATUS (40 bits) polled until `done` or `error`, then READ of the observation words while idle; SCK = clk / 8;
- `cycles` = clocks with the `busy` pin high; `host_cycles` = the rest of the token's time at the host (START, the STATUS polls), both counted by the testbench and marked `source: testbench`. READ is reported apart (`read_cycles`) unless the host reads every token (`host.read_words` > 0);
- token and position must fit the 16-bit START fields; legality for the model is the chip's error flag;
- a step whose STATUS says done while the busy pin never rose, or is still high, ends with `invalid_output` (STATUS and the pin disagree), so a stale `done` cannot pass for a result.

`cycles` is the chip's own busy pin, counted by the testbench: a chip that dropped busy early would report fewer compute cycles. tok/s does not depend on it: it is the token's whole time at the host, from the START frame to the last STATUS frame. `done` is seen at the next STATUS poll, so up to one poll (336 cycles) of waiting is host time, as it would be for a polling host. The design's own figure is compared separately (`cycles_match_design`); the spatial engine reports no cycle count of its own over the pins, so there is no chip-reported count to compare with.

Measurement is **trusted** only when the backend is one of the harness's testbench bridges, the manifest declares `measurement: {cycles: testbench, host_cycles: testbench, trusted: true}`, and every step was counted there; a chip that claims a testbench is not believed. The counted cycles are also compared with the design's own figure (`model.cycles_per_token`, from the generator): `cycles_match_design`.

Measured on 2026-10-22 (WSL2, 16 threads), 5K proxy, 16 dividers, 8 MHz: Verilator build 8 s; `conform --quick` passes T-01–T-17 in about 2 min, `conform` passes T-01–T-19 (T-06 n/a: the proxy produced no stop token) in 12.8 min with no container left behind; `bench` on all 20 prompts to the end of the 64-token context: bit-exact with L2 (tokens, logits and every observation point of 761 generated and 2,560 compared positions), 1,327 cycles per token at every position (= the design), 753 host cycles, **3,846 tok/s**, measurement trusted, verdict pass, 10.6 min. Next-token agreement with the float reference is 0.73 (the proxy is untrained and ternary).

## Through the kernel: the plugin `vchip` (L4)

[plugins/vchip](../../plugins/vchip) runs the harness on a project's artifacts. A run builds its chips in its workspace (`chips_from_artifacts` in [build.py](src/llmchip_vchip/build.py)): the L2 golden from `numeric.spec` + `numeric.weights`, the float reference from `model.ir` when given, and, when an `rtl.bundle` of exactly that spec and those weights is given, the RTL chip, which is then the chip under test. Handlers:

- `bench`: the evaluation set (`eval.set`) or typed-in `prompts`; outputs `vchip.report` (the report without policy and verdict, plus where the design's cycles and clock came from) and `vchip.outputs` (`outputs.jsonl`); metrics such as `bit_exact`, `tok_s`, `measurement_trusted`, `cycles_match_design`, `next_token_agreement`.
- `conform`: T-01–T-19 (`quick` skips T-18/T-19); outputs `vchip.conformance`; metrics are counts (`checks_failed`, `checks_not_run`, …).

A `chip.spec` (S3) of exactly that design gives the design's cycles and clock. The flow pack judges the runs: `vchip.l3` (RTL, 20 prompts × 64 tokens, bit-exact with L2, trusted cycles equal to the chip spec's, ≥ 1000 tok/s at the chip spec's clock) and `vchip.conformance` (at least 20 prompts; for an RTL chip, the acceptance part at that standard and clock). The Web UI page "Virtual chips" starts both and shows their reports. A cancelled run ends its simulators and removes their containers. A kernel input file has no suffix, so an evaluation set is recognized by its content (`format: llmchip-evalset/1`).

## A chip: `chip.json`

```json
{
  "format": "llmchip-vchip/1",
  "id": "taalas-rom-5k",
  "backend": "process",
  "model": {"family": "llama2c", "params": 5248, "context": 64, "vocab": 35},
  "tokenizer": {"kind": "llama2c", "path": "tokenizer.bin", "stop": [1]},
  "artifacts": {"rtl": "rtl/rtl.bundle"},
  "clock_mhz": 8,
  "host": {"kind": "spi", "sck_div": 8, "read_words": 0},
  "capabilities": ["greedy", "cycles", "traces"],
  "command": ["{python}", "-m", "my_chip.serve", "{chip_dir}"],
  "timeout_s": 60,
  "prompts": "prompts.jsonl",
  "policy": {"golden": "taalas-rom-5k-l2", "quality": {"bit_exact": true}, "speed": {"tok_s_min": 1000}}
}
```

| Field | Meaning |
|---|---|
| `backend` | `reference`, `numeric`, `process`, `rtl`, or any name with `entrypoint: "module:Class"` (an in-process adapter) |
| `model.context`, `model.vocab` | required; the harness assumes nothing else about the model |
| `tokenizer` | `kind` `llama2c` (tokenizer .bin) or `hf` (tokenizer.json); `stop` token ids end a generation; optional `bos` is prepended when the tokenizer does not add it |
| `artifacts` | named files or directories; all are hashed into every run |
| `clock_mhz` | the clock tok/s is computed at; `null` gives cycles only |
| `host` | `direct` (no frames) or `spi`: START (48 bits) + STATUS (40 bits), plus READ (24 + 32 per word) when `read_words` > 0, each bit `sck_div` clocks; `token_bits`/`pos_bits` (16) must hold the vocabulary and context |
| `capabilities` | `greedy` (required), `cycles`, `logits` (perplexity), `traces` (observation points) |
| `policy` | or `policy.json` beside the manifest; see below |
| `measurement` | `cycles`, `host_cycles`: `testbench`, `chip` or `model`; `trusted`; `testbench` (a description). L3 chips declare testbench |
| `simulator` | `rtl` backend: `image` pinned by digest, `tool` |

An invalid manifest makes the chip `unavailable` with its reasons (`vchip list`); other chips still load.

## The `process` backend: how hardware plugs in

An RTL or gate-level simulation, a cycle model, an FPGA bridge or a container runs as a child process and answers JSON lines on stdin/stdout ([adapters/process.py](src/llmchip_vchip/adapters/process.py)):

```text
{"op": "hello"}                      -> {"ok": true, "protocol": "llmchip-vchip-process/1"}
{"op": "reset"}                      -> {"ok": true}
{"op": "prefill", "tokens": [...]}   -> {"ok": true, "results": [<step>, ...]}
{"op": "step", "token": t, "pos": p} -> {"ok": true, "token": n, "cycles": c, "host_cycles"?: h, "logits"?: [...], "traces"?: {...}}
failure                              -> {"ok": false, "error": {"code": "invalid_input", "message": "..."}}
```

A Python chip only builds a `VirtualChip` and calls `llmchip_vchip.serve.serve(chip)`. A request without an answer within `timeout_s` kills the process (`timeout`); an exit or a line that is not a response is a `crash`. The next `reset` starts a new process, so one failed prompt does not stop a benchmark; nothing is retried. If the chip simulates the host frames itself it returns `host_cycles`, and the harness uses them instead of its frame model. The frame model counts one START and one STATUS per step (a host that knows when the step ends); STATUS polls while the chip is busy are not counted, so a chip whose host must poll should report its measured `host_cycles`.

A timeout kills the chip's whole process group (the process runs in a session of its own), so a simulator started through a shell or a container wrapper does not survive as an orphan. Writes to the chip share the request's deadline.

## Prompt sets

JSONL, one `{"id", "prompt", "expected"?}` per line (`{"passage": "..."}` lines are held-out text for perplexity), or an `llmchip-evalset/1` JSON file, recognized by its content (the bundled TinyStories-style and general sets that `vchip new` copies into a chip). A prompt the chip's tokenizer cannot represent (beyond a tiny vocabulary, or an unknown token) ends that prompt with `invalid_input`; it is not fed to the chip.

## Generation

Greedy only (argmax, lowest id on ties). Positions 0..n-2 of the prompt are prefilled, the last prompt token is stepped at n-1, and each output is fed back until `max_tokens`, a stop token (recorded, not part of the text) or the end of the context. Errors end the prompt with a code: `invalid_input`, `context_overflow`, `invalid_output`, `timeout`, `crash`, `unsupported`.

## Metrics and policy

| Quality | |
|---|---|
| `next_token_agreement` | the chip is fed the reference chip's generated sequence; share of positions where its prediction equals the reference's token |
| `sequence_match`, `first_divergence` | free-running generations identical to the reference's, and where they first differ |
| `bit_exact` | against the golden chip: tokens, and logits and traces where both return them, identical (teacher-forced and free-running) |
| `perplexity`, `reference_perplexity` | over the prompt set's passages, from the chip's logits (`logits` capability) |
| `repetition_rate`, `length_mean`, `stop_reasons`, `expected_match`, `errors` | from the chip's own generations |

| Speed | |
|---|---|
| `cycles_per_token_*` | decode steps: median, mean, min, max, first, last |
| `host_cycles_per_token`, `host_cycles_source` | per token; `model` (frame sizes) or `chip` (measured) |
| `tok_s`, `tok_s_compute_only` | `clock / mean(compute + host)`, and without the host |
| `latency_ms_p50/p95`, `time_to_first_token_ms_median` | at the declared clock |

Everything is simulation (`simulated: true`). A metric that was not measured is `null` with its reason under `missing`, never 0: a chip that declares `cycles` but reports 0 for a step gets no speed at all, and on a direct host `host_cycles_per_token` is `null` (tok/s is then compute only). A reference with a longer context than the chip is replayed within the chip's context.

The policy holds the chip's own thresholds; the harness sets none. Rules are `<metric>_min`, `<metric>_max` or `<metric>` (exact), under `quality` or `speed`; `reference` and `golden` name the chips to compare with; `max_tokens` the default length. Each rule is pass, fail or n/a; the run is `pass`, `fail`, `incomplete` (no failure, something not measured) or `no_policy`. One rule always applies: any error fails the run, whether in a generation, while the chip replays a comparison chip's sequence, or on a perplexity passage.

## Conformance (`vchip conform`)

| Check | |
|---|---|
| T-01 | manifest valid, chip opens |
| T-02 | tokenizer round trip on the prompt set |
| T-03 | reset and prefill return one valid result per token |
| T-04 | greedy generation; teacher-forced prefill predicts the same tokens as stepping |
| T-05 | a full context prefills; a step at position = context is `invalid_input`; a longer prompt is `context_overflow`; a full prompt generates at most one token |
| T-06 | generation ends at a stop token (n/a if none was produced) |
| T-07 | out-of-range token and position are `invalid_input`, and the chip generates the same tokens afterwards |
| T-08 | SPI host: every step accounts at least the START + STATUS frame cycles (n/a on a direct host) |
| T-09 | two runs give identical tokens and cycles |
| T-12 | every step reports cycles, when the chip declares `cycles` |
| T-15 | the hardware bridge loads, resets, prefills and steps, and reports testbench measurement (n/a for other backends) |
| T-16 | trusted cycles: testbench-counted, declared, and equal to the design's figure (n/a without a `measurement` block) |
| T-17 | the host interface on the pins, checked by the testbench bridge (a chip's report about itself is not accepted): a START sent while the busy pin is high is ignored (same token, cycles and observation words), STATUS polled until done, READ while idle repeatable and ending with the step's token, an illegal token and an illegal position flagged, a legal step afterwards without reset; and the observation words read on the pins equal the golden's. Fails for a chip that declares trusted measurement without a testbench bridge |
| T-18 | the policy's acceptance run: bit-exact with the golden on the whole prompt set at the policy's length, no errors (n/a without a bridge or golden; `not_run` with `--quick`) |
| T-19 | trusted speed: measurement trusted and tok/s at least the policy's `tok_s_min` (1000 by default) |

A check is `n/a` when it does not apply to the chip (frames on a direct host, no stop token declared) and `not_run` when it applies but could not be checked (no prompt of the set fits the tokenizer and context, no generation completed). The result is `pass` only without failures and without `not_run`; otherwise `fail` or `incomplete`. T-10, T-11, T-13 and T-14 are covered by `bench` (golden, reference, errors) and by the harness's own tests, not by `conform`.

The harness's own tests ([tests/test_harness.py](tests/test_harness.py)) cover T-01 to T-14 of the design, with fault-injecting process chips ([tests/chips/toy_chip.py](tests/chips/toy_chip.py)) for crashes, hangs, garbage output and wrong tokens.

## Not yet

- One RTL generator: `taalas-spatial/1`. `taalas-rom/1` gets an RTL chip once its generator exists. No gate-level backend yet (T-20, Icarus + SDF on the routed netlist: L3+), no perf backend.
- `tests/test_rtl_vchip.py` runs the simulator only with the EDA image (skipped visibly otherwise) and benches 4 prompts; the full 20-prompt acceptance (T-18) runs with `LLMCHIP_VCHIP_L3=1`.
- The reference and graph-L2 backends recompute the whole sequence each step (no KV cache): fine for small models, slow for long generations on large ones.
- Through the kernel, only chips of an `llmchip-numeric-spec/1` spec (S2 of a llama2.c model) and `taalas-spatial/1` bundles; the checks `vchip.l3` and `vchip.conformance` belong to no gate. No separate HTTP API (the kernel API is the API), no sampling.
