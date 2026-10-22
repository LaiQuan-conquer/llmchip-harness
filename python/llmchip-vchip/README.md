# llmchip-vchip: the test harness for virtual chips

A model-independent host that loads any **virtual chip**, feeds it prompts, drives generation, records tokens and cycles, measures quality and speed, and writes a report. It answers "is the model good, and is the design logic right" before tapeout, which silicon cannot answer.

- A **virtual chip** is a model plus a backend that executes it **one step at a time**: given the token at a position, it returns the next token and the cycles the step took ([interface.py](src/llmchip_vchip/interface.py)).
- The **harness** owns everything around the step: tokenizer, generation loop, stop rules, host protocol, metrics, policy and reports. Every backend is driven by the same loop, so reference, L2 and hardware chips are compared on the same terms.

Design: `drafts/virtual_chip_workbench_design_zh.md` (owner draft). It is outside the kernel: it reads chip directories and writes run directories, and does not register artifacts or verdicts with harnessd.

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
| `backend` | `reference`, `numeric`, `process`, or any name with `entrypoint: "module:Class"` (an in-process adapter) |
| `model.context`, `model.vocab` | required; the harness assumes nothing else about the model |
| `tokenizer` | `kind` `llama2c` (tokenizer .bin) or `hf` (tokenizer.json); `stop` token ids end a generation; optional `bos` is prepended when the tokenizer does not add it |
| `artifacts` | named files or directories; all are hashed into every run |
| `clock_mhz` | the clock tok/s is computed at; `null` gives cycles only |
| `host` | `direct` (no frames) or `spi`: START (48 bits) + STATUS (40 bits), plus READ (24 + 32 per word) when `read_words` > 0, each bit `sck_div` clocks; `token_bits`/`pos_bits` (16) must hold the vocabulary and context |
| `capabilities` | `greedy` (required), `cycles`, `logits` (perplexity), `traces` (observation points) |
| `policy` | or `policy.json` beside the manifest; see below |

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

JSONL, one `{"id", "prompt", "expected"?}` per line (`{"passage": "..."}` lines are held-out text for perplexity), or an `llmchip-evalset/1` JSON file (the bundled TinyStories-style and general sets that `vchip new` copies into a chip). A prompt the chip's tokenizer cannot represent (beyond a tiny vocabulary, or an unknown token) ends that prompt with `invalid_input`; it is not fed to the chip.

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

A check is `n/a` when it does not apply to the chip (frames on a direct host, no stop token declared) and `not_run` when it applies but could not be checked (no prompt of the set fits the tokenizer and context, no generation completed). The result is `pass` only without failures and without `not_run`; otherwise `fail` or `incomplete`. T-10, T-11, T-13 and T-14 are covered by `bench` (golden, reference, errors) and by the harness's own tests, not by `conform`.

The harness's own tests ([tests/test_harness.py](tests/test_harness.py)) cover T-01 to T-14 of the design, with fault-injecting process chips ([tests/chips/toy_chip.py](tests/chips/toy_chip.py)) for crashes, hangs, garbage output and wrong tokens.

## Not yet

- No RTL, gate-level or perf backend of its own: hardware comes in through `process`. The frame-level host behavior (START while busy, READ) is the chip's business until a chip exposes its frames.
- The reference and graph-L2 backends recompute the whole sequence each step (no KV cache): fine for small models, slow for long generations on large ones.
- Not connected to the kernel: runs are not artifacts and verdicts are not gate evidence. No HTTP API, no Web UI page, no sampling.
