# Current Implementation and Extension Guide

English | [中文](implementation.zh.md)

| Item | Content |
|---|---|
| Document version | v0.5 |
| Date | 2026-10-30 |
| Describes | 2026-10-30, the flow-pack wiring fix (commit `7232a39`), and the decision-record convention of commit `f0a4945` (`docs/adr/`, ADR-012, AGENTS.md "Decision records"): `spec.design` is judged on its own `verify_spec` run (the tile design node carries no check), and the tile's equivalence check is `rtl.tile.equivalence`; the pack loader refuses a pack whose node judges another handler's check, and registers no check of a refused pack. Before it, R2b: the SPEC step of Qwen2.5-0.5B-Instruct: the fast L2 (GPU, bit-identical to the reference L2) and the KV cache, calibrated (GPTQ) quantization, the diagnosis of wrong answers, open benchmarks (quick set and full set) and the six-step flow pack `flow-six`; R1 (commit `11d0dcd`): stage pipelines (every step's "run development" and "run all tests"), CPU/memory/GPU scheduling, run checkpoints and resumption; R0 (commit `d771bb9`, owner decision 2026-10-24): approval gates, waivers, export and `packager` removed, the flow is S0–S7; before it, commit `9434f86`: M4's `9cc76a0`, plus the flow page, `yarn demo`, model import and graph recovery, the numeric schemes of S2, the exact speed and microarchitecture options of S3, the spatial engine `taalas-spatial/1` through S5, and the virtual-chip test harness `llmchip-vchip`, standalone and as the kernel plugin `vchip` with its Web UI page (design L4) |
| Upstream documents | [Requirement Analysis](<../Coding Harness MVP Requirement Analysis.md>) v0.7, [Microkernel Architecture](<../Microkernel Architecture.md>) v0.12 |

This document says **what the code does today and how to extend it**. It is descriptive, not authoritative. What the product must do is set by the requirements, and how the system is meant to be organized is set by the architecture. If this guide and the code disagree, the code is the fact and this guide is out of date: fix the guide. Between the two language versions of this guide, the Chinese one is authoritative, as for every key document. If the code and an upstream document disagree, that is a conflict. Record it in §7.3 and report it (AGENTS.md, "Authoritative documents"). Do not resolve it by editing either side alone.

Source references name a file and a symbol (`Scheduler.execute` in [runs/scheduler.ts](../packages/kernel/src/runs/scheduler.ts)), not line numbers, so they survive edits.

---

## 1. Current implementation scope

### 1.1 Milestones

| Milestone (requirements §11.4) | Commit | State in code | How it is proven |
|---|---|---|---|
| M1a kernel foundation | `826168b` | Done | `packages/kernel/tests/*`, `tests/kernel-api.spec.ts`, `tests/recovery.spec.ts` |
| M1b execution and verdicts | `62e7a88` | Done | `tests/runs.spec.ts`, `tests/workflow.spec.ts`, `tests/estimator.spec.ts` |
| M1c agents and security | `427d16e` | Done; agents run only inside the kernel | `tests/agents.spec.ts`, `tests/mcp.spec.ts`, `tests/providers.spec.ts` |
| M1d Web UI part | `851590d` | Browser UI only; no Tauri shell, installer or onboarding | `tests/web-ui.spec.ts`, `tests/ui-latency.spec.ts` |
| M1.5 storage calibration | `3691b26` | Done for stdcell ROM and SRAM 512×8; OpenRAM ends as unusable | `tests/calibration.spec.ts` (needs the EDA image) |
| M2 model to spec (S1–S3) | `9284a99` | Done on stories260K and the proxy model; no trained reference model | `tests/m2-flow.spec.ts` |
| M3 RTL and verification (S4–S5) | `3842361` | Done on the proxy model | `tests/m3-flow.spec.ts` (needs the EDA image) |
| M4 physical and signoff (S6–S7) | `9cc76a0` | Done on the proxy model, 1.2×2 mm die; its S8 tapeout package was removed with R0 | `tests/m4-flow.spec.ts` (opt-in, 39 min measured) |
| M5 migration to the tapeout process, M6 post-silicon | — | Deleted (owner decision 2026-10-24: no tapeout). The migration impact assessment (FR-MIG) still works | `tests/m4-kernel.spec.ts`, `plugins/estimator/tests/test_migration.py` |
| R0 approval gates and waivers removed | `d771bb9` | Done: no gates, snapshots, approvals, release, waivers or export; `packager` deleted; requirements v0.7, architecture v0.11 | `tests/workflow.spec.ts`, `tests/m4-kernel.spec.ts`, the flow tests |
| R1 pipelines and resources | `11d0dcd` | Done: stage pipelines (`pipelines.*`, CLI `pipeline`, the two entries and the pipeline panel of the Flow page), `flow-default` declares the development pipeline and the required tests of S0–S7; CPU, memory and GPU scheduling; run checkpoints and resumption (IF-2 `checkpoint`, input `resume`); architecture v0.12 | `tests/pipelines.spec.ts`, `tests/pipelines-flow.spec.ts`, `tests/resources.spec.ts`, `tests/checkpoint.spec.ts` |
| R2a fast L2 and benchmarks | this commit | Partly done (the first part of R2): a KV cache for the graph L2; the fast L2 on the GPU (`TorchL2`), bit-identical to the reference L2; the quick set and the full set of seven open benchmarks on L0 and L2, code answers judged in a container without network; paired-bootstrap comparison; the SPEC step of the six-step flow pack `flow-six` (development pipeline and required tests). The numeric spec is provisionally W4 by plain rounding (RTN); calibrated quantization, KV int8, diagnosis of wrong answers, `taalas-tile/1` and the sky130/IHP profiles follow in later parts of R2 | `plugins/numeric/tests/test_l2_torch.py`, `test_bench.py`, `test_benchcode.py`, `tests/qwen-spec.spec.ts` (opt-in) |

So M1 as a whole is **not** accepted: AC-M1-01 (installer) and the desktop shell are missing. See §7.

### 1.2 Implemented

| Area | What works | Entry point |
|---|---|---|
| Kernel daemon | Start, single-owner lock, recovery, ordered shutdown | `harnessd serve`, [kernel.ts](../packages/kernel/src/kernel.ts) `Kernel.start` |
| K1 event store | Append-only SQLite log with a hash chain, projections, replay | [events/store.ts](../packages/kernel/src/events/store.ts) |
| K2 artifact store | Content-addressed, read-only objects, lineage, label propagation | [artifacts/store.ts](../packages/kernel/src/artifacts/store.ts) |
| K3/K6 workflow | Projects, slots, branches, stages (admission checks only), verdicts, migration records | [workflow/engine.ts](../packages/kernel/src/workflow/engine.ts) |
| K3 pipelines | Every step's development pipeline and required tests: dependencies, failures and skips, reuse, cancel, resume after the fix, continuing after a kernel restart | [workflow/pipelines.ts](../packages/kernel/src/workflow/pipelines.ts) |
| K4 scheduler | Persistent queue, CPU/memory/GPU scheduling, process and container execution, timeouts, cancellation, retries (resuming from checkpoints), pause, run checkpoints | [runs/scheduler.ts](../packages/kernel/src/runs/scheduler.ts) |
| K5 API policy | Owner token vs external token; method allow-list | [security/policy.ts](../packages/kernel/src/security/policy.ts) |
| K8 egress | Labels, destination limits, NDA fingerprints, declassification, audit | [security/egress.ts](../packages/kernel/src/security/egress.ts) |
| K9 plugin registry | Manifest scan, validation, data import | [plugins/registry.ts](../packages/kernel/src/plugins/registry.ts) |
| K10 config and secrets | Layered config, AES-256-GCM secret file, redaction | [config/](../packages/kernel/src/config/) |
| Agent runtime | Managed agents with tool checks, change sets, handoff, loop and budget stops | [agents/runtime.ts](../packages/kernel/src/agents/runtime.ts), [agents/providers.ts](../packages/kernel/src/agents/providers.ts) |
| IF-1 kernel API | JSON-RPC 2.0 over WebSocket `/v1/rpc`, HTTP `/v1/artifacts` | [server.ts](../packages/kernel/src/server.ts), [client](../packages/client/src/index.ts) |
| IF-2 plugin protocol | JSON-RPC 2.0, one message per line, over stdio; notifications `progress`, `log`, `checkpoint` | [plugins/process.ts](../packages/kernel/src/plugins/process.ts), SDKs |
| IF-3 tool containers | Podman, no network, read-only inputs and plugin directory | `Scheduler.containerCommand` |
| CLI and MCP | `llmchip` commands (including `pipeline plan/start/list/show/cancel/resume`), generic `call`, MCP on stdio | [cli/src/main.ts](../packages/cli/src/main.ts), [cli/src/mcp.ts](../packages/cli/src/mcp.ts) |
| Web UI | Nine pages, Chinese and English. The default page, Flow, shows a project's S0–S7 path with results and checks in plain language; all stages and every stage have two entries, "run development" and "run all tests", and the pipeline panel lists each pipeline's nodes, runs, verdicts and reasons | [apps/web/src](../apps/web/src) |
| Domain flow | S0–S7 with bundled plugins, each stage judged by its checks (table in §2.4) | [plugins/](../plugins) |
| Model quality (open benchmarks) | The quick set (900 questions, fixed seed 20261024) and the full set of seven open benchmarks (MMLU, CMMLU, C-Eval, GSM8K, IFEval, HumanEval+, MBPP+), answered by L0 (transformers, bf16, GPU) and L2 (the fast L2) and judged by each benchmark's rule; code runs only in a read-only container without network; per benchmark, the drop of L2 against L0 with its paired-bootstrap 95% interval, judged by one check per benchmark | [bench.py](../plugins/numeric/src/llmchip_numeric/bench.py), [l2_torch.py](../plugins/numeric/src/llmchip_numeric/l2_torch.py), [flow-six](../plugins/flow-six/flow.json) |
| Real model import and graph recovery | A downloaded model directory (safetensors + config.json + tokenizer.json, or llama2.c) is imported by local path, censused from its headers, estimated, recovered into a computation graph (Llama family: Llama, Mistral, SmolLM, Qwen2, Qwen3, llama2.c), executed as L0 and tested item by item against transformers / the run.c port: weights bit for bit, configuration, parameter count, every layer's hidden state, logits, top-1 and greedy generation per prompt, single-token and long-sequence boundary cases. A project's own llama2.c weights (e.g. the proxy) are imported from its artifacts. The S1 page picks any imported model or a new path, takes custom prompts, and shows every test item | [model-llama/src/llmchip_model/pipeline.py](../plugins/model-llama/src/llmchip_model/pipeline.py) |

### 1.3 Interface only (declared, no behavior)

- Plugin kinds `ui-panel`, `llm-provider` and `parser` in `PluginKind` ([kernel.schema.json](../packages/kernel-api/schemas/kernel.schema.json)). No plugin declares them. LLM providers are kernel code, not plugins.
- Check `sdf.timing` in the default flow. It is defined, but no flow step uses it. It requires the capability `sdf-timing-sim`, which no plugin declares, so it can only judge `indeterminate`.
- The `formal` field of the verification report (sim-verilator `verify`). It is always empty (FR-VER-07).
- `PluginHost` for in-kernel Cordis plugins ([plugin-host.ts](../packages/kernel/src/plugin-host.ts)). It works and is tested, but only `KernelOptions.plugins` loads anything into it, and only tests pass that. No product plugin runs in-kernel (see C5 in §7.3).

### 1.4 Not implemented

The Tauri desktop shell, installer, dedicated WSL distro, first-launch onboarding and automatic image pulls. Also: CHARTER.md generation, the sample project, project export and import, the project git working directory (architecture §6.3), model training (FR-QNT-05, P2), graph recovery outside the Llama family and of GGUF, and `images/`. §7.1 lists them with requirement IDs. Owner decisions are kept in `docs/adr/` (ADR-012 is the latest), which is local and not committed (AGENTS.md, "Decision records").

---

## 2. Modules, responsibilities and dependencies

### 2.1 Packages and entry points

| Module | Entry | Responsibility | May depend on |
|---|---|---|---|
| `@llmchip/kernel-api` | [src/index.ts](../packages/kernel-api/src/index.ts), [schemas/kernel.schema.json](../packages/kernel-api/schemas/kernel.schema.json) | Contracts: method table, event kinds, plugin protocol, records. The schema is the source of truth; `src/generated/` is generated | nothing at run time (`ajv` for its tests). **Never Cordis** (AP-6) |
| `@llmchip/kernel` | `harnessd` → [src/cli.ts](../packages/kernel/src/cli.ts) → [src/main.ts](../packages/kernel/src/main.ts) → `Kernel.start` | The daemon: K1–K10, agent runtime, API server | `kernel-api`, `cordis` (only in `plugin-host.ts`), `ws`, `ajv` |
| `@llmchip/client` | [src/index.ts](../packages/client/src/index.ts) `KernelClient` | IF-1 client for Node.js and browsers | `kernel-api` |
| `@llmchip/cli` | `llmchip` → [src/bin.ts](../packages/cli/src/bin.ts) → [src/main.ts](../packages/cli/src/main.ts) | Command line, `llmchip ui`, `llmchip mcp` | `client`, `kernel-api` |
| `@llmchip/plugin-sdk` | [src/index.ts](../packages/plugin-sdk/src/index.ts) `runPlugin` | IF-2 SDK for TypeScript plugins | `kernel-api` |
| `llmchip-sdk` (Python) | [llmchip_sdk/\_\_init\_\_.py](../python/llmchip-sdk/src/llmchip_sdk/__init__.py) `run_plugin` | IF-2 SDK for Python plugins | standard library |
| `llmchip-vchip` (Python) | `vchip` → [llmchip_vchip/cli.py](../python/llmchip-vchip/src/llmchip_vchip/cli.py) | Test harness for virtual chips: loads a chip from its `chip.json`, drives greedy generation from prompts through the chip's host protocol, measures quality against reference and golden chips and speed from cycles, applies the chip's policy, writes run directories ([README](../python/llmchip-vchip/README.md)). Outside the kernel | `llmchip-model`, `llmchip-numeric`, `llmchip-rtl` (the reference, numeric and RTL chips). Never the kernel; hardware runs as a separate process (the RTL bridge, in the EDA container) |
| `@llmchip/web` | [apps/web/src/main.tsx](../apps/web/src/main.tsx) | Web UI, served by harnessd from `apps/web/dist` | `client`, `kernel-api`, React |
| `plugins/<id>` | `llmchip-plugin.json` | Domain work: one process per run | an SDK only; never the kernel |
| `vendor/*` | [vendor/README.md](../vendor/README.md) | Cordis and companions, as upstream source | each other |

Allowed direction. `yarn check:workspace` only checks that workspace imports in `packages/` are declared as `workspace:*` and referenced in `tsconfig.json`, and that `kernel-api` has no workspace dependencies. It does not check the direction itself, and it does not look at `apps/web`.

```
Imports (build time):
  kernel-api        ◀── kernel, client, plugin-sdk, cli, web
  client            ◀── cli, web
  vendor/cordis     ◀── kernel (plugin-host.ts only, among production sources)
  plugin-sdk        ◀── TypeScript plugins        llmchip_sdk ◀── Python plugins

Protocols (run time):
  cli / web / MCP bridge ──IF-1 (WebSocket + HTTP)──▶ harnessd
  harnessd ──IF-2 (stdio)──▶ plugin process   [inside podman for runtime: container (IF-3)]
  plugins never import the kernel, and never reach it except through IF-2
```

No tool checks the following rules, so keeping them is up to you:

- Front ends (CLI, Web UI, MCP, tests that act as clients) reach the kernel only through `KernelClient`. They never import `@llmchip/kernel`. `tests/` may start a kernel in process (`tests/helpers.ts` `startKernel`), but should drive it through the client.
- A plugin returns outputs, metrics and a parse status. Only the verdict engine judges (AP-7, ADR-009).
- Kernel state changes only by `EventStore.append`. Every SQL table besides `events` and `projections` belongs to a `Projection` and is written only in its `apply`. Components may read projection tables directly.

### 2.2 Kernel components

`Kernel`'s constructor builds components 1–14 in dependency order. Each one receives only components listed before it. `Kernel.start` then loads in-kernel plugins and builds the server (15):

| # | Component | Class, file | Owns | Uses |
|---|---|---|---|---|
| 1 | Home layout and lock | `homeLayout`, `acquireLock` ([config/home.ts](../packages/kernel/src/config/home.ts), kernel.ts) | `<home>/` directories, `run/harnessd.lock` | — |
| 2 | K10 config | `loadConfig` ([config/config.ts](../packages/kernel/src/config/config.ts)) | merged `WorkspaceConfig` (schema defaults < `config/config.json` < flags) | — |
| 3 | K10 secrets | `SecretStore`, `Redactor` ([config/secrets.ts](../packages/kernel/src/config/secrets.ts)) | `secrets/secrets.json.enc`, `secrets/master.key` (or `LLMCHIP_MASTER_KEY`) | events, bound after #5 (`bindEvents`) |
| 4 | Logger | `createLogger` ([util/logger.ts](../packages/kernel/src/util/logger.ts)) | `logs/harnessd.log` (redacted) | Redactor |
| 5 | K1 events | `EventStore` | `db/harness.sqlite` | Redactor |
| 6 | K2 artifacts | `ArtifactStore` | `store/objects/`, `store/tmp/`, `artifacts` projection | events |
| 7 | K9 registry | `PluginRegistry` | in-memory manifests; imports plugin data files as artifacts | artifacts |
| 8 | K4 scheduler | `Scheduler` | `runs` projection (= the queue), `runs/<id>/`, child processes, containers | events, artifacts, registry |
| 9 | Incidents | `Incidents` ([security/incidents.ts](../packages/kernel/src/security/incidents.ts)) | `incidents` projection | events |
| 10 | K8 gateway | `EgressGateway` | fingerprint and declassification projections | events, artifacts, incidents |
| 11 | K5 policy | `Policy` | the external allow-list | incidents, gateway |
| 12 | K3/K6 | `Workflow` | flow packs, check definitions, `workflow` projection | events, artifacts, registry, scheduler, incidents |
| 12a | K3 pipelines | `Pipelines` ([workflow/pipelines.ts](../packages/kernel/src/workflow/pipelines.ts)) | `pipelines` projection, the runs it waits for | events, registry, scheduler, workflow, logger |
| 13 | Agent runtime | `AgentRuntime` | `agents` projection, live sessions | events, artifacts, scheduler, workflow, gateway, incidents, logger, secrets (not the API policy) |
| 14 | K9 in-kernel host | `PluginHost` | Cordis root context | events, artifacts, logger |
| 15 | IF-1 server, built in `Kernel.start` | `KernelServer` ([server.ts](../packages/kernel/src/server.ts)) | sockets; `Kernel.start` writes `run/harnessd.json` | policy, handlers, events (subscriptions), artifacts (HTTP) |

All API methods are wired in one place, `Kernel.handlers()`. Each method name maps to one component call.

### 2.3 Home directory (`$LLMCHIP_HOME`, default `~/.llmchip`)

```
config/config.json         optional; schema WorkspaceConfig
secrets/                   master.key, secrets.json.enc        (0700)
db/harness.sqlite          events + projections (WAL, synchronous=FULL)
store/objects/ab/cdef…     artifact content, mode 0444
store/tmp/                 in-flight writes; emptied at start
run/harnessd.json          url, owner token, external token     (0600, while running)
run/harnessd.lock          pid of the owning kernel
logs/harnessd.log          JSON lines, secrets redacted
runs/<run-id>/             in/ out/ work/ log.txt; removed after the run
plugins/                   extra plugin search path
```

### 2.4 Bundled plugins

`plugins.list` on a fresh home shows the following plugins. Stages and checks come from `flow-default` (flow version 0.12.0: 0.11.0 removed the gates, stages S8 and S9, the checks `package.integrity` and `external.signoff`, and the `waiver` field of check definitions; `accuracy.budget` and `phy.drc` admitted waivers, so they are at version 2. Version 0.10.0 added the virtual-chip checks `vchip.l3` and `vchip.conformance`; `rtl.equivalence` is at version 2 since line coverage counts fully covered files, `arch.spec` at version 3 since it judges a cycle target when one is set; 0.12.0 added every stage's development pipeline and its required tests).

| Plugin | Runtime | Handlers | Used in | Unit tests |
|---|---|---|---|---|
| `flow-default` | data | flow pack: S0–S7, 20 checks, stage entry checks, hard-constraint slots, every stage's development pipeline and required tests; data `estimate-request-proxy`, `timing-exceptions-none` | all | via `tests/*-flow.spec.ts` |
| `flow-six` | data | six-step flow pack (Qwen2.5-0.5B-Instruct, owner decision 2026-10-24): the SPEC, RTL, VER and SYN steps so far, 48 checks; PD and SGN follow with R6 and R7 | SPEC, RTL, VER, SYN | `tests/qwen-spec.spec.ts`, `tests/flow-packs.spec.ts`, `tests/tile-verify.spec.ts`, `tests/tile-syn.spec.ts` |
| `process-library` | data | profiles `gf180mcu`, `sephonchitech-180bcd`, `sky130`, `ihp-sg13g2`; libraries `library-180nm`, `library-130nm`; `signoff-gf180mcu` | S0, S3, S6–S7 | via estimator and flow tests |
| `estimator` | process (Python) | `estimate`, `check_views`, `migrate` | S0, stage entry S4/S6/S7, FR-MIG | `test_estimate.py`, `test_migration.py` |
| `model-llama` | process (Python) | `synthesize`, `intake`, `reference`, `import` (optionally also as one model archive), `recover`, `run_graph`, `compare` | S0 (import), S1, SPEC | `test_model.py`, `test_graph.py` |
| `numeric` | process (Python; `benchmark`, `l2_fast_check`, `calibrate_graph` and `diagnose` use the GPU) | `quantize`, `infer`, `evaluate`, `quantize_graph`, `calibrate_graph`, `calibration_data`, `evaluate_graph`, `benchmark_data`, `benchmark`, `benchmark_compare`, `diagnose`, `l2_fast_check` | S2, SPEC | `test_numeric.py`, `test_graph_l2.py`, `test_l2_torch.py`, `test_bench.py`, `test_benchcode.py`, `test_gptq.py`, `test_diagnose.py` |
| `arch-taalas` | process (Python) | `design` (`taalas-tile/1` for a graph numeric spec, the vector engine, or `taalas-spatial/1`), `verify_spec`, `explore`, `slice`, `tlm` | S3, SPEC | `test_arch.py`, `test_spatial.py`, `test_slice.py`, `test_tile.py`, `test_tlm.py` |
| `rtl-taalas` | process (Python) | `generate`, `check_isa`, `tile_generate` (the `taalas-tile/1` RTL bundle of a slice), `tile_check` (ROM round trip, rules, the three linters, Yosys, the three ROM views) | S4, S5, RTL | `test_rtl.py`, `test_spatial.py`, `test_tile.py` |
| `sim-verilator` | container | `lint`, `qualify`, `verify`, `gatesim`, `sdfsim`, `generate` | S5, S7, Playground | `test_simlib.py` |
| `eda-gf180` | container | `calibrate`, `constraints`, `implement`, `drc`, `lvs`, `sta` | M1.5, S6, S7 | `test_calibrate.py`, `test_physical.py` |
| `syn-yosys` | container | `synth`, `dft`, `timing`, `atpg` | SYN | `test_syn.py` |
| `vchip` | process (Python; starts the simulator in Podman) | `bench`, `conform` | after S2 (L2), after S4 (RTL); Web UI page "Virtual chips" | `test_plugin.py` |

Test-only plugins in [tests/fixtures/plugins](../tests/fixtures/plugins):

- `stub-tool` (TypeScript) has handlers `report` (params `violations`, `truncate`, `coverage`, `crash`), `qualify`, `sleep`, `hang`, `crash`, `fail` and `escape`.
- `container-stub` (Python in a container) has handlers `probe` (probes the sandbox) and `sleep`.
- `test-flow` is a three-stage flow pack.

What each domain plugin produces:

- **estimator**: feasibility, area, cost and performance estimates (FR-EST), with low/typical/high values, a source and confidence per item, and the missing parameters. `check_views` reports the integration views a calibrated storage implementation has, per stage. `migrate` assesses migration impact against the design's process-capability dependencies (FR-MIG).
- **process-library**: process profiles (with technology data for migration), the implementation library and the GF180 signoff rules. Every value records its source and confidence. Empirical values stay empirical until calibrated.
- **flow-default** (flow version 0.12.0): stages S0–S7 with their checks, stage entry checks, and the hard-constraint slots `constraints`, `timing-exceptions` and `signoff-rules`. There are no approval gates (owner decision 2026-10-24). Every stage declares a development pipeline `develop` and required tests `tests`, which drive the 5K proxy along the same positive path as `yarn demo`: the params are fixed in the flow pack (the proxy's structure, the ternary numeric scheme, the vector engine on the 1.2×2 mm die); default artifacts (the proxy's estimate request `estimate-request-proxy`, the empty timing exception registry `timing-exceptions-none`, the process profile, the evaluation set, the verification plan, the signoff rules) are bound only when the slot is empty. S1 has no required test (the proxy's intake has no check).
- **flow-six** (flow version 0.2.0): the six steps the owner decided on 2026-10-24 (SPEC, RTL, VER, SYN, PD, SGN), for Qwen2.5-0.5B-Instruct; no approval gates. Four steps so far (SPEC, RTL, VER, SYN); PD and SGN follow with R6 and R7. Three inputs of SPEC that the owner supplies: the slot `model` (the model archive `model-llama import` makes with `archive: true`), `benchmark-data` (the benchmarks of this machine, packed by `numeric benchmark_data`) and `calibration-data` (the calibration text of this machine, packed by `numeric calibration_data`: WikiText-2, or a text file). They are ordinary slots, not hard constraints, so a pipeline or an agent can bind them; what only the owner has is the model and the benchmark data of this machine. The three checks `model.census`, `model.graph` and `model.upstream` are defined in `flow-default` as well: the loader requires identical definitions across packs (§5.2), so a change to one must be made in both, or the pack is dropped. The development pipeline: import (judging `model.census`), graph recovery (`model.graph`), comparison with transformers (`model.upstream`), calibrated quantization (GPTQ on the calibration text: W4, groups of 128, context 2048, fraction bits per layer, 24-bit residual stream), the fast L2 against the reference L2 (`spec.l2.fast`), the quick set on L0 and on L2, their comparison (7 checks `spec.accuracy.quick.<benchmark>`), the diagnosis of the questions L0 answers correctly and L2 wrongly (`spec.diagnosis`), the slice of layer 0 (`spec.slice`), the `taalas-tile/1` design (the node `design`: chip spec and PPA, no check of its own), the consistency check of that chip spec (the node `verify`: `verify_spec`, judging `spec.design`; a node may judge only checks defined on its own handler, and the loader refuses a pack that declares another handler's check) and the transaction-level model on the same tokens (`spec.tlm`). The required tests judge `model.upstream`, `spec.l2.fast`, the 7 quick-set checks, `spec.diagnosis`, `spec.slice`, `spec.design` and `spec.tlm` on the bound slots; the 7 full-set checks `spec.accuracy.<benchmark>` judge a comparison with `set: full`, which can only be started with `runs.start` for now (§7.2). The RTL step (flow version 0.2.0) generates the `taalas-tile/1` bundle of the bound slice (`rtl-taalas tile_generate`: the templates, the view-1 ROM contents of every bank, the manifest) and checks it (`tile_check`, 5 checks: `rtl.tile.lint`, `rtl.tile.synthesizable`, `rtl.tile.roundtrip`, `rtl.tile.rules`, `rtl.tile.rom.views` - the last one proves the behavioural ROM and its synthesised standard-cell netlist equal with Yosys's equivalence engine). They are named `rtl.tile.*` because `flow-default` already defines `rtl.lint`, and the loader requires identical definitions across packs. The VER stage (R4) binds the same `rtl-bundle` and runs `rtl-taalas tile_verify`: `ver.unit` (template suite), `ver.slice` (the functional S-1 q_proj slice against `GraphL2`), `ver.cdc` (the clk/host_sclk domains) and `ver.layer` (the two Q-full layers, 17 observations each, chained against `GraphL2`). `ver.coverage`, `ver.formal`, `ver.mutation`, `ver.dft` and `rtl.tile.equivalence` are runnable; `rtl.tile.equivalence` runs the tiny full model through RTL layer 0, layer 1, final_norm and lm_head and compares all 128 logits with L2.
  - `spec.accuracy.quick.<benchmark>` / `spec.accuracy.<benchmark>`: one per benchmark, by requirements FR-QNT-04: a drop (L0 − L2, in points) above the allowance (MMLU, CMMLU, C-Eval 2; GSM8K, IFEval 3; HumanEval+, MBPP+ 5) fails; a drop within it whose 95% interval's upper bound lies above it is indeterminate (a completeness condition with `when`); otherwise pass. One check per benchmark because the verdict engine judges completeness first: in one check, one benchmark's wide interval would turn another benchmark's clear failure into indeterminate (found in review, 2026-10-24).
- **model-llama**: model import and graph recovery (below), plus llama2.c checkpoint and tokenizer intake into the llama2.c Model IR used by S2–S7. Proxy models (a planned model's structure, fixed seed, untrained, with the bundled `evalset-proxy`). The L0 FP32 reference, a NumPy port of `run.c`, checked against the vendored, compiled `run.c` in `plugins/model-llama/upstream/`. `import` with `archive: true` also keeps the whole model as one artifact, `model.archive` (a tar of the model files and `bundle.json`), which a pipeline passes through a slot to every handler that reads the model (`open_model`; they still accept `bundle` and `files` too).
- **numeric**: quantization (int8/int4/int3/ternary, group scales), the numeric spec (requirements §3.4.2), and the L2 bit-exact integer model with observation-point traces. Also the L2-vs-L0 accuracy evaluation (S2 evidence), with bundled evaluation sets. The rounding (`half-up`, `half-even`, `truncate`), the overflow handling (`saturate`, `wrap`) and the accumulator width (the proven no-overflow width, or a narrower one) are structured fields of the spec, defined once in [arith.py](../plugins/numeric/src/llmchip_numeric/arith.py); the defaults give the L2 of before they existed, bit for bit. `quantize_graph` / `evaluate_graph` do the same for any recovered graph ([graph_l2.py](../plugins/numeric/src/llmchip_numeric/graph_l2.py), spec format `llmchip-numeric-graph-spec/1`): an analysis of models the vector engine cannot implement, with fraction bits per layer and a 24-bit residual stream by default, judged by `numeric.accuracy`. On a llama2.c graph it equals the chip L2 bit for bit.
  - **KV cache**: `GraphL2.forward` takes a `KVCache`; a prompt fed first and then continued token by token gives the same results, bit for bit, as one pass; `rows` computes the logits of chosen positions only (the logits of a whole sequence are too large with a vocabulary of 150,000).
  - **The fast L2** ([l2_torch.py](../plugins/numeric/src/llmchip_numeric/l2_torch.py), `TorchL2`): the same integer arithmetic on PyTorch, on the GPU when there is one. Matrix products are its only floating-point operations, and they are exact: the operands are split into 8-bit limbs and summed in blocks of 256 terms, so every partial sum stays below 2^24, which float32 represents exactly; TF32 is off. Several sequences run as one right-padded batch; padding changes no result and no overflow count; the KV cache has a slot per sequence. Qwen2.5-0.5B (W4, groups of 128): about 150 ms per token for one sequence (launching the kernels is the bottleneck), 6.8 ms per token in a batch of 32; a 300-token prompt in about 0.35 s (2026-10-24). `l2_fast_check` (check `spec.l2.fast`) compares three things: the logits of every step generated through the KV cache in a batch, every observation point and overflow count of a whole-sequence pass, and the batched, padded path that scores answers (logits and overflow counts).
  - **Benchmarks**: `benchmark_data` packs the benchmarks downloaded on this machine (the layout of the tools directory: `hf/`, `datasets/cmmlu`, `datasets/evalplus`, `nltk_data`) into one artifact, `benchmark.data` (parquet files per lm-eval task and split, the EvalPlus problem files, the NLTK data; label `internal`: CMMLU and C-Eval are licensed for non-commercial use), and draws the quick set with the fixed seed 20261024: 5 questions per subject of the multiple-choice benchmarks and 5 per generation benchmark, 900 in all (`benchmark.sets`). Evaluations read only this artifact: lm-eval's dataset loading is routed to the packed files ([benchdata.py](../plugins/numeric/src/llmchip_numeric/benchdata.py)), so prompts, few-shot examples, filters and metrics are lm-eval's own. `benchmark` (params `layer: L0|L2`, `set: quick|full`): L0 is lm-eval's Hugging Face model (bf16, the upstream native precision); L2 is the same model class with its two model calls answered by the fast L2 ([benchlm.py](../plugins/numeric/src/llmchip_numeric/benchlm.py)): log-likelihoods from the host's float64 log-softmax of the chip's integer logits times 2^-frac, generation by greedy decoding through the KV cache. Both decode greedily only (the model's own repetition penalty of 1.1 and sampling settings are replaced; found in review) with the same `max_length` (the numeric spec's context). Correct answers: the option with the highest log-likelihood (multiple choice); the number in the last `\boxed{}` (GSM8K, zero-shot with the instruction Qwen uses for its own evaluations; lm-eval's 5-shot default cuts an instruction model's answers at 256 tokens); strict prompt-level accuracy (IFEval); HumanEval+ / MBPP+ are generated with EvalPlus's prompt, stop strings and sanitizer and run by `evalplus.evaluate`, base and plus tests, under `podman run --network=none --read-only`; both must pass ([benchcode.py](../plugins/numeric/src/llmchip_numeric/benchcode.py); the image is pinned by digest). A checkpoint after each benchmark lets an interrupted run continue with the next one. `benchmark_compare` pairs two reports question by question (same questions, same model, same settings, or it refuses) and gives each benchmark's drop with its paired-bootstrap 95% interval (10,000 resamples, fixed seed), and the questions only one of them answered correctly. The L0 results are cached per (model, questions, settings) by run reuse: an L0 run with the same inputs and params is not computed again (requirements FR-QNT-08).
- **arch-taalas**: the Taalas-style hardwired template. It derives the chip spec and PPA from a frozen numeric spec, and checks their consistency (the S3 check `arch.spec`). Its speed is not a formula: it compiles the engine's microcode (`Compiled`) and counts its cycles with [perf.py](../plugins/rtl-taalas/src/llmchip_rtl/perf.py), a state-by-state model of the core and the memory port whose busy cycles equal the RTL simulation's, step by step ([performance.py](../plugins/arch-taalas/src/llmchip_arch/performance.py); `verify_spec` recomputes them, so a chip spec cannot claim another speed). The microarchitecture options of the chip spec are the lanes, `memoryInterface` (`spi`, or `sqi`: quad SPI on four bidirectional lines) and `operandBuffers` (`none`; `attention`: the head's q and the output accumulators as rotating registers; `all`: also a vector buffer for MATVEC and RMS). `explore` evaluates every combination (exact cycles, estimated area, fit, Pareto front), also for a graph numeric spec of an imported model (analysis, from its shape).
- **rtl-taalas**: compiles the numeric spec into a microcoded vector engine as one deterministic `rtl.bundle`. The bundle holds:
  - the Verilog core, with data in an external SPI or quad-SPI SRAM and optional operand buffers, and an SPI host interface;
  - ROMs as generated case tables, and the microcode;
  - a Verilator testbench with an SRAM model, and `gl_tb.v` for gate level;
  - the instruction-level simulator `isa.py`.

  The weight mapping rule is reversible and round-trip checked. `check_isa` verifies the microcode against L2.
- **The spatial engine `taalas-spatial/1`** (owner decision 2026-10-20: the demo chip follows Taalas, the model as circuits). `arch-taalas design` with `architecture: taalas-spatial/1` ([spatial.py](../plugins/arch-taalas/src/llmchip_arch/spatial.py)), and `rtl-taalas generate` on such a chip spec ([spatial/](../plugins/rtl-taalas/src/llmchip_rtl/spatial/)):
  - every matrix row is a bit-serial adder tree wired from its integer weights (+1 adds an input, −1 subtracts it, 0 is no wire, larger levels add shifted copies); the group mantissa is a fixed sum of shifted copies (CSD digits, one tapped delay line per group); the row output rounds and narrows as the numeric spec says ([sp_lib.v](../plugins/rtl-taalas/src/llmchip_rtl/spatial/rtl/sp_lib.v)). Every layer has its own circuits;
  - attention is a shared unit (16 multipliers, both heads against the one KV head; 16 exact restoring dividers), the KV cache is on chip as a register file, read one row per clock (rotating shift rings were tried first: their 32,768 flop-to-flop links each needed a hold buffer after CTS, and the buffers did not place); there is no program and no external memory;
  - the state machine has one state per stage of `Spatial.schedule()`, so a step takes a fixed number of cycles at every position (1,327 for the proxy: 6,029 tok/s at 8 MHz);
  - the round trip (FR-RTL-02) reads every circuit back from the generated netlist and compares its linear map with q × m (`check_round_trip`); the bundle (format `llmchip-rtl-bundle/3`) ships a model of the circuits, `sp_model.py`, used as the reference where the L2 trace has none;
  - the S3 page shows a spatial design with its own views (`S3Spatial.tsx`): the circuits per matrix (rows, serial adders, delay bits, stream bits), the attention unit and KV cache, the area blocks against the die, the schedule's cycles by stage group and per stage, and the dividers × clock design space; its bench chooses the template, the dividers, the clock and the die;
  - the host interface adds `0x30 READ` (observation words); verification reads every observation point through it, not through a backdoor;
  - area is the generated structure times cell areas measured by synthesis (`spatialCells` in the `gf180mcu` profile), placed at the profile's utilization; the die is the smallest within the size rules (5.0 × 3 mm for the proxy);
  - the option `dividers` (4 to 64 exact softmax dividers) trades cycles for area; `explore` with `architecture: taalas-spatial/1` evaluates every divider count and clock (exact cycles of the generated schedule, area, die, fit, Pareto front: proxy at 8 MHz, 3,733 tok/s with 4 dividers to 7,124 tok/s with 64, 5.89 to 6.25 mm² of cells). For a graph numeric spec of an imported model it is an analysis from the shape, with the proxy's circuit ratios per weight format, stated as an extrapolation.
- **sim-verilator** (Verilator, Icarus Verilog and cocotb in the pinned `iic-osic-tools` image):
  - lint and simulator qualification;
  - the verification plan (regression, directed, boundary, exhaustive, random, coverage; a default plan is bundled), with first-divergence localization (operation, element, cycle, VCD window); for a spatial bundle, `verification-plan-spatial` (the bit-serial cells unit-tested with cocotb at the design's stream lengths, `cocotb_spatial.py`; the core's table-index units tested exhaustively, `cocotb_spatial_index.py`). Line coverage counts every annotated source file, including the fully covered ones (`--annotate-all`; before 2026-10-21 those were left out of the count); the constant tables are data and are not counted. The generated C++ is split (`--output-split`) so it compiles in parallel;
  - gate-level simulation of the routed netlist (L4, Icarus Verilog with the PDK cell models);
  - SDF timing simulation with annotation statistics (supplementary, not qualified);
  - L3 generations for the Chip Playground.
- **eda-gf180** (same image: gf180mcuD PDK, Yosys, OpenROAD, Magic, KLayout, Netgen, OpenSTA):
  - storage implementation calibration (FR-EST-13);
  - constraints from the chip spec;
  - physical implementation with the pad ring (LibreLane Chip flow);
  - DRC, LVS, and 9-corner STA with constraint completeness. Each check runs on exactly the artifacts it judges.
- **vchip** (owner's draft design, L4; [entry](../plugins/vchip/src/llmchip_vchip_plugin/__main__.py)): the virtual-chip test harness on a project's artifacts. Each run builds its chips in the run workspace (`chips_from_artifacts` in [build.py](../python/llmchip-vchip/src/llmchip_vchip/build.py)): the L2 golden from `numeric.spec` and `numeric.weights`, the float reference from `model.ir` (written back as a llama2.c checkpoint, `save_llama2c`) when given, the RTL chip from an `rtl.bundle` when given, which is then the chip under test. The bundle must come from exactly that spec and those weights (its `source` hashes), and only `taalas-spatial/1` runs under the pin-level testbench. A `chip.spec` (S3) must be designed from the same spec and weights and, with an RTL, describe the same design (architecture, dividers); its cycles and clock are then the design's figures. Without it the cycles are the bundle's and the clock is the `clockMHz` param or 8 MHz; the metrics say which (`design_cycles_source`, `clock_source`). `bench` takes the `eval.set` or typed-in `prompts`; `conform` runs T-01–T-19 (`quick` leaves T-18/T-19 `not_run`) and reports the standard its acceptance part applied (`acceptance_tok_s_min`, `acceptance_bit_exact`, `acceptance_max_tokens`). The reports carry no verdict: the checks judge the metrics. `vchip.l3`: completeness RTL, ≥ 20 prompts, ≥ 64 tokens, golden compared, design cycles and clock from the chip spec; criteria no errors, bit-exact, trusted measurement, cycles equal to the design, ≥ 1000 tok/s. `vchip.conformance`: completeness no check unrun, ≥ 20 prompts and, for an RTL chip, the acceptance part at the L3 standard and the chip spec's clock; criterion no check fails. So a run cannot pass by choosing its clock or by trimming its prompts. A metric the run could not measure is absent, so a check needing it is indeterminate. The checks belong to no stage's admission (the flow is unchanged); their verdicts are evidence for exactly the run's inputs. A cancelled run (SIGTERM) ends its chips' simulator processes and removes their containers before it exits.

---

## 3. One complete run path

The walkthrough below was run on 2026-10-19 against a fresh home. The event sequence numbers are the ones observed then.

### 3.1 Path A: `llmchip estimate --model stories260K`

```sh
yarn harnessd serve &                        # LLMCHIP_HOME=/tmp/h for a throwaway home
yarn llmchip estimate --model stories260K    # prints the Markdown summary
yarn llmchip run list                        # the run, its execution and outputs
yarn llmchip events list                     # what was recorded
```

**Kernel start** (`Kernel.start`):

1. The constructor acquires `run/harnessd.lock`, loads config and secrets, and opens the event store. It verifies the last `events.verifyTailOnStart` (1000) hashes. A broken chain refuses to start (`EVENT_LOG_CORRUPT`). It empties `store/tmp/`.
2. Event 1 is `kernel.started`.
3. `PluginRegistry.scan` searches `plugins/` (when `plugins.bundled`), then `<home>/plugins`, then `plugins.searchPaths`. `--plugins` replaces the `searchPaths` of `config.json`, because the config layers merge shallowly. It validates each `llmchip-plugin.json` against `PluginManifest` and the `kernelApi` range, and imports every `data` file as an artifact (events 2–10 are `artifact.created`, all `plugin:*`). It rejects a bad plugin, records the problem, and does not abort the start.
4. `Workflow.loadFlows` reads every `flow.pack` artifact and collects check definitions. A pack with conflicting definitions of one check id, one that does not match the schema (for example one that still declares gates), or one whose pipeline declares a node judging a check of another handler (or an entry check or required test no pack defines) is rejected whole as a `FLOW_INVALID` problem written to the kernel log: none of its stages and none of its checks are registered. The pack a project was created with stays stored as it was: when a project from before R0 is read, its history-only fields (`gates`, the stages' entry `gates`, the checks' `waiver`) are ignored.
5. `Scheduler.recover` finishes any run still recorded as `running` as `interrupted`. It kills the run's orphaned process group (only if `/proc/<pid>/environ` carries `LLMCHIP_RUN=<id>`) and its container. It retries the run if attempts remain (from its latest checkpoint when the handler declares `resume`), and wipes `runs/`. Then `Pipelines.recover` continues the pipelines still running: a node whose run was interrupted runs again (from the checkpoint, likewise), and runs that ended meanwhile are recorded on their nodes. In-kernel plugins passed in `KernelOptions.plugins` are loaded next (tests only).
6. `KernelServer.listen` binds to `127.0.0.1:<random port>`. `run/harnessd.json` is written with the URL and two fresh tokens. Then `Scheduler.pump` runs.

**The command** (`case 'estimate'` in [cli/src/main.ts](../packages/cli/src/main.ts)):

7. The CLI reads `run/harnessd.json`. If it is missing, the CLI says how to start the kernel and exits 1. `KernelClient.connect` opens `ws://…/v1/rpc?token=…`.
8. `plugins.list` finds the process profile (default `sephonchitech-180bcd`) and the implementation library among the data artifacts. It then calls `runs.start {plugin: estimator, handler: estimate, params, inputs: [profile, library]}`.

**The API** (`KernelServer.handleMessage`):

9. The server validates the request params against the method's schema definition (`INVALID_PARAMS`, JSON-RPC -32602). `Policy.authorize` lets the owner token through. With the external token, a method outside the allow-list opens a `permission_denied` incident and fails `PERMISSION_DENIED`. The server calls the handler, then validates the result (`RESULT_INVALID` becomes an internal error).

**Queueing** (`Scheduler.start`, synchronous, before anything runs):

10. It resolves the handler (`PLUGIN_NOT_FOUND`, `HANDLER_NOT_FOUND`). A `data` plugin cannot run (`PLUGIN_NOT_EXECUTABLE`). It validates params against the handler's JSON Schema, applying defaults (`RUN_PARAMS_INVALID`). Each input artifact must exist (`ARTIFACT_NOT_FOUND`), and must match the handler's input names, types, `required` and `multiple` (`RUN_INPUT_UNKNOWN`, `_TYPE`, `_MISSING`, `_MULTIPLE`).
11. The run label is the most sensitive input label. Event 11 is `run.queued` with the full `RunRecord`. The `runs` projection inserts the row, which *is* the queue. The method returns the queued record.

**Execution** (`Scheduler.pump` → `Scheduler.execute`):

12. Capacity is `scheduler.maxParallel` (default 2). Nothing starts while the kernel is paused.
13. It creates `runs/<id>/{in,out,work}`. Each input is copied (reflink if possible) into `in/` and set to 0444, so a plugin cannot alter stored content.
14. It builds the command from `entry.command`, substituting `${python}` (`.venv/bin/python` when present), `${node}` and `${pluginDir}`. For `runtime: container` it wraps the command in `podman run` (`Scheduler.containerCommand`). `PluginProcess.spawn` starts it in its own process group, with an environment of `PATH`, `HOME` and the like plus `LLMCHIP_RUN`. Plugins never see secrets.
15. Event 12 is `run.started` (pid). A timeout is armed. Its length is the `timeoutMs` passed to `runs.start` if any, otherwise the handler's `timeoutMs` (estimator: 60 s), otherwise `scheduler.defaultTimeoutMs` (1 h).
16. The IF-2 exchange happens over stdin and stdout. `initialize` checks that the plugin's `id` equals the manifest's. Then `invoke {run, handler, params, inputs[{name, hash, type, label, path}], outDir, workDir}`. The Python SDK runs `handle_estimate` ([estimator/\_\_main\_\_.py](../plugins/estimator/src/llmchip_estimator/__main__.py)), which writes `estimate.json` and `estimate.md` to `outDir`. It returns `Result(outputs, metrics, parse={status: complete})`. The kernel validates the result as `ProtocolInvokeResult` and sends `shutdown`. Plugin stderr and `log` notifications go to `log.txt`, redacted. `progress` notifications update the in-memory progress.
17. The execution status is computed: `exited_zero` / `exited_nonzero` / `crashed` / `timed_out` / `cancelled` / `interrupted`. Metrics and parse status are kept **only** for `exited_zero`.
18. `registerOutputs` registers the outputs only after `exited_zero`. Each output must be a file inside `out/` and declared with that name and type in the manifest (`RUN_OUTPUT_INVALID`, which turns the run into `exited_nonzero`). Each output is stored with `inputs` = the run's inputs (lineage), `run`, `creator: plugin:estimator`, and a label no lower than the run's. Events 13–14 are `artifact.created`. Outputs are stored one at a time, not in one transaction. If a later output is invalid, the earlier ones stay in the store, carrying `run: <id>`, while the run record lists no outputs (conflict C6 in §7.3). The run log becomes a `run.log` artifact, whatever the execution status (event 15).
19. `finish` appends event 16, `run.finished`, with execution, exit code, outputs, metrics, parse and log. In the same transaction it queues a retry when the execution was `crashed` or `interrupted` and attempts remain. Then it wakes `runs.wait` callers and deletes `runs/<id>/`.

**Result:**

20. The CLI's `runs.wait` returns the finished record. The CLI downloads the `summary` output over `GET /v1/artifacts/<hash>` (bearer token; external clients pass the egress gateway first) and prints it. `artifact lineage <summary>` then shows the profile and library as its inputs, through the run.

**Failure handling, as observed:**

| Situation | Where it stops | What the caller sees | What is recorded |
|---|---|---|---|
| Kernel not running | CLI | `harnessd is not running for <home>`, exit 1 | nothing |
| Params violate the handler schema | `Scheduler.start` | `RUN_PARAMS_INVALID`, exit 1 | nothing (not queued) |
| Input artifact missing, or input unknown, mistyped, missing or repeated | `Scheduler.start` | `ARTIFACT_NOT_FOUND`, `RUN_INPUT_UNKNOWN` / `_TYPE` / `_MISSING` / `_MULTIPLE` | nothing |
| Handler raises | plugin → JSON-RPC error -32001 | run `exited_nonzero`, `error` = `ValueError: …` | `run.finished`, a log artifact with the traceback. No `shutdown` is sent; the kernel waits up to `cancelGraceMs` (2 s) for the plugin to exit, then kills the group |
| Plugin exits before answering, returns an invalid result (`PLUGIN_RESULT_INVALID`), or identifies as another plugin | `Scheduler.execute` | `crashed` | `run.finished`; retried only if `maxAttempts` > 1 |
| Plugin answers `initialize` with an error | `Scheduler.execute` | `exited_nonzero` | `run.finished` |
| Plugin writes a non-JSON line to stdout | `PluginProcess` | the run continues | a `protocol violation` warning in the run log |
| Output outside `out/` or undeclared | `registerOutputs` | `exited_nonzero`, `RUN_OUTPUT_INVALID …` | the run lists no outputs; outputs stored before the bad one remain (C6) |
| Timeout or `runs.cancel` | `Scheduler.abort` | `timed_out` / `cancelled` | `cancel` notification, SIGTERM, then SIGKILL after `cancelGraceMs`, `podman kill` |
| Kernel killed mid-run | next `Kernel.start` | `interrupted`; retried only if `maxAttempts` > 1 (default 1) | `Scheduler.recover` (AC-M1-04) |

### 3.2 Path B: from the run to a verdict and a stage (S0 → S1)

There are no approval gates (owner decision 2026-10-24). A verdict binds its run's input hashes: it is evidence for those artifacts only. Only a stage's admission checks (`entry.checks`) block entering it; a failed check does not lock the flow, but the failure stays on record until the cause is fixed and the work runs again and passes. These commands were run in sequence on a fresh home on 2026-10-24:

```sh
yarn llmchip project create chip --title demo                    # created project chip (flow "default")
yarn llmchip artifact put req.json --type estimate.request --media-type application/json
yarn llmchip artifact put charter.md --type project.charter
yarn llmchip project bind chip charter <hash>                    # also: estimate-request, process-profile, impl-library
yarn llmchip run start estimator estimate --input request=<req> --input profile=<p> --input library=<l> --wait
yarn llmchip verdict evaluate estimate.feasibility <run> --project chip   # estimate.feasibility: pass
yarn llmchip stage enter chip S1                                 # entered stage S1
yarn llmchip stage enter chip S2                                 # entered stage S2
yarn llmchip call gates.release '{"project":"chip","gate":"G0"}'  # unknown method gates.release
```

What happens inside [workflow/engine.ts](../packages/kernel/src/workflow/engine.ts):

- **`Workflow.evaluate`** (K6) checks that the run belongs to the check's plugin and handler. `judge` then applies the check in order. The execution must be accepted, the parse must be `complete`, a `requiredCapability` must be qualified, and the `completeness` criteria must hold. Any failure gives `indeterminate`. A missing metric also gives `indeterminate`. A failed `criteria` gives `fail`. There are no waivers: a failure is a failure. The verdict's `subjects` are the run's input hashes. The engine appends `verdict.recorded`. A `fail` or `indeterminate` opens a `check_failed` incident, and the incident carries `divergence_*` metrics when the run reported them. A pass that follows a non-pass of an older check version opens a `standard_lowered` incident (AC-M1-13).
- **`Workflow.bind`** appends `project.slot_bound`. Slots in `hardConstraintSlots` are bound only by humans; anyone else opens a `hard_constraint_change` incident.
- **`Workflow.enterStage`** requires every entry check (`{check, slot}`) to have a latest passing verdict on the bound artifact. Otherwise it fails `STAGE_ENTRY_BLOCKED` with the reasons, including `views.<stage>.missing` metrics (FR-PHY-08).
- **`Workflow.applyMigration`** records a confirmed migration assessment and the stage to restart (`migration.applied`); restarting at S3 or earlier needs a human (FR-MIG-04).

Older event logs with `approval.*`, `waiver.recorded` and `package.exported` events still read and verify: these kinds stay in the schema as history only, with no writer, and the `workflow` projection (version 3) ignores them. The verdict value `waived` and `VerdictRecord.waiver` likewise appear only in history.

`tests/flow-helpers.ts` `throughS3` and `throughS5Proxy` drive the same path through S3 and S5 with the real plugins. `tests/m4-flow.spec.ts` continues it to S7 and traces the GDS back to the model weights.


### 3.3 Path C: the two entries of a stage (pipelines)

Every step's "run development" and "run all tests" are pipelines (requirements FR-PRJ-07/08). These commands were run in sequence on a fresh home with the bundled plugins on 2026-10-24:

```sh
yarn llmchip project create demo --title demo
yarn llmchip pipeline plan demo S2 --kind develop      # S2: 5 nodes, not ready; lists each missing slot (S2/quantize needs slot:model-ir ...), exit code 1
yarn llmchip pipeline plan demo all --kind develop     # S0, S1, S2, S3, S4, S5, S6, S7: 40 nodes, ready
yarn llmchip pipeline start demo S0 --kind develop --wait   # develop S0  passed  (3/3 done); S0/estimate judged estimate.feasibility: pass
yarn llmchip pipeline start demo S1 --kind develop --wait   # develop S1  passed  (5/5 done)
yarn llmchip pipeline start demo S0 --kind test --wait      # test S0  passed; S0/estimate.feasibility reused the development run
yarn llmchip pipeline list --project demo
```

What happens inside [workflow/pipelines.ts](../packages/kernel/src/workflow/pipelines.ts):

- **`Pipelines.plan`** expands the nodes from the flow pack the project was created with (`build`): per step of a development pipeline `<stage>/defaults`, the `beforeEntry` nodes, `<stage>/enter`, then the other nodes; `all` chains every stage that declares a development pipeline (a step's `defaults` depends on all nodes of the previous one). Each required test is one node, independent of the others. Inputs refer to `slot:<slot>`, `data:<data id>` or `node:<node>.<output>`. A slot that is unbound now and that no earlier node of this pipeline binds, or data that does not exist, is listed as missing.
- **`Pipelines.start`** refuses with `PIPELINE_INPUTS_MISSING` when inputs are missing, with `PIPELINE_EMPTY` when nothing is declared, and with `PIPELINE_RUNNING` when a pipeline is running on one of the stages on the same project branch; otherwise it appends `pipeline.started` (with all nodes) and calls `advance`.
- **`advance`** repeats: a pending node whose dependencies are all done is executed; a node with a failed, skipped or cancelled dependency is `skipped` (`PIPELINE_DEPENDENCY_FAILED`); when no node is pending or running it appends `pipeline.finished` (`passed` if all are done, else `failed`).
- Executing a node: `defaults` binds the empty slots to the default artifacts; `enter` calls `Workflow.enterStage` (the admission checks judge); `run` starts a run as the person who started the pipeline; `test` first looks for a successful run of the same handler with the same params (defaults filled in) and inputs and reuses it (`reused`), else starts one. When the run ends (`Scheduler.wait`), a `run` node binds its outputs to slots and judges its listed checks, a `test` node judges its check; any verdict other than `pass`, a run that did not end successfully, or a refused binding makes the node `failed`, with the reason. A pipeline binds a hard-constraint slot only while it is empty; an artifact a person bound is not replaced (`PIPELINE_HARD_CONSTRAINT_BOUND`), and a person binds the new one by hand. Only a node's current run (or the run it reused) finishes the node: when a pipeline is resumed right after a cancel, the cancelled run does not decide the new attempt. Every state change is a `pipeline.node` event.
- **`Pipelines.cancel`** marks unfinished nodes `cancelled` and cancels their runs; **`Pipelines.resume`** (a failed or cancelled pipeline) appends `pipeline.resumed` and puts the failed, skipped and cancelled nodes back to pending; done nodes stay. When a node with the same inputs runs again and its last run left a checkpoint, and the handler declares `resume`, it continues from the checkpoint.

`tests/pipelines.spec.ts` proves these semantics with `test-flow` (including continuing after the kernel is killed, from the checkpoint of the interrupted node); `tests/pipelines-flow.spec.ts` runs the development and the tests of S0–S3 on a fresh project with the bundled plugins, and S0–S7 through `all` with `LLMCHIP_FULL_FLOW=1`.

---

## 4. Data and resource ownership

### 4.1 Who writes what

| Resource | Sole writer | Readers | Lifetime and release |
|---|---|---|---|
| Event log (`events`) | `EventStore.append` / `transaction` | kernel components; clients through `events.list` / `events.subscribe` | append-only, never rewritten; `events verify` checks the chain |
| Projection tables | each `Projection.apply`, inside the append transaction | owning component, UI via API | disposable; `projections rebuild` replays the log |
| Artifact content and catalog | `ArtifactStore.put` (`artifact.created`) | scheduler (copies), HTTP download, agents (`read_artifact`) | immutable; `markDeleted` only marks it, and content is kept (no purge or GC yet) |
| Artifacts from plugin data files | `PluginRegistry.register`, at each scan | flows, CLI `estimate`, tests | idempotent by hash; label default `public` |
| Run outputs | `Scheduler.registerOutputs` | any client | only after `exited_zero`; label ≥ run label |
| Run checkpoints (`run.checkpoint`) | `Scheduler.storeCheckpoint`, when an IF-2 `checkpoint` notification arrives | retries and pipeline resumption (input `resume`), any client | stored while the run is going, one after the other in the order reported, kept if the run fails; the path must be a regular file in the run's `work/` (not a symbolic link); a run ends only after all its checkpoints are stored |
| Pipelines | `Pipelines` (`pipeline.*` events) | API, UI, CLI | events only; continued by `Pipelines.recover` after a restart |
| Run logs (`run.log`) | `Scheduler.execute` | any client | stored for every run, whatever its execution status |
| Uploads (`PUT /v1/artifacts`) | `KernelServer` for the owner token only | — | external tokens are refused with an incident |
| Run workspace `runs/<id>/` | `Scheduler.execute` creates and removes it | the plugin (in/ read-only) | removed after every run; all of `runs/` is wiped at start |
| Plugin processes and containers | `PluginProcess` (own process group), `podman run --rm --name=llmchip-run-<id>` | — | killed on finish, cancel, timeout or kernel stop; orphans killed at the next start |
| Verdicts, constraints, projects, migration records | `Workflow` | API | events; verdicts are never deleted, failed ones stay |
| Incidents | `Incidents.open` / `close`, called by `Policy`, `Workflow`, `EgressGateway`, `AgentRuntime` | UI, API | closing needs a cause and an action (FR-AGT-27) |
| Egress decisions | `EgressGateway.check`, before every LLM request and every MCP download | audit | one `egress.decided` per request |
| Handoff to another provider | `AgentRuntime.switchProvider` compares the pack's label with the provider's limit itself | API | stores the pack as `agent.handoff`; a refused switch opens a `handoff_blocked` incident, with no `egress.decided` |
| Tasks, agent steps, change sets | `AgentRuntime` | API, UI | the state lives in events, so a new session is rebuilt from the log (handoff) |
| Secrets | `SecretStore.set` / `remove` (`secret.changed` carries names only) | providers via `secret(name)` | values never in events or logs (`Redactor`) |
| `run/harnessd.json`, lock | `Kernel.start` / `shutdown` | CLI, MCP bridge, tests | deleted at shutdown; a stale lock from a dead pid is taken over |

Event kinds by writer: `kernel.started/stopped`, `projection.rebuilt` (Kernel); `kernel.paused`, `run.*` including `run.checkpointed` (Scheduler); `pipeline.started`, `pipeline.node`, `pipeline.resumed`, `pipeline.finished` (Pipelines); `artifact.*` (ArtifactStore); `secret.changed` (SecretStore); `plugin.*` (PluginHost); `verdict.*`, `project.*`, `stage.*`, `constraint.*`, `migration.*` (Workflow; `approval.*`, `waiver.recorded` and `package.exported` are history only, with no writer); `incident.*` (Incidents); `egress.*`, `declass.*` (EgressGateway); `task.*`, `agent.step`, `changeset.*` (AgentRuntime). Every payload is validated against its schema definition (`KERNEL_EVENT_PAYLOAD_DEFS`) before it is hashed. Unknown kinds are refused.

### 4.2 Cancellation and shutdown

- **One run**: `runs.cancel` → `Scheduler.abort`. The scheduler sends the IF-2 `cancel` notification (the SDKs set `ctx.signal` / `ctx.cancelled`), runs `podman kill` for containers, sends SIGTERM to the group, and sends SIGKILL after `cancelGraceMs`. A queued run that never started is finished as `cancelled` directly.
- **Emergency stop** (FR-AGT-23): `kernel.pause` appends `kernel.paused`. No new run starts and agent loops pause; running runs continue. `kernel.resume` reverses it.
- **Kernel stop** (`Kernel.shutdown`) runs in a fixed order:
  1. delete the runtime file;
  2. close the server;
  3. stop advancing pipelines (`Pipelines.stop`: runs interrupted now do not fail their nodes; the next `Pipelines.recover` runs them again), then stop the scheduler (abort every run as `interrupted` and wait for each to record its result);
  4. dispose in-kernel plugins in reverse load order, with timeouts;
  5. append `kernel.stopped`;
  6. close the database and release the lock.

  `SIGINT`/`SIGTERM` and `kernel.shutdown` all take this path. The agent runtime is not stopped explicitly (conflict C7 in §7.3).
- **Agent tasks** stop at the emergency stop, at `maxSteps`/`maxTokens` (`budget_exceeded` incident), after `loopThreshold` repeats of one failing call, on a blocked egress, or on an LLM error. A stopped task is `paused` and can be resumed with `tasks.run`.

---

## 5. Extension

Each recipe names a real example that already follows it, and the test that proves it. Before extending anything, check whether a manifest, flow pack or data file can express the change. Kernel code changes should be the last resort (AP-2).

### 5.1 An out-of-process plugin (a new tool or domain step)

Example: `estimator` ([manifest](../plugins/estimator/llmchip-plugin.json), [entry](../plugins/estimator/src/llmchip_estimator/__main__.py)). The smallest examples are [stub-tool](../tests/fixtures/plugins/stub-tool) (TypeScript) and [container-stub](../tests/fixtures/plugins/container-stub) (Python in Podman).

1. Write `plugins/<id>/llmchip-plugin.json` (schema `PluginManifest`):
   - `id`, `version`, `kinds`, and `kernelApi: "^0.3"`.
   - `runtime`: `process`, `container` or `data`.
   - `entry.command`, using `${python}`, `${node}` and `${pluginDir}`. For a container, also `entry.image`, pinned by digest.
   - `handlers[]`, each with `inputs` (name, artifact `types`, `required`, `multiple`), `outputs` (name, `type`, `mediaType`), a JSON Schema `params` with defaults, and an optional `timeoutMs`.
   - Optional `capabilities` with a qualification handler.
2. Implement the handlers.
   - Python: `run_plugin("<id>", "<version>", {"<handler>": fn})`. `fn(ctx)` reads `ctx.input(name).path` and `ctx.params`, writes files under `ctx.out_dir`, and returns `Result(outputs=[Output(name, path, type)], metrics={…}, parse={"status": "complete", "parser": "<id>/<v>"})`. It calls `ctx.check_cancelled()` in long loops.
   - TypeScript: `runPlugin({id, version, handlers})` returns the same shape, and handlers watch `ctx.signal`.
   - In both: stdout belongs to the protocol (the SDKs redirect `print` and `console.*` to stderr, which goes to the run log). Report `parse.status` honestly: `complete`, `incomplete` when a report was not fully read, or `failed` (the schema `ParseStatus` allows no other value). Never return a verdict.
3. Python `process` plugins only: add the plugin to `[tool.uv.workspace].members` and to the root `dependencies` in `pyproject.toml`, then run `uv lock && yarn setup:python`. Container plugins are not workspace members: the kernel mounts the Python SDK into the container at `/llmchip/sdk`.
4. Tests:
   - A unit test next to the plugin: `plugins/<id>/tests/test_*.py` (collected by `yarn check:python`).
   - An end-to-end test through the kernel in `tests/`, using `startKernel(undefined, {config: {plugins: {searchPaths: [...]}}})` as [tests/runs.spec.ts](../tests/runs.spec.ts) does for `stub-tool` and [tests/containers.spec.ts](../tests/containers.spec.ts) for `container-stub`.
   - Tests that need an image skip, visibly, when it is absent (`podman image exists`).
5. Run `yarn llmchip plugins list`, which shows registration problems as `problem:` lines. Then run `yarn llmchip run start <id> <handler> --input …`.

Verified by hand: `yarn harnessd serve --plugins tests/fixtures/plugins`, then `run start stub-tool report --input design=<hash>` with `violations` 0, then 3, then `truncate`. `verdict evaluate stub.drc <run>` gave `pass`, `fail` (`CRITERION_FAILED`) and `indeterminate` (`PARSE_INCOMPLETE`).

### 5.2 A check or a stage entry check

Example: the stage entry check `impl.views.S4`. It is defined in [flow-default/flow.json](../plugins/flow-default/flow.json), judges `estimator/check_views`, and is attached to stage S4 as `entry.checks: [{check: "impl.views.S4", slot: "storage-impl"}]`. The other example is the S0 check `estimate.feasibility`.

1. Make the handler report the metrics the check needs, and nothing judged.
2. Add a check to the flow pack's `checks`:
   - `id`, `version`, `plugin`, `handler`.
   - `completeness`: conditions under which the result means anything, else `indeterminate`.
   - `criteria`: pass or fail. A criterion with `when` (a condition on another metric) applies only when the condition holds; when it does not, the verdict records `CRITERION_NOT_APPLIED`, and a missing condition metric makes it `indeterminate`. The handler reports the condition explicitly (example: `arch.spec` judges `cycles_within_target` when `cycles_target_set` is true; `stub.limit` in `test-flow`).
   - Optional `requiredCapability`, `requireParse` and `acceptExitCodes`.
   - Change `version` whenever a standard changes. That is how a lowered standard is detected (AC-M1-13).
3. If it should block entering a stage, reference it from that stage's `entry.checks` with the slot it judges; the run's input must be the artifact bound to that slot, or the verdict does not judge it. There are no gates and no waivers: a failed check stays failed until it is fixed and runs again.
4. A check id may be defined by several flow packs only if every definition is identical. Otherwise the conflicting pack is not loaded, and the kernel log records a `FLOW_INVALID` problem naming the check (`plugins list` does not show flow pack problems). A pipeline node may judge only checks defined on its own plugin and handler, because the verdict is recorded against that node's run; a pack that declares another handler's check, or whose stage entry checks or required tests name an undefined check, is refused at load (tests: [tests/flow-packs.spec.ts](../tests/flow-packs.spec.ts) for the invariant over the bundled packs, [tests/workflow.spec.ts](../tests/workflow.spec.ts) for the refusal of a pack that cannot be judged).
5. Tests:
   - Verdict semantics are exercised with `test-flow` in [tests/workflow.spec.ts](../tests/workflow.spec.ts).
   - The bundled flow packs load whole, every node judges a check of its own handler, every plugin they run is registered and a pack that cannot be judged is refused: [tests/flow-packs.spec.ts](../tests/flow-packs.spec.ts), [tests/workflow.spec.ts](../tests/workflow.spec.ts).
   - The S0 check on missing data is covered in [tests/estimator.spec.ts](../tests/estimator.spec.ts).
   - Stage entry on views is covered in [tests/calibration.spec.ts](../tests/calibration.spec.ts) (EDA image).

A new flow pack is a `data` plugin with a `flow.pack` data file ([test-flow](../tests/fixtures/plugins/test-flow)). Projects choose one with `projects.create {flow}`.

### 5.2a A stage's development pipeline and required tests

Example: stage S2 of [flow-default/flow.json](../plugins/flow-default/flow.json): in `develop`, `quantize` binds `spec` and `weights` to `numeric-spec` and `l2-weights`, `evaluate` takes inputs such as `node:quantize.spec` and judges `accuracy.budget`, `infer` binds `trace` to `l2-trace`; `tests` judges `accuracy.budget` on the bound slots. The smallest example is stages A and B of [test-flow](../tests/fixtures/plugins/test-flow/flow.json).

1. Write `develop` in the stage: `defaults` (slot → `data:` or `slot:` reference, bound only when the slot is empty) and `nodes`. Each node has an `id`, `plugin`, `handler`, and optional `params`, `inputs` (input name → reference or array of references), `after`, `bind` (output name → slot), `judge` (check ids), `beforeEntry` (runs before the stage is entered, for its admission checks to judge) and `timeoutMs`.
2. Write `tests` in the stage: each is a `check` with the `inputs` and `params` of its run. The handler comes from the check definition; a successful run with the same params and inputs is reused, so tests after the development pipeline do not compute again.
3. References point only to earlier nodes of the step (`node:`), to slots (`slot:`) or to plugin data (`data:`); data crosses steps through slots. A flow pack referring to an undeclared node fails with `PIPELINE_INVALID` when it is expanded.
4. When params change what a check judges, change the check's `version` as in §5.2; pipelines themselves have no version.
5. Test as [tests/pipelines.spec.ts](../tests/pipelines.spec.ts) (semantics) and [tests/pipelines-flow.spec.ts](../tests/pipelines-flow.spec.ts) (real plugins: on a fresh project every step must be `ready` and pass, and the tests must reuse the development runs) do.

### 5.2b A handler's resources and checkpoints

Example: `heavy` (`resources: {memGb: 3}`), `gpu` (`gpu: 1`) and `steps` (checkpoints and resumption, [plugin.ts](../tests/fixtures/plugins/stub-tool/plugin.ts)) of [stub-tool](../tests/fixtures/plugins/stub-tool/llmchip-plugin.json).

1. Resources: write `resources: {cpu, memGb, gpu}` in the handler's manifest. Without it, process plugins count as 1 core and 1 GiB, container plugins as the scheduler's `containerCpus`/`containerMemory`, neither with a GPU. A container run's `--cpus` and `--memory` are this request. Only one run uses GPUs at a time (requirements NFR-15), and only it sees them (`CUDA_VISIBLE_DEVICES`); container plugins are not given GPUs. The host's capacity is the configuration `scheduler.resources`; items not written are detected (CPU cores, total memory, `nvidia-smi -L`).
2. Checkpoints: the handler writes the checkpoint completely in its work directory (to a temporary name, then renames it), then calls `ctx.checkpoint(path, name)` (Python and TypeScript), with `path` relative to `work/`.
3. Resumption: declare the optional input `resume` (type `run.checkpoint`) in the manifest and read `ctx.resume` at the start of the handler. On a retry (`maxAttempts` > 1), when a pipeline reruns a node interrupted by a kernel restart, and when `pipelines.resume` reruns a node with the same inputs, the kernel passes the latest checkpoint.
4. Test as [tests/resources.spec.ts](../tests/resources.spec.ts) and [tests/checkpoint.spec.ts](../tests/checkpoint.spec.ts) do; the Python SDK's notification is covered by `python/llmchip-sdk/tests/test_protocol.py`.

### 5.3 Process, library or template data

Example: [process-library](../plugins/process-library/llmchip-plugin.json). Add a file under `data/` and a `data[]` entry (`id`, `type`, `path`, `label`). Every value carries its source and confidence (AGENTS.md). Consumers find data by type and id through `plugins.list`, as the CLI `estimate` command does. Tests: [tests/estimator.spec.ts](../tests/estimator.spec.ts), and `plugins/estimator/tests/test_estimate.py` for the numbers.

### 5.3a An architecture family for graph recovery

Example: `qwen3` in [graph.py](../plugins/model-llama/src/llmchip_model/graph.py).

1. Map the model's `model_type` to a family in `HF_TYPES`; tensor names go in a names table (`HF_NAMES`, `LLAMA2C_NAMES`).
2. Emit the decoder block's nodes with the family's differences, and record each difference as a decision with its evidence (a config field or a tensor).
3. If the family needs an operation the interpreter lacks, add it to `run_node` in [execute.py](../plugins/model-llama/src/llmchip_model/execute.py).
4. Teach [census.py](../plugins/model-llama/src/llmchip_model/census.py) the family's tensor names if they are new (`ROLE_PATTERNS`), so the census check still finds every role.
5. Test with a tiny random model built by transformers, as `test_recovered_graph_matches_transformers` in [plugins/model-llama/tests/test_graph.py](../plugins/model-llama/tests/test_graph.py) does: logits within 1e-4 relative, identical greedy tokens.
6. Give the new node ids and decisions readable names in [views/steps/S1.tsx](../apps/web/src/views/steps/S1.tsx) and both catalogs.

Anything a family does not support must return `supported: false` with a reason code, never an approximate graph.

### 5.3b A numeric mode (rounding, overflow, accumulator)

Example: `half-even` rounding. A mode is chip behavior, so it has four implementations that must agree bit for bit:

1. [arith.py](../plugins/numeric/src/llmchip_numeric/arith.py) (used by `l2.py` and `graph_l2.py`) and the parameter enum of `quantize` / `quantize_graph` in `plugins/numeric/llmchip-plugin.json`; `spec.py` writes it into the spec with its definition.
2. The self-contained copy in [isa.py](../plugins/rtl-taalas/src/llmchip_rtl/isa.py) (it ships in the RTL bundle), selected by `modes(params)`.
3. A define emitted by `Compiled._widths` in [compiler.py](../plugins/rtl-taalas/src/llmchip_rtl/compiler.py) (listed in `flags` of `bundle.py`) and an `ifdef` variant in [llmchip_functions.vh](../plugins/rtl-taalas/src/llmchip_rtl/rtl/llmchip_functions.vh).
4. The ALU unit tests in `plugins/sim-verilator/cocotb_alu.py`, which read the mode from the bundle parameters.

Tests: `test_rounding_modes_match_exact_arithmetic` (exact rationals), `test_isa_rules_are_the_numeric_spec_rules` and `test_every_numeric_scheme_compiles_to_the_same_bits_as_l2` in `plugins/rtl-taalas/tests/test_rtl.py`, and the RTL part of `tests/numeric-schemes.spec.ts` (Verilator against L2, the ALU in the mode). Mind signedness in Verilog: an unsigned operand makes a whole expression unsigned and `>>>` logical (the first half-even attempt failed 9920 random ALU vectors that way).

### 5.3c A microarchitecture option of the engine

Example: `memoryInterface: sqi` ([llmchip_sqimem.v](../plugins/rtl-taalas/src/llmchip_rtl/rtl/llmchip_sqimem.v)). An option changes cycles and area, never results:

1. Accept it in `Compiled(options)` and emit a define from `_widths` (listed in `flags` of `bundle.py`); `arch-taalas` takes it as a `design` / `explore` parameter and writes it into the chip spec's `dataflow`, from which `rtl-taalas generate` reads it.
2. Implement it in the RTL under the define, with a reset value for every register; keep the instruction set and L2 unchanged.
3. Model its cycles in [perf.py](../plugins/rtl-taalas/src/llmchip_rtl/perf.py): the state walk (`fast=False`) mirrors the RTL state by state; a closed form for long loops must equal it (`test_cycle_model_closed_form_equals_the_state_walk`).
4. Add it to `OPTIONS` in [tests/arch-perf.spec.ts](../tests/arch-perf.spec.ts): the busy cycles of every RTL step must equal the chip spec's prediction, and the RTL must equal L2.
5. Measure its area: synthesize it and implement it on the die (`eda-gf180 implement`). The area estimate of the process library is not enough (§7.2).

### 5.3d An operator of the spatial engine

Example: the SiLU gate of the spatial core (`core_verilog` in [generator.py](../plugins/rtl-taalas/src/llmchip_rtl/spatial/generator.py)).

1. Add a stage to `Spatial.schedule()` with its exact cycle count; the generator emits one state per stage, so the schedule is the cycle model.
2. Emit its logic in `core_verilog`: arithmetic as continuous assignments (no function with local variables in a clocked block), constants as `sp_rom_*` table modules, multipliers as `sp_mul` instances, every register with a reset value. Lint must stay at zero warnings.
3. Mirror it in [sp_model.py](../plugins/rtl-taalas/src/llmchip_rtl/spatial/sp_model.py) and keep `test_the_model_of_the_circuits_equals_l2` passing.
4. Run [tests/spatial-flow.spec.ts](../tests/spatial-flow.spec.ts): the RTL equals L2 bit for bit and every step takes the scheduled cycles. Re-measure the area by synthesis when the operator adds logic and update `spatialCells`.

### 5.3e An open benchmark

Example: GSM8K uses its own lm-eval task `GSM8K_QWEN` ([bench.py](../plugins/numeric/src/llmchip_numeric/bench.py)); the other multiple-choice and generation benchmarks use lm-eval's tasks, the code benchmarks EvalPlus.

1. Data: add the benchmark with its lm-eval group or problem file to `CHOICE` (5 per subject in the quick set), `GENERATION` or `CODE` (5 per benchmark) in [benchdata.py](../plugins/numeric/src/llmchip_numeric/benchdata.py), with its license. `pack` reads the splits the lm-eval tasks use from this machine's Hugging Face cache; data that can only be read offline in another form (the CMMLU CSVs) is converted as `_cmmlu_loader` does.
2. Correctness: put the lm-eval metric and filter that mark a correct answer into `bench.METRIC`; a task of your own is a config dict returned by `task_specs`.
3. Checks: add `spec.accuracy(.quick).<benchmark>` for the quick and the full set to [flow-six](../plugins/flow-six/flow.json), with the question counts from `benchmark_data`'s metrics; the owner sets the allowed drop (requirements FR-QNT-04).
4. Tests: as [test_bench.py](../plugins/numeric/tests/test_bench.py) (comparison, L2 answering) and [tests/qwen-spec.spec.ts](../tests/qwen-spec.spec.ts) (the real model, opt-in).

### 5.4 A kernel API method

Example: `kernel.pause` (FR-AGT-23). Follow it through every layer:

1. Schema: `PauseParams` and `PauseResult` in [kernel.schema.json](../packages/kernel-api/schemas/kernel.schema.json), then `yarn gen:schemas`. `yarn check:generated` fails if you forget.
2. Contract: `'kernel.pause'` in `KernelMethods` and `KERNEL_METHOD_DEFS` ([kernel-api/src/index.ts](../packages/kernel-api/src/index.ts)). `packages/kernel-api/tests/index.spec.ts` checks that every named definition exists.
3. Handler: one line in `Kernel.handlers()` calling the component (`Scheduler.setPaused`). The `MethodHandlers` type makes a missing handler a type error.
4. Policy: add the method to `EXTERNAL_METHODS` ([security/policy.ts](../packages/kernel/src/security/policy.ts)) only if external clients may call it. `kernel.pause` is owner-only.
5. Front ends: a CLI case (`kernel pause`), a UI control ([Settings.tsx](../apps/web/src/views/Settings.tsx)). `llmchip call <method> '<json>'` works for every method without a dedicated command.
6. Tests: [tests/runs.spec.ts](../tests/runs.spec.ts) "holds new runs while paused and resumes them" and [tests/agents.spec.ts](../tests/agents.spec.ts) "stops agents at the emergency stop and at their budget".

A new event kind follows steps 1–2 for its payload (`KernelEventPayloads`, `KERNEL_EVENT_PAYLOAD_DEFS`). `EventStore.registerKind` maps a kind to an existing schema definition. Only kernel code can call it. Out-of-process plugins cannot define event kinds.

### 5.5 A projection (a new read model)

Example: `runsProjection` in [runs/scheduler.ts](../packages/kernel/src/runs/scheduler.ts), and `incidentsProjection`. Implement `Projection {name, version, setup, reset, apply}` and register it in the owning component's constructor with `events.registerProjection`. Bump `version` whenever `setup` or `apply` changes; the projection is then rebuilt from the whole log at the next start. Test: [packages/kernel/tests/events.spec.ts](../packages/kernel/tests/events.spec.ts) "rebuilds a projection from the log with identical state".

### 5.6 An LLM provider kind

Example: `openai-compatible` and `anthropic` in `createProvider` ([agents/providers.ts](../packages/kernel/src/agents/providers.ts)).

1. Add the kind to `LlmProviderConfig` in the schema.
2. Add a `case` that builds the request and goes through `send` / `gateway.check` before any byte leaves.
3. Read keys only through `secret(config.apiKeySecret)`.

Test against a local mock server, as [tests/providers.spec.ts](../tests/providers.spec.ts) does. Agent tests use the `scripted` kind.

### 5.7 An agent tool or an MCP tool

- **Agent tool**: add a `ToolSpec` to `TOOLS` and a `case` in `AgentRuntime.execute` ([agents/runtime.ts](../packages/kernel/src/agents/runtime.ts)). Check the task's grants first (`writeScope`, `runs`); `deny()` records the incident. Return a `ToolResult` with the content's true label. Tests: [tests/agents.spec.ts](../tests/agents.spec.ts) "blocks out-of-scope writes, deletions, shell commands and ungranted runs, recording incidents".
- **MCP tool**: add a `Tool` to `TOOLS` in [cli/src/mcp.ts](../packages/cli/src/mcp.ts). Usually `run: rpc('<method>')`. The method must be in `EXTERNAL_METHODS`, or the kernel refuses it with an incident. Test: [tests/mcp.spec.ts](../tests/mcp.spec.ts).

### 5.8 A Web UI page or text

Example: [views/Estimator.tsx](../apps/web/src/views/Estimator.tsx).

1. In [App.tsx](../apps/web/src/App.tsx), add the page to the `Page` union and to `PAGES`, and add its render branch.
2. Add every text key, including `nav.<page>`, to both catalogs in [i18n.ts](../apps/web/src/i18n.ts). Add kernel message codes to `CODES`.
3. Use only `KernelClient` calls.
4. Add the page to the `PAGES` list in [tests/web-ui.spec.ts](../tests/web-ui.spec.ts), which keeps its own copy.

The test checks catalog parity and unknown keys. It renders every listed page in both languages in headless Chromium, and fails if a catalog key appears verbatim on a page. Run `yarn build` first so `apps/web/dist` exists.

What the Flow page ([views/Flow.tsx](../apps/web/src/views/Flow.tsx)) shows for a stage is declared in [views/flowSteps.ts](../apps/web/src/views/flowSteps.ts). To show a handler's results there:

1. Add its `plugin/handler` to the stage's `runs`. A run judged by a stage's check is shown under that stage, whatever its handler.
2. Add the metrics to show as `facts`: a label key, a formatter, and optionally `good`, which colours the tile.
3. Add the outputs a person wants to open as `deliverables`.
4. Give the handler and its checks readable names in `RUN_TITLES` and `CHECK_TITLES`. The activity feed in the right panel ([views/activity.ts](../apps/web/src/views/activity.ts)) uses the same names.

Example: the S6 facts "core area" and "timing after place and route" read `core_area_um2` and `flow_setup_violations` of `eda-gf180/implement`.

A stage can also have a detailed demo view, opened from the "Detailed demo" button on its card. Write it as `views/steps/<stage>.tsx` and register it in `DETAILS` in `Flow.tsx`; the open detail is `selection.stage`, so the sidebar lists it under the selected project and the back bar (and the browser's back button) returns to it, as to every page and artifact visited before. Example: [views/steps/S0.tsx](../apps/web/src/views/steps/S0.tsx) reads the stage's `estimate.report` and lays it out as question and answer, inputs, the area and speed calculations, the reverse fit, the verdict, and a form that reruns the estimator outside the project. Build explanations from structured fields, not from a plugin's free text, so that both languages read naturally. `tests/web-ui.spec.ts` opens the S0, S1 and S2 views in both languages, from the flow page and from the sidebar, and returns with the back bar. [views/steps/S2.tsx](../apps/web/src/views/steps/S2.tsx) explains the numeric spec section by section, runs schemes through `runScheme` in `modelPipeline.ts`, compares every evaluated scheme, and makes a chip-path scheme the formal spec with `adoptScheme` (binds the `numeric-spec` and `l2-weights` slots, judges `accuracy.budget`, runs `infer`).

### 5.9 An in-kernel system plugin (Cordis)

Implement `InKernelPlugin {id, inject, provides, apply(ctx, kernel)}` ([plugin-host.ts](../packages/kernel/src/plugin-host.ts)) and load it only through `PluginHost.load`. Register everything as a Cordis effect so unloading cleans it up. Today only `KernelOptions.plugins` feeds the host, and only tests pass it ([packages/kernel/tests/plugin-host.spec.ts](../packages/kernel/tests/plugin-host.spec.ts)). Making a product plugin in-kernel also means adding a production loading path. That is a change to the architecture's §5 guards; agree it with the owner first.

### 5.10 A virtual chip for the test harness

Example: the toy process chip [tests/chips/toy_chip.py](../python/llmchip-vchip/tests/chips/toy_chip.py), which serves a toy model or wraps an existing chip and adds a cycle model; proved by `test_process_chip_wrapping_l2_is_bit_exact_with_the_numeric_golden` and the fault tests in [tests/test_harness.py](../python/llmchip-vchip/tests/test_harness.py).

1. Make a chip directory with `chip.json` (`format: llmchip-vchip/1`; `model.context`, `model.vocab`, `tokenizer`, `host`, `capabilities`, `clock_mhz`; README "A chip"). Hardware uses `backend: process` with a `command`; an in-process Python adapter uses `entrypoint: "module:Class"`.
2. Implement `VirtualChip` (`metadata`, `reset`, `prefill`, `step`, `close`; [interface.py](../python/llmchip-vchip/src/llmchip_vchip/interface.py)). Raise `ChipError("invalid_input", …)` for a token or position out of range. A process chip calls `llmchip_vchip.serve.serve(chip)`; an RTL simulation can instead speak the JSON-lines protocol of [adapters/process.py](../python/llmchip-vchip/src/llmchip_vchip/adapters/process.py) itself, and returns `host_cycles` when it plays the host frames.
3. Make the golden: `vchip new numeric` from the same model and spec, and name it in the chip's `policy` (`golden`, `quality.bit_exact: true`), with `reference` for agreement and the speed rule (`speed.tok_s_min`).
4. `vchip conform <chip>` must pass; then `vchip bench <chip>` judges it by its policy.

A new bundled backend is a class in `llmchip_vchip/adapters/` added to `registry.BACKENDS`, tested as the reference and numeric ones are (`test_conformance_of_the_baseline_chips`).

Through the kernel, a chip is not a directory: the plugin `vchip` builds the chips from a project's artifacts (§2.4), so a new generator's bundle runs there once `chips_from_artifacts` accepts its architecture. Proved by [plugins/vchip/tests/test_plugin.py](../plugins/vchip/tests/test_plugin.py) and [tests/vchip.spec.ts](../tests/vchip.spec.ts).

A hardware chip whose speed must be trusted (design L3) runs under one of the harness's own testbench bridges instead: real example `rtl` ([adapters/rtl.py](../python/llmchip-vchip/src/llmchip_vchip/adapters/rtl.py), [bridges/rtl_bridge.py](../python/llmchip-vchip/src/llmchip_vchip/bridges/rtl_bridge.py), [bridges/tb/vchip_tb.cpp](../python/llmchip-vchip/src/llmchip_vchip/bridges/tb/vchip_tb.cpp)), made by `vchip new rtl`, proved by [tests/test_rtl_vchip.py](../python/llmchip-vchip/tests/test_rtl_vchip.py). Any RTL bundle with the bundle's `tb_top` ports (clk, rst_n, the host SPI pins, busy) and the START/STATUS/READ protocol runs under it; a new generator only has to write such a bundle. A new bridge is added to `metrics.TESTBENCH_BACKENDS` only if it counts the cycles itself, on the pins.

---

## 6. Verification

### 6.1 Commands

Measured on the development machine (WSL2, Node 24.22, uv 0.13, Podman 3.4 with the pinned `iic-osic-tools` image present), 2026-10-19:

| Command | Verifies | Observed |
|---|---|---|
| `yarn check:workspace` | packages, references, `workspace:*` deps, vendor manifest and licenses | "5 packages, 9 vendored packages" |
| `yarn check:generated` | `kernel-api/src/generated` matches the schema | "up to date (2 files)" |
| `yarn check:python` | ruff lint and format, pytest of the SDK, the test harness and all Python plugins | 306 passed, 1 skipped, 17 min 49 s with the EDA image, the sandbox image and a GPU (2026-10-26, R5); at R1, 165 passed, 1 skipped, 6 min 43 s; at R0, 164 passed, 1 skipped (R0 removed the `packager` tests); before, 158 passed, 1 skipped, 4 min 52 s (2026-10-22); 32 of them the test harness, 7 its L3 simulator tests (skipped visibly without the image; the skip is the check of the behaviour without it) |
| `yarn typecheck` | project references, then sources, tests, scripts and the Web UI | clean |
| `yarn build` | `lib/` of every package, `apps/web/dist` | clean |
| `yarn test` | all Vitest specs (below) | 36 files passed, 2 skipped (m4-flow, qwen-spec); 137 tests passed, 3 skipped (m4-flow, S0–S7 of `pipelines-flow` and qwen-spec, all opt-in; 2026-10-30) |
| `yarn verify` | all of the above in order; required before every commit | exit 0 (2026-10-24); with the images present, Python checks 9 min 15 s, Vitest 769 s (R3) |
| `LLMCHIP_FULL_FLOW=1 yarn test tests/m4-flow.spec.ts` | full S0–S7 on the proxy, and the lineage from the GDS to the weights | see §6.3 |
| `yarn llmchip events verify` / `artifact verify` | hash chain, stored content against hashes | `ok: N … checked` |
| `yarn vchip bench CHIP [--reference R] [--golden G]` | the test harness on a chip: generation over a prompt set, quality and speed, the chip's policy, a run directory | stories260K numeric (int8) chip against its reference, 20 prompts to the end of a 64-token context: next-token agreement 0.992, identical generations 0.65, perplexity 3.167 (reference 3.159), 11 s (2026-10-22). `vchip conform` passes on the reference and numeric chips of stories260K and on the reference chip of tiny Qwen3 |
| `yarn vchip new rtl --proxy --id spatial-5k`, then `vchip conform` / `vchip bench spatial-5k` | design L3: the `taalas-spatial/1` RTL of the 5K proxy under the pin-level testbench, against its L2 golden and float reference | Verilator build 8 s; `conform` T-01–T-19 pass (T-06 n/a), 12.8 min, `--quick` (without T-18/T-19) about 2 min; `bench` on 20 prompts to the end of the 64-token context: bit-exact with L2 (tokens, logits, all observation points), 1,327 cycles per token at every position, 753 host cycles, 3,846 tok/s at 8 MHz, measurement trusted, verdict pass, 10.6 min (2026-10-22) |
| `yarn demo [--until S0..S7] [--arch spatial]` | drives project `demo` through the positive path of the full flow against the running kernel, up to the given stage, for the Flow page | before R0 (with S8 and approvals) S0–G4 passed, 2241 s (about 37 min) with containers at 16 CPUs / 12 GB; not yet rerun in full after R0 |
| `yarn demo --model PATH [--bits N] [--process ID]` | starts a project from a downloaded model: S0 (import, census, estimate) and S1 (graph recovery, L0, comparison) | all census, graph and comparison checks pass: SmolLM2-135M 11 s, Qwen3-0.6B 46 s, DeepSeek-R1-Distill-Qwen-1.5B 89 s, stories260K 1 s; largest relative logit error 6.8e-6, identical greedy tokens; all infeasible on the 2.4 mm² GF180 die |

Without the container images, `calibration`, `m3-flow`, `ui-latency` and `containers` skip visibly. `yarn verify` then finishes much faster but proves less. A skipped test is not a pass (DP-10). Say which ones skipped when you report.

### 6.2 What each test proves

| Spec | Behavior | Requirement / AC | Runs |
|---|---|---|---|
| `packages/kernel-api/tests/index.spec.ts` | the schema compiles as strict JSON Schema 2020-12; every method and payload names an existing definition; label order | IF-1, IF-2 contracts | always |
| `packages/kernel/tests/events.spec.ts` | hash chain, tamper detection, atomic transactions, redaction, projection rebuild and catch-up | K1, FR-TRC-01 | always |
| `packages/kernel/tests/artifacts.spec.ts` | content addressing, idempotence, label raise, lineage, mark-deleted, verify, temp cleanup | K2, FR-TRC-02, FR-SEC-02 | always |
| `packages/kernel/tests/config.spec.ts` | config layers, secret encryption, redaction | K10, NFR-07 | always |
| `packages/kernel/tests/plugin-host.spec.ts`, `tests/vendor.spec.ts` | Cordis host guards: hung or throwing plugins, unload order | ADR-001 | always |
| `packages/kernel/tests/main.spec.ts`, `tests/cli.spec.ts` | `harnessd` and `llmchip` command lines | FR-INT-07 | always |
| `tests/kernel-api.spec.ts` | token, param validation, uploads, gapless subscriptions, no secrets in logs, single kernel per home | IF-1 | always |
| `tests/runs.spec.ts` | queueing checks, crashes, invalid outputs, timeout, cancel, pause, orphan kill and retry | K4, AC-M1-04 | always |
| `tests/resources.spec.ts` | scheduling by capacity: a run that does not fit waits in queue order (and the runs behind it too), small runs side by side, GPUs to one run at a time (with two GPUs too) and hidden from the others, a run larger than the whole capacity runs alone | NFR-15 | always |
| `tests/checkpoint.spec.ts` | a checkpoint is stored at once as an artifact of the running run; a crashed run leaves its checkpoint and the retry resumes with the input `resume`; a checkpoint outside the work directory is refused | FR-TRC-10, IF-2 | always |
| `tests/pipelines.spec.ts` | plan and missing inputs, slots bound by earlier nodes in `all`, development pipeline (enter, run, bind, judge), tests reusing development runs, a failure stays failed and its dependents are skipped, resume after the fix, cancel, resume right after a cancel; one running pipeline per stage; hard-constraint slots not overwritten by pipelines; a flow pack with a dependency cycle refused; after a graceful kernel stop or a kill the pipeline continues and the interrupted node resumes from its checkpoint | FR-PRJ-07/08, FR-TRC-10, AC-15 | always |
| `plugins/numeric/tests/test_l2_torch.py` | the fast L2 equals the reference L2 bit for bit: logits, every observation point and the overflow counts, under three roundings, two overflow modes and a narrow accumulator, on llama2.c, Qwen2 and Qwen3 graphs; on the GPU when there is one; the KV cache token by token equals one pass (both implementations); a batch computes every sequence as alone, slot reuse included; the exact products equal integer products on extreme values; TF32 is off | FR-QNT-03 | always; the GPU case skips without CUDA |
| `plugins/arch-taalas/tests/test_slice.py` | the slice levels of a decoder layer: S-1 one Q head, S-2 adds one KV head and the O columns of whole heads, S-3 adds MLP channels; the weight and bit counts, the observation points each level reproduces, and the refusal of a column range that is not a whole number of weight groups | FR-MDL-06 | always |
| `plugins/rtl-taalas/tests/test_tile_engine.py` | the pipelined datapath against a Python reference: random activations and 4- and 8-bit levels, four (MACS, WORDS, BITS) combinations, the engine's accumulators compared lane by lane with the sum computed here; the testbench is Verilog run by Verilator `--binary` | FR-RTL-01/02 | always; skips visibly without the tools directory |
| `plugins/rtl-taalas/tests/test_attention_reference.py` | the attention reference against `GraphL2.attention`: the scale is multiplied before the shift (11,585 for headDim 8, not a power of two), the KV cache is the `(S, kv_heads * hd)` layout of every cached position, and the shifts use `arith.rshift`'s rounding; head-by-head, several lengths, and a truncating shift as the negative control | FR-RTL-01/02 | always |
| `plugins/rtl-taalas/tests/test_tile_attention.py` | `tile_attention` against the reference: DOT, the registered exp-table read and its wait state, the restoring division, WSUM and EMIT, for one and several positions, a grouped-query head pair and full int16 vectors; Verilator `--binary`, the exp table is `tile_lut` itself | FR-RTL-01/02 | always; skips visibly without the tools directory |
| `plugins/rtl-taalas/tests/test_tile_qfull_top.py` | `tile_qfull_top`: one engine per layer on a shared bank set and activation vector, the serial `done` chain and the pipelined first-row chain, the final layer's rows equal to the matvec reference | FR-RTL-01 | always; skips visibly without the tools directory |
| `plugins/rtl-taalas/tests/test_tile_normalization.py` | `tile_rmsnorm` against `GraphL2.rmsnorm` (attn_norm DIM 32 and per-head q_norm DIM 8) and `tile_rope` against `GraphL2.rope` (both Q and K, positions 0/1/5/15); the tests exposed and now lock three template bugs: the registered LUT needs a wait state, the rsqrt exponent must scan the highest set bit, and the norm product needs its 64-bit intermediate | FR-VER-04, FR-RTL-01 | always; skips visibly without Verilator |
| `plugins/rtl-taalas/tests/test_tile_linear.py` | the generic bit-exact linear on o_proj (input v kind, output x kind) and the saturated int16 residual add, both against `GraphL2` | FR-VER-04, FR-RTL-01 | always; skips visibly without Verilator |
| `plugins/rtl-taalas/tests/test_tile_attention_kv.py` | `tile_attention` over two real `tile_kv` caches: post-RoPE K, v_proj V, positions 0/1/3/7/15, fq=fk=13 (the q_norm/k_norm fraction), compared bit for bit with `GraphL2.attention` | FR-VER-04, FR-RTL-01 | always; skips visibly without Verilator |
| `plugins/rtl-taalas/tests/test_tile_mlp.py` | the MLP gate: `tile_silu` (clipped sigmoid index, Q15 table, two rounding shifts) and gate/up/down projections against `GraphL2` | FR-VER-04, FR-RTL-01 | always; skips visibly without Verilator |
| `plugins/rtl-taalas/tests/test_tile_layer.py` | the chained Q-full layer harness: attn_norm, q/k/v, per-head q_norm/k_norm, RoPE, two `tile_kv` caches, attention, o_proj, residual, mlp_norm, gate/up, SiLU, down_proj and the second residual, for layers 0 and 1, with all 17 observation points compared bit for bit against `GraphL2` | FR-VER-05, FR-RTL-01 | always; skips visibly without Verilator |
| `plugins/rtl-taalas/tests/test_tile_coverage.py` | Verilator line coverage of the chained layer harness, at least 90% (currently 96%) | FR-VER-05 | needs Verilator |
| `plugins/rtl-taalas/tests/test_formal.py` | SymbiYosys BMC of `tile_residual` against its saturating sum, with a deliberately non-saturating module that must fail | FR-VER-05 | needs SymbiYosys |
| `plugins/rtl-taalas/tests/test_mutation.py` | MCY mutation campaign on `tile_residual`: 50 mutants, directed/boundary/random testbench, equivalence-classified survivors; score must be ≥ 95% (currently 100%, 12 equivalent as `NOCHANGE`) | FR-VER-05 | needs MCY, Yosys and Verilator |
| `plugins/rtl-taalas/tests/test_dft.py` | DFT readiness audit of the generated bundle (reset on every non-RAM sequential block, only `clk`/`host_sclk`, no latch idioms, ROM-only `initial` blocks) plus gated-clock/latch/non-ROM-initial/missing-reset negative controls | FR-VER-05 | always; skips visibly without a bundle |
| `plugins/syn-yosys/tests/test_syn.py` | Yosys synthesis of the S-1 slice on sky130A, GF180MCU and IHP SG13G2 in the pinned EDA image, OpenSTA timing at 100 ns and a 1 ns negative control, Fault scan-chain insertion for sky130 (1553 internal + 142 boundary flops), Fault stuck-at ATPG on the residual DFT target (99.16% / 96.79% / 98.65% on the three PDKs) with a tied-output negative control, wrong-DFF-list negative controls on all three PDKs, and an unknown-PDK negative control | FR-SYN | needs Podman and the EDA image |
| `tests/tile-syn.spec.ts` | the SYN stage and its three `syn.synth.*` checks on the `syn-yosys` handler | FR-SYN | always |
| `plugins/rtl-taalas/tests/test_full_model.py` | the tiny full-model logits path: RTL layer 0 → RTL layer 1 (consuming the RTL residual) → final_norm → lm_head, 128 int32 logits compared with `GraphL2` | FR-VER-05, FR-RTL-01 | always; skips visibly without Verilator |
| `plugins/rtl-taalas/tests/test_tile_slice.py` | the functional S-1 slice: the generator's weight banks, group mantissas and biases, real `attn_norm` activations from `GraphL2`, the q_proj head-0 rows compared bit for bit on four tokens; Verilator `--binary` | FR-VER-04, FR-RTL-01/02 | always; skips visibly without the tools directory |
| `plugins/rtl-taalas/tests/test_verify.py` | the R4 plan (runnable and blocked items), the clock-domain check (`clk` plus the documented `host_sclk`), and the runnable coverage/formal/mutation/DFT items and the not-run reason for the full-chip equivalence item | FR-VER-01/04 | always |
| `plugins/rtl-taalas/tests/test_rom_macro.py` | the hard-macro view built from `rom_bank0..3.hex`: the black box (no contents), the simulation model (`$readmemh`), the provisional LEF and area arithmetic, and Yosys equivalence between the simulation model and `tile_rom` at the macro's parameters | FR-RTL-02, FR-SGN-07 | always; the equivalence part skips visibly without Yosys |
| `plugins/rtl-taalas/tests/test_tile.py` | the `taalas-tile/1` RTL bundle: the ROM mapping rule (`taalas-tile-rom/2`, row major: one word is a row's 128 consecutive columns) round-trips the quantized levels of int4 and int8 matrices, including columns not a whole number of chunks and words wider than 64 bits; the generated S-1 slice writes one bank per matrix with its `$readmemh` contents and the manifest with every hash; `tile_check` passes all five checks - ROM round trip, the rules (no `lint_off`, a reset in every clocked block, no functions), the three linters (Verilator `-Wall`, Verible and slang-tidy) with no warnings, Yosys `check -assert`, and the three ROM views (behavioural, standard-cell and via-programmed) equal by Yosys's equivalence engine (`equiv_make`/`equiv_simple`/`equiv_induct` after `async2sync`); a via mismatch is the negative control - and a corrupted bank, an injected `lint_off` or a view-2 netlist from another bundle is reported; the bundle also carries the numeric spec's rsqrt, exp and sigmoid tables as `$readmemh` files (`tile_lut`), by entry count and digest, the integer RMSNorm template (`tile_rmsnorm`: one multiplier and one accumulator reused over DIM cycles, the even exponent, the Q15 rsqrt table, half-up shift and saturation) the RoPE template (`tile_rope`: the two table layouts - element j with j+HD/2 and interleaved pairs - , a pair per two cycles, Q14 tables, half-up rounding and saturation) and the KV cache template (`tile_kv`: one row per KV head and position, one write and one read port), the pipelined datapath (`tile_engine`: S0 address, S1 word and activation, S2 group sums, S3 group accumulators, one word per cycle) and its row-major mapping (`taalas-tile-rom/2`: a word is one row's 128 consecutive columns, the array's 128 products summed by a tree of eight groups) | FR-RTL-01/02/03/04, FR-SGN-07 | always; the lint and synthesis parts skip visibly without the tools directory |
| `plugins/arch-taalas/tests/test_tlm.py` | the transaction-level model of `taalas-tile/1`: it walks the schedule's phases for a token sequence, records the L2 observation point of every phase that produces one (q, k, v, h1, h3, x), is deterministic, and its cycle count equals the chip spec's; a chip whose cycles disagree is caught | FR-ARC-03, plan 7.1 (TLM = L2) | always |
| `plugins/arch-taalas/tests/test_tile.py` | `taalas-tile/1`: the schedule table is the cycle model (one ROM word of `macs` weights per cycle, a matrix takes `columns x ceil(rows/macs)`), one engine per layer or one engine for a slice, the output projection segmented over the engines, MACs that do not divide the shapes refused, and a tampered schedule caught by `verify_spec` | FR-ARC-01/02 | always |
| `plugins/numeric/tests/test_gptq.py` | calibrated quantization: the PyTorch float graph gives the NumPy interpreter's logits; GPTQ keeps the spec's weight format (levels, 15-bit mantissas, one exponent) and, on correlated inputs, changes the product XW less than rounding on the same grid does; a calibrated spec with per-matrix formats loads as the L2, runs, and stores each matrix at its own width | FR-QNT-01 | always (torch, CPU) |
| `plugins/numeric/tests/test_diagnose.py` | the L2's observation points (dequantized) are the float graph's; a diagnosis names the first divergent token and the layer with the largest deviation (or records that the two agree for the whole window) | FR-QNT-09 | always (torch, CPU) |
| `plugins/numeric/tests/test_bench.py` | comparison: drop and paired interval, the questions only one model answered, refusal of reports of other questions, sets, data, models or settings; the L2 answering lm-eval: log-likelihoods equal the reference L2's, greedy generation equals the reference and stops at a stop string; the evidence of `spec.l2.fast` finds no mismatch on a correct fast L2 and reports mismatches in all three comparisons on a fast L2 that rounds wrongly (negative control) | FR-QNT-03/04 | always (tiny Qwen3) |
| `plugins/numeric/tests/test_benchcode.py` | the code sandbox: the reference solution passes, a wrong one fails, code that tries to write a host file fails and leaves no file on the host; cancelling (SIGTERM) removes the container within the 2 s grace period | FR-QNT-04, AGENTS.md "Adding a plugin" 6 | needs Podman, the sandbox image and the EvalPlus problems in the tools directory; skipped visibly otherwise |
| `tests/qwen-spec.spec.ts` | the SPEC step of Qwen2.5-0.5B-Instruct: once the owner gave the model and the benchmark data, every node of the development pipeline runs, `model.*` and `spec.l2.fast` pass, the 7 quick-set checks reach a verdict; the required tests reuse the development runs | FR-MDL, FR-QNT-03/04/08 | needs `LLMCHIP_QWEN=1`, a GPU, and the model, benchmark data and sandbox image of the tools directory; 1 passed in 1277 s (2026-10-24), and in 1211 s after the flow-pack fix (2026-10-30) |
| `tests/pipelines-flow.spec.ts` | bundled plugins: on a fresh project the development and the tests of every step of S0–S3 pass, the tests reuse the development runs; `all` runs the development and all tests of S0–S7 | FR-PRJ-07/08 | S0–S3 always (needs `.venv`); S0–S7 only with `LLMCHIP_FULL_FLOW=1` **and** the EDA image; 1 passed in 1967 s (2026-10-24) |
| `tests/recovery.spec.ts` | acknowledged writes survive repeated SIGKILL | NFR-04 | always |
| `tests/workflow.spec.ts` | indeterminate on incomplete parse, crash or unqualified capability; no waivers: a failure stays a failure and opens an incident, only a fixed design passes, the history stays; stages entered without gates, the old methods are gone; a home written before R0 (projects on an old flow pack, approval/waiver/export events, `waived` verdicts, migration events with `gates`) still opens, replays and verifies its hash chain; a newly registered flow pack that still declares gates is refused; branches | AC-M1-03, architecture ADR-011 | always |
| `tests/agents.spec.ts` | iteration loop on change sets, permissions, loop and budget stops, fake-PDK egress, handoff, lowered standards | AC-M1-02/07/08/09/13 | always |
| `tests/mcp.spec.ts`, `tests/providers.spec.ts` | MCP boundary; OpenAI-compatible and Anthropic adapters against mock servers | FR-INT-05/06 | always |
| `tests/estimator.spec.ts` | three models, full breakdown; missing data gives "insufficient data" and the S0 check indeterminate | AC-M1-10 | always; **fails** (does not skip) without `.venv` |
| `tests/m2-flow.spec.ts` | S0 → S3 on stories260K, each stage judged by its checks; earlier verdicts do not judge a new numeric spec, and the old chip spec fails against it | M2, AC-04 | always; **fails** without `.venv` |
| `tests/m4-kernel.spec.ts` | hard-constraint slots bound only by humans; a migration records the stage to restart and refuses an unknown stage | FR-SGN-02b, FR-MIG-03 | always |
| `tests/model-import.spec.ts` | import of a tiny Qwen3 in the real Hugging Face layout, census, estimate from the census, graph, L0, the full test suite against transformers; llama2.c against the run.c port; a project's own proxy weights; a model without config.json reported, not guessed | FR-MDL-01/03/05/06 | always; needs `.venv` |
| `plugins/model-llama/tests/test_graph.py` | Llama (tied/untied), Mistral with sliding window, Qwen2, Qwen3 against transformers; llama2.c against the run.c port; duplicated tied output; truncated file; unsupported models | FR-MDL | always |
| `python/llmchip-vchip/tests/test_harness.py` | the virtual-chip test harness: invalid manifests unavailable with reasons, tokenizer round trip and unknown text refused, the reference chip equal to the run.c port, prefill and step agreeing, context boundary and invalid input, stop token, SPI frame cycles and tok/s, determinism, bit-exact against the numeric golden through a process chip, policy verdicts, crashes, hangs and garbage recorded without stopping the run, a timeout killing the chip's whole process group, no speed from a chip that reports no cycles, conformance without a usable prompt incomplete rather than pass, two model families | design T-01–T-14 (owner draft); FR-VER-04/05 | always (`yarn check:python`) |
| `python/llmchip-vchip/tests/test_rtl_vchip.py` | design L3: `vchip new rtl` makes the three chips; pinned image and measurement are validated; a process chip that claims a testbench is not trusted; with the image, the RTL chip conforms with `--quick` (T-15–T-17 pass; T-18/T-19 `not_run`, so the result is incomplete, not pass), is bit-exact with L2 on 4 prompts × 64 positions with trusted cycles equal to the design and > 1000 tok/s, and a killed bridge leaves no container | design T-15–T-19 | simulator parts need the EDA image (skipped visibly); all 20 prompts with `LLMCHIP_VCHIP_L3=1` |
| `tests/kernel-footprint.spec.ts` | startup time and idle RSS of harnessd | NFR-01 (kernel part) | Linux |
| `tests/arch-perf.spec.ts` | the S3 speed is exact: for the proxy in four microarchitecture options (SPI / SQI, buffers none / attention / all), the busy cycles of every RTL step equal the chip spec's prediction position by position, and the RTL equals L2 | FR-ARC-04/05, FR-EST-07 | needs the EDA image (790 s on 2026-10-20) |
| `tests/spatial-flow.spec.ts` | the spatial engine from S2 to S5 on the proxy: designed (cycles from the fixed schedule, ≥ 1000 tok/s, die from the measured cells, `verify_spec` recomputes the cycles, the cycle target judged), generated with the round trip read back from the netlist, lint-clean, and every item of `verification-plan-spatial` passed: L3 equals L2 bit for bit with observation points read over the host interface, busy cycles equal at every position, the bit-serial cells unit-tested, line coverage ≥ 90% | FR-ARC-04/05, FR-RTL-01/02/03, FR-VER-04 | needs the EDA image (261 s on 2026-10-21) |
| `tests/numeric-schemes.spec.ts` | any recovered graph (tiny Qwen3, the proxy from its own artifacts) quantized and its L2 evaluated against L0, a narrow accumulator overflowing, `numeric.accuracy` judged, a graph spec refused by the vector engine; with the EDA image, a half-even / wrap / 24-bit-accumulator scheme from the numeric spec to the chip spec, RTL equal to L2 on the ISA simulator and in Verilator, the ALU tested in those modes | FR-QNT, FR-RTL-01, FR-VER | graph part always; RTL part needs the EDA image (about 4 min) |
| `plugins/vchip/tests/test_plugin.py` | the plugin's handlers without the kernel: chips built from a project's artifacts (inputs without a file suffix, as the kernel hands them over); the design's cycles and clock from a chip spec only of exactly this design (other weights, no hash, other dividers or architecture refused), a clock given as a param marked as such; L2 against the float reference of the Model IR, equal to the reference made from the checkpoint; reports without policy or verdict, unmeasured metrics absent; an RTL bundle of other weights or of the vector engine refused; conformance as counts; with the image, the RTL bit-exact with L2 with cycles counted by the testbench (1,327 = the design, > 1000 tok/s), quick conformance leaving T-18/T-19 unrun | design L4; ADR-010, AP-7 | always; RTL parts need the EDA image (skipped visibly) |
| `tests/vchip.spec.ts` | the plugin through the kernel: L2 of the proxy against its float reference on the 20 prompts of `evalset-proxy`, reports as artifacts without verdicts, `vchip.l3` indeterminate on L2 (not hardware), `vchip.conformance` pass on the 20 prompts and indeterminate on 3, typed-in prompts; with the image, the spatial RTL from `rtl-taalas generate`, bit-exact with L2, cycles and clock from the chip spec, and a cancelled run leaving no simulator container | design L4 | L2 part always (needs `.venv`); RTL part needs the EDA image; `LLMCHIP_VCHIP_L3=1` benches all 20 prompts and `vchip.l3` must pass (2026-10-22: 3 tests passed in 661 s; the full `vchip conform spatial-5k` after this change: T-01–T-19 pass, T-06 n/a, 12.7 min, no container left) |
| `tests/web-ui.spec.ts` | catalog parity; every page in both languages in a browser; the S0, S1, S2 and S3 detail views, S3 also for a spatial-engine project (circuits, area blocks, schedule, design space); the virtual-chip page with a conformance report and a generation judged indeterminate by `vchip.l3` | AC-M1-12 | browser part needs `apps/web/dist` and Chromium |
| `tests/containers.spec.ts` | no network, read-only inputs, container removed on cancel | IF-3 | needs `python:3.12-slim` image |
| `tests/calibration.spec.ts` | every storage candidate ends available or unavailable; stage entry on views | AC-M15-01/02/03 | needs EDA image |
| `tests/m3-flow.spec.ts` | verification plan, injected defect localized (the verdict judges only the faulty bundle) | M3, AC-03/09 | needs EDA image |
| `tests/ui-latency.spec.ts` | p95 < 200 ms for UI queries during EDA runs | NFR-03 (kernel side) | needs EDA image |
| `tests/m4-flow.spec.ts` | S0–S7, DRC/LVS/STA, negative timing and SDF cases, lineage from the GDS to the weights, migration assessment | M4, AC-02/05/06/08/12/14 | only with `LLMCHIP_FULL_FLOW=1` **and** the EDA image |

### 6.3 Opt-in full flow

`LLMCHIP_FULL_FLOW=1 yarn test tests/m4-flow.spec.ts` runs the physical tools. Run it after any change to `rtl-taalas`, `eda-gf180` or the signoff checks. Result on 2026-10-20, on the quad-SPI design (before R0, with S8 and G4): passed (1 test, 2132 s, about 36 min, with Podman 3.4 on WSL2). After R0 (S0–S7, no gates, lineage with tool version and verdicts), rerun on 2026-10-24: passed (1 test, 2070 s, about 34.5 min).

After any change to the spatial generator (`llmchip_rtl/spatial/`) or to the test harness's RTL bridge (`llmchip_vchip/bridges/`), run the full L3 conformance once (hard constraint, owner decision 2026-10-22; AGENTS.md, "Containers"): `yarn vchip new rtl --proxy --id spatial-5k` and `yarn vchip conform spatial-5k` without `--quick`, or `LLMCHIP_VCHIP_L3=1 uv run --frozen pytest python/llmchip-vchip/tests/test_rtl_vchip.py`. Results on 2026-10-22: see the `vchip` rows of §6.1. Rerun on 2026-10-25 after the bridges' container cleanup was fixed (below, §7.2): T-01–T-19 pass, T-06 n/a, about 20 minutes under load, no container left behind.

`LLMCHIP_QWEN=1 yarn test tests/qwen-spec.spec.ts` runs the SPEC step of Qwen2.5-0.5B-Instruct end to end: the development pipeline (import, graph recovery, the comparison with transformers, the quantization, the fast L2 against the reference L2, the quick set on L0 and on L2, their comparison), then the required tests, which judge the development runs again. Result on 2026-10-24 on the RTX 5060 Ti: passed (1 test, 1277 s, about 21 min); after the flow-pack fix of 2026-10-30 (the design judged by its own `verify` node, `rtl.tile.equivalence`, the tile TLM reached) it passed again (1 test, 1211 s, about 20 min): `spec.design` and `spec.tlm` reached their verdicts for the first time, and every required test reused its development run. With plain rounding (RTN, R2a) all 7 quick-set checks fail, the expected result of an uncalibrated W4; with GPTQ (R2b, 2026-10-25) MMLU becomes indeterminate and six still fail. Neither is a reason to change a check:

| Quick set (900 questions) | L0 % | L2 RTN % | L2 GPTQ % |
|---|---|---|---|
| MMLU | 40.7 | 36.1 | 40.4 |
| CMMLU | 50.4 | 34.0 | 31.3 |
| C-Eval | 43.8 | 39.2 | 25.4 |
| GSM8K | 40 | 0 | 20 |
| IFEval | 40 | 20 | 0 |
| HumanEval+ | 40 | 0 | 0 |
| MBPP+ | 60 | 0 | 0 |

The GPTQ row is the spec `c030b380` (W4, groups of 128, 64 windows of 2048 tokens of WikiText-2, 169 matrices, 2 min 37 s on the GPU); its MMLU drop is 0.35 points with the 95% interval [−5.96, 7.02], so the check is indeterminate (the interval's upper bound lies above the 2-point allowance), and the other six fail. Calibration raised MMLU by 4 points over RTN but lowered C-Eval by 14 and CMMLU by 3: a W4 model calibrated on English text loses Chinese accuracy. That is measured, recorded, and neither excused nor hidden by a changed standard; the diagnosis of the wrong answers (below) is the tool to locate it.

The generation benchmarks score 5 questions each in the quick set, so their percentages move in steps of 20 points. The full set was run on L0 on 2026-10-24 (2098 s: MMLU 327, CMMLU 237, C-Eval 36, GSM8K 662, IFEval 562, HumanEval+ 105, MBPP+ 170; bf16, 2048 tokens of context), next to the numbers Qwen publishes for the same model:

| Benchmark | Questions | L0 % | Qwen % | Setting |
|---|---|---|---|---|
| MMLU | 14042 | 42.1 | 47.5 | Qwen's is the base model, 5-shot; not published for the Instruct model |
| CMMLU | 11582 | 47.4 | — | not published |
| C-Eval | 1346 | 43.9 | — | the validation split; not published |
| GSM8K | 1319 | 48.9 | 49.6 | zero-shot with Qwen's instruction, boxed answer, greedy |
| IFEval | 541 | 26.8 | 27.9 | strict prompt-level accuracy |
| HumanEval+ | 164 | 34.1 | 35.4 | Qwen's is HumanEval, base tests only |
| MBPP+ | 378 | 41.5 | 49.6 | Qwen's is MBPP, base tests only |

The full set on L2 (the calibrated W4 spec `c030b380`) was started on 2026-10-25; all seven benchmarks finished on 2026-10-25 (01:15 UTC), and every one of them fails its allowance (FR-QNT-04: 2 points for the multiple-choice benchmarks, 3 for GSM8K and IFEval, 5 for the code benchmarks):

| Full set, L2 (partial, 2026-10-25) | L0 % | L2 % | Drop (95% interval) | Allowance |
|---|---|---|---|---|
| MMLU | 42.05 | 36.97 | 5.08 [4.21, 5.98] | 2 |
| CMMLU | 47.39 | 30.56 | 16.83 [15.71, 17.94] | 2 |
| C-Eval | 43.91 | 27.19 | 16.72 [13.15, 20.28] | 2 |
| GSM8K | 48.90 | 16.45 | 32.45 [29.64, 35.25] | 3 |
| IFEval | 26.80 | 19.04 | 7.76 (strict, 541 questions; no paired interval recorded here) | 3 |
| HumanEval+ | 34.15 | 5.49 | 28.66 (pass@1, 164 problems; no paired interval recorded here) | 5 |

| MBPP+ | 41.53 | 13.23 | 28.30 (pass@1, 378 problems; no paired interval recorded here) | 5 |

The conclusion is that the provisional W4 spec, even calibrated by GPTQ, does not meet the accuracy criterion: mixed precision or a wider format for the sensitive matrices is the way R2 continues, not a lowered standard.

GSM8K, IFEval and HumanEval+ are within about one point of the published numbers. MBPP+ is 8 points lower because EvalPlus's plus tests are stricter than the base tests Qwen scores; the two are not the same benchmark. MMLU, CMMLU and C-Eval have no published number for the Instruct model. These L0 numbers are the baseline the full-set comparison judges L2 against; the full set on L2 was run on 2026-10-25 (§6.3 and §7.2).

---

## 7. Current limitations

### 7.1 Not implemented

| Gap | Requirements |
|---|---|
| Tauri desktop shell, Windows installer, dedicated WSL distro, first-launch onboarding, automatic image pull | FR-UI-01, FR-UI-03, NFR-08, NFR-13, AC-M1-01, AC-01, ADR-002, ADR-005 |
| CHARTER.md generation and history, stage playbooks, sample project | FR-PRJ-02, FR-PRJ-06, FR-AGT-11 |
| Project git working directory; merging a change set records state only | FR-TRC-09, architecture §6.2–6.3 |
| Training the reference model (owner-deferred; the proxy stands in) | FR-QNT-05, requirements §11.1 |
| Graph recovery outside the Llama family (GPT-2, MoE, …), of GGUF (census only), and with RoPE scaling (e.g. Llama 3.1/3.2); download from Hugging Face inside the workbench | FR-MDL-01, FR-MDL-02, FR-MDL-03 |
| Project export and import, reproduce command, audit export | FR-PRJ-05, FR-TRC-07, FR-TRC-08 |
| Artifact purge, GC and retention | architecture §4.2, FR-AGT-20 |
| Externally launched coding agents managed by the kernel | FR-AGT-03 |
| `images/` build definitions | architecture §14 |

### 7.2 Partial

- **Estimator**: multi-chip is a chip count only (FR-EST-06). No sweep or Pareto (FR-EST-08). Calibration does not update the model automatically (FR-EST-10).
- **Calibration** covers SRAM 512×8 only of the four configurations (FR-EST-13). The sky130 and IHP sg13g2 profiles are new (2026-10-25): their cell areas come from the PDKs' liberty files, but their ROM and SRAM densities in `library-130nm` are empirical, scaled from the GF180MCU calibrated value, and both profiles say `calibration: not run`. A slice of 57,344 weights estimates to 2.65–5.21 mm² (typ 3.79) on sky130 and 4.05–7.25 mm² (typ 5.66) on IHP at 128 MACs, against the 3.0 mm² placeholder prerun die: conditionally feasible on sky130, infeasible on IHP until the densities are calibrated.
- **Verification**: only `rtl-functional-sim` is a declared, qualified capability. Gate-level simulation has no capability gate. SDF simulation runs, but `sdf.timing` cannot pass (§1.3, FR-VER-01/03, AC-12 positive case).
- **Storage contents check** (FR-SGN-07): the mapping is reversible and round-trip checked at generation. No signoff check compares macro contents, because the design uses a standard-cell ROM.
- **Physical**: the die is 1.2×2 mm, not 2 mm² (owner decision, requirements v0.6). The design subject is the untrained proxy. The spatial engine does not fit that die; it needs 5.0 × 3 mm by its measured cells (owner decision 2026-10-20: the die grows as needed; the requirements' §11.1 is updated once the size is confirmed by placement). It has not been implemented physically yet.
- **Spatial engine** (measured 2026-10-21, proxy, 8 MHz): 1,327 cycles per token (6,029 tok/s); with the host's START and STATUS frames 2,059 cycles (about 3,900 tok/s). Synthesized cells 5.96 mm²: KV rings 2.09, matrix circuits 1.94 (serial adders 0.80, row outputs 0.75, delay lines 0.39), core logic 0.97 and its 16 softmax dividers 0.05, core registers 0.42, attention multipliers 0.41, tables 0.07, host 0.02. The core logic per dimension and the table cost are extrapolated for other shapes. The KV registers get their reset value from a zero pass after reset (AGENTS.md, owner decision 2026-10-21); the gate-level proof is due with the physical implementation. The generator implements dim a power of two (head size at least 4), 2 heads, 1 KV head and a context of 64, any number of layers and any hidden size up to 255, and refuses other shapes with a reason. Without a `die` (or `targetAreaMm2`) the area limit is the profile's prerun area (2.4 mm²), which the spatial engine does not fit; the PPA states the die it suggests (`suggestedDie`), and the design is made with that die until the prerun constraint is updated (P4).
- **Migration**: the assessment works, and a confirmed one records the stage to restart. Downstream artifacts and verdicts are not marked invalid (FR-MIG-03, FR-TRC-05). Restarting at S3 or earlier needs a human (FR-MIG-04); this is untested: the only non-human caller is the external token, and `migration.apply` is not on the external allow-list. There is no NDA PDK import channel with automatic labelling (FR-INT-04, FR-SEC-01).
- **Agents**: no change set size threshold (FR-AGT-06). No soft-constraint path (FR-AGT-09). No per-role tool profiles or LLMs (FR-AGT-14/19). The declassified handoff pack is suggested, not generated (FR-AGT-13a).
- **Scheduling and pipelines** (R1): the bundled plugins declare `resources` only where they need more than the defaults: `numeric`'s `benchmark` and `l2_fast_check` ask for 4 cores, 8 GiB and a GPU (they use it when there is one, fall back to the CPU otherwise, and run alone on a machine without a GPU, because a run larger than the whole capacity runs alone); every other handler runs at the defaults (process plugins 1 core and 1 GiB, container plugins 4 cores and 8 GiB). So on the 15 GB development machine, two container runs at the default limit (8 GiB) no longer run side by side but one after the other (before, they overcommitted memory); `tests/ui-latency.spec.ts` declares a capacity of 32 GiB to create its load. The queue is strict: a run that does not fit holds back the runs behind it, which costs utilization but starves none. Container plugins are not given GPUs. A test reuses an earlier run by comparing handler, params and inputs, not the plugin version (run records do not store it). Resuming "run all tests" after a kernel kill has no test (resuming a development pipeline does; both take the same path). Whether a pipeline may write hard-constraint slots (now: only while empty) is for the owner to decide. The params of `flow-default`'s pipelines are fixed in the flow pack and drive the 5K proxy only; the pipelines for Qwen come with the six-step flow pack from R2 on.
- **UI**: no incident filters or jump links. No RTL, waveform or layout viewer beyond PNG. Settings lack LLM, quota and egress configuration (FR-UI-10/11/13, FR-AGT-25/26). The pipeline panel lists the nodes' states, runs, verdicts and reasons, but not each node's duration, resources or live log (the node graph of FR-UI-14 is only partly built). The Flow page's "done" is still an approximation: every check judged in the stage has a passing latest verdict, the latest run of every handler succeeded, and the next stage was entered (except for the last stage); it does not yet judge by the flow pack's required tests.
- **Tool versions**: a run record holds the plugin and handler, not the container image of that run; the image is found only in the plugin's manifest (pinned by digest), so after the manifest changes the run itself does not tell the old image (FR-TRC-02, AC-08).
- **Verdicts from before R0**: in an old home, a version-1 verdict `waived` of `accuracy.budget` or `phy.drc` followed by a version-2 pass opens a `standard_lowered` incident, although the standard became stricter (no waivers). Say so when closing the incident.
- **CLI**: declassification and migration have no dedicated commands. Use `llmchip call` (FR-INT-07).
- **TypeScript plugin SDK** has no tests of its own. It is exercised only through `stub-tool`.
- **Imported Hugging Face models** reach S2 of `flow-default` as analysis only: S3–S7 consume the llama2.c Model IR (`model.ir`), and arch-taalas refuses a graph numeric spec with a reason. The NumPy L0 interpreter has no KV cache (the whole sequence per step); the graph L2 and the fast L2 have one.
- **The SPEC step of Qwen** (R2a): the numeric spec is W4 calibrated by GPTQ on WikiText-2 since R2b, and the full set shows that this is not enough: on the six benchmarks that finished, L2 drops 5.1 points on MMLU, 16.8 on CMMLU, 16.7 on C-Eval, 32.5 on GSM8K, 7.8 on IFEval, 28.7 on HumanEval+ and 28.3 on MBPP+ against L0 (2026-10-25, the full set) - R2 continues with mixed precision or a wider format for the sensitive matrices, not by lowering the allowance; mixed precision (a format per matrix) is implemented but not used in the pipeline; the diagnosis of the wrong answers is `numeric/diagnose` (`spec.diagnosis`: first divergent token, the layer with the largest deviation, its matrices); KV int8, the design of `taalas-tile/1` and the slice now exist (`arch-taalas design` with `architecture: taalas-tile/1` and `arch-taalas slice`); - **Model quality after the full set**: the four benchmarks that finished (MMLU, CMMLU, C-Eval, GSM8K) all exceed their allowances with the calibrated W4 spec (§6.3), so the next calibration experiment is W8, or W4 with W8 for the sensitive matrices (the per-matrix `overrides` of `calibrate_graph`): the engine's ROM doubles for a W8 matrix (the whole model's W4 ROM is 1.98 Gbit, `taalas-tile/1`'s area model already calls Q-full infeasible at W4, so W8 changes no feasibility conclusion for Q-full, and the slice S-1 is unaffected). The quick set on the W8 spec is the first measurement.
- **The transaction-level model exists** (`arch-taalas tlm`: the schedule's phases, the L2 observation point of each, the cycles, bit for bit against L2 on the same tokens); the sky130/IHP profiles follow in later parts of R2. On the calibrated spec the tile is 24 engines of 128 MACs (128 divides 896 and 4864; the plan's 448 does not divide 4864 - the greatest common divisor is 128), 512-bit ROM words, and 189,564 cycles per token at 8 MHz, 42.2 tok/s, with the output projection segmented over the 24 engines; with 64 MACs it is 379,085 cycles, 21.1 tok/s. The cycle model is row major since the pipeline change: a matrix costs `rows * ceil(columns/macs)` words plus three pipeline cycles per phase (S1 word, S2 group sums, S3 accumulators), and the schedule table, `verify_spec` and the transaction-level model all use it. Two schedule switches are modelled as well: `classifierOverlap` makes the segmented classifier its own schedule row, so a token pays the slower of the layer pipeline and the classifier stream instead of their sum, and `dualMlp` fuses `gate_proj`/`up_proj` into one two-stream phase. On the calibrated spec at 24 engines of 448 MACs they take a pass from 54,320 cycles to 31,927 (1.70x; 147.3 to 250.6 tok/s at 8 MHz, 1,566 at 50 MHz), and `design` records both in `dataflow`. The RTL side of the dual MLP is `tile_engine2`: two `tile_engine` instances march the same addresses in lockstep from two sets of banked ROMs (`INIT_A0..3`, `INIT_B0..3`), share one activation write port and emit both row values, so a gate/up phase costs one matrix time at the price of a doubled MAC array; both streams' vectors are checked against the Python reference. The same replication gives the batch: `tile_engine_b` instantiates `B` engines, starts them together and demultiplexes the activation write port by token, so each token has its own activation buffer and accumulator set while all of them read the same words - the cycle behaviour of the model's `batch`. In this RTL model each copy carries its own ROM instance (the replicated array of the plan); a production layout fans one ROM out to the `B` arrays, which changes area but not cycles. It also takes a `batch` parameter (N9, the throughput plan in the design draft): one pass produces `batch` tokens, the matvec phases stream the weights once for all of them so their cycles do not change, the attention and vector phases - per token - scale with the batch, and `performance.cyclesPerPass` records the pass. With 448 MACs and B=4 a pass is 78,947 cycles for four tokens: 405 tok/s at 8 MHz, 2,533 at 50 MHz, 5,067 at 100 MHz; the classifier overlap and the gate/up parallel phases of the plan are schedule changes that are not in the model yet. The transaction-level model on 8 tokens covers 12 phases and 194 observation points with 0 mismatches, the same 190,014 cycles and a deterministic second run. The area model calls Q-full infeasible (even its optimistic die is 18,440 mm2 against a 850 mm2 reticle): the whole model's W4 ROM does not fit any die, which is why Q-full is RTL and verification only and the slice (S-1, 57,344 weights) is what goes to synthesis, physical design and signoff. The slice S-1 of layer 0 is 57,344 weights (229,376 bits), as the plan says; S-2 and S-3 are one Q head larger than the plan's table (286,720 and 630,784 weights instead of 229,376 and 573,440) because the O projection must take two whole heads (128 columns) to stay on the 128-weight group boundaries - the plan assumed a group size of 64 for O. The full set has no entry of its own yet (requirements FR-QNT-08): `benchmark` with `set: full` can only be started with `runs.start` or the CLI, and `verdicts.evaluate` then judges `spec.accuracy.<benchmark>` on the comparison. The quick set has only 5 questions per generation benchmark: a drop is then a multiple of 20 points, so a quick check passes only when L0 and L2 are correct on exactly the same questions (a drop of 0 with the interval [0, 0]; e.g. both score 0 of 5), fails when the drop is above the allowance, and is indeterminate otherwise — such a quick-set pass with 5 questions is possible (conflict C8, §7.3); the verdict on model quality comes only from the full set. L0's scores and the numbers Qwen publishes ([benchmark-reference.json](../plugins/numeric/data/benchmark-reference.json)) are shown side by side in the comparison, not judged; they are the plugin's data, not a run input. The GSM8K setting was chosen from L0 on the first 200 questions (with the repetition penalty on: lm-eval's default 33.5%, 8-shot CoT 36.5%, zero-shot CoT 42.0%, `GSM8K_QWEN` 44.5%; Qwen publishes 49.6%); the full set without the penalty is in §6.3. The code sandbox mounts the work directory writable, so code under test could in principle rewrite its own result file; it is code of this workbench's model, not adversarial, and recorded as a known risk. `l2_fast_check` runs the reference L2 on the CPU, about 3 minutes for 20 prompts, without checkpoints. Owner decisions of 2026-10-24: this GSM8K setting stands; the calibrated quantization of R2c (GPTQ) calibrates on WikiText-2, downloaded into the tools directory (`tools/hf/datasets/Salesforce___wikitext`), not into the repository; the full set on L2 was started on 2026-10-25 (checkpointed; hours on one GPU); until it finishes, the full-set checks cannot reach a verdict.
- **The RTL of the tile (R3)**: a `taalas-tile/1` bundle generates for a slice (S-1 is 57,344 weights) and its ROM banks for the whole model (Q-full: 24 layers x 7 matrices plus the tied classifier's bank), each bank with view 1, the synthesised view 2 and the via-programmed view 3, each proved equivalent by Yosys. The attention block and the Q-full top are now templates in the bundle: `tile_attention` reproduces `GraphL2.attention` with a dedicated 17x17 multiplier, a registered exp-table read with its wait state, a restoring division per position and a weighted-sum accumulator; `tile_qfull_top` chains one engine per layer, serialised on `done` or pipelined on the first row, and the generator writes one bank set per layer (`layer<n>_bank0..3.hex`) plus a `tile_qfull_banked` top that loads them. Not built yet: the per-phase schedule encoding in the RTL and the residual-buffer handshake that turns the pipelined chain into a real layer pipeline. The three ROM views are present: view 1, view 2, and view 3 (the via-programmed array `rom_view3.v`/`rom_via.json`, proved equivalent to view 1 on the same 64-word prefix view 2 uses; `rom_view3_equiv.v` and a via-mismatch negative control are in the tests). The RTL lint is the R3 three-tool standard: Verilator `-Wall`, Verible (tabs, trailing spaces, EOF newline, forbidden macros, 120-column lines) and slang-tidy's synthesis checks, all zero warnings; `rtl.tile.lint` and `rtl.tile.rom.views` were bumped to version 2. The datapath now exists in the row-major form the design draft decides: a ROM word holds one output row's 128 consecutive weights, `tile_mac` multiplies the 128 weights by the 128 activations of that column chunk and sums them with eight group adders (S2 registered), the eight group accumulators (S3 registered) add the chunk sums of a row, and the row's value is the tree of the eight accumulators, emitted with its index three cycles after the row's last chunk (`tile_engine`). `taalas-tile-rom/2` is the mapping rule; S-1's 57,344 weights are 448 words of 512 bits (229,376 bits), matching the draft's corrected count. Its synthesis at the GF180MCU tt corner (Yosys + OpenSTA, `tt_025C_1v80`) gives 10,884 um² for the engine with a one-word ROM (55% of it sequential) and a 24.7 ns critical path at a 10 ns clock (WNS -16.3 ns, down from -20.1 before the MAC array was made free-running and its wide enable net removed): the path is control, not arithmetic - seven gates whose 4-5 ns each come from fanout. More important, the flat 448-word ROM synthesized alone (512-bit words, random contents) is 0.34 mm2 and a 478.7 ns address-to-output path at tt: the ROM, not the ALU, is the bottleneck of a slice, exactly as the design draft warns. Banked ROM with a registered output per bank (draft 2.2: four banks of 112 words, the read split into bank decode and column mux) is therefore required before any frequency claim.

N5 ran the LibreLane 3.1.0.dev3 Classic flow (Yosys, OpenROAD, OpenRCX, Magic, KLayout, on the local GF180MCU PDK with `--manual-pdk`; 10 ns clock, 900 x 900 um die) on the engine with its 448-word ROM: it completed through place, CTS, route and the sign-off-adjacent checks, producing GDS, DEF, LEF, SDC and SPEF for three corners. Result of the two placements at a 10 ns clock: on a 600 x 600 um die the engine is 78,669 um2 of standard cells (21.9% of the die), 12,973 instances, 63.7 mW, 2.18 mV worst IR drop, with WNS -0.714 ns at tt/25C/5.00V (a 10.71 ns period, 93.3 MHz) and -9.97 ns at ss/125C/4.50V (19.97 ns, 50.1 MHz); on a 900 x 900 um die the looser floorplan costs timing: 91,522 um2, 21,349 instances, 64.1 mW, 1.65 mV, WNS -0.87 ns at tt and -10.12 ns at ss. Hold is 0 at every corner, the KLayout and Magic DRC reports are empty, the antenna and power-grid violations are zero, and the flow writes GDS, DEF, LEF, SDC and three-corner SPEF. So the physical design reaches the 50 MHz the ss corner allows and 93 MHz at tt. The 50 MHz is now closed by construction: the group sum in `tile_mac` was a serial accumulation in a loop, which synthesis kept as a GroupSize-deep adder chain - the 22.3 ns ss path - and it is now eight partial chains of GroupSize/8 combined by a three-level tree (addition modulo 2^ACC is associative, so every engine vector is bit identical and the 68 rtl-taalas tests pass unchanged). Re-measured through LibreLane at a 20 ns clock on a 450 um die with the narrowed activation port (65 pins): tt/25C/5.0V +8.61 ns, nom ss/125C/4.5V +0.13 ns, max ss -0.019 ns, hold +0.11 ns, 181,266 um2 of standard cells, 27.2 mW, 1.63 mV worst IR; the same configuration before the tree was -0.91 ns at nom ss. So 50 MHz closes at the typical and nominal slow corners and is 19 ps short of the harshest one; ROM banking is therefore no longer needed for 50 MHz - it is the 100 MHz enabler, together with the S2 split the design draft describes (2.1). Banking is in the RTL all the same (`taalas-tile-rom/3`: four banks of 2^(ADDR-2) words, one $readmemh file per bank, the bank selected by the address's top two bits, the read register still the engine's S1): all four banks change no value in synthesis - Yosys maps the four arrays to the same mux network as the flat array and the flow's metrics are identical to the picosecond - so its worth is physical (hierarchical placement or a hard ROM macro), not logical, and the per-bank files are what a banked hard macro will be built from. The design draft's 2.1 split is in the RTL as well: `tile_mac` registers the products (S2a) before the group tree (S2b), the first-chunk flag travels with the data (`clear` became `first`, delayed twice), and the engine holds the activation one cycle into the drain; at a 10 ns clock it gives -0.82 ns at tt on a 450 um die (from -1.39 ns without the split). A fourth stage - the row sum of the eight group accumulators is registered before `y` - takes that to -0.286 ns on 450 um and -0.062 ns on a 500 um die (99.4% of 100 MHz; the 450 um run is 181,266 um2, 74.8 mW, 3.48 mV worst IR, the 500 um one 226,747 um2, 75.7 mW, 0.22 ns hold). So 100 MHz at tt is 62 ps away, with 50 MHz still the closed, measured target. The frequency claim turned out to hinge on the corner, not on the datapath: the standard-cell flop itself costs 6.29 ns of clk-to-Q plus 3.18 ns of setup at ss/125C/1.62V (9.5 ns before any logic), against 2.50 + 1.00 ns at tt/25C/1.80V (3.5 ns); a two-flop path through one inverter measures 10.6 ns at ss, which matches the liberty. 100 MHz is therefore comfortable at tt and out of reach at ss with this library, so the corner and voltage of the frequency claim are an owner decision recorded in the design draft (drafts/tile-180nm-pipeline.md, N4 finding). The attention block is implemented, not just sketched: `tile_attention` walks one head at a time with a single 17x17 multiplier, so the score is `(dot * att_scale) >> (fq + fk + 15 - SCORE_FRAC)` saturated to int32, the causal mask walks j <= position, `d = min(smax - s, EXP_RANGE * 2^SCORE_FRAC - 1)` indexes the exp table at `d >> (SCORE_FRAC - 5)` (the table's output is registered, so the state machine waits one cycle), `p = (e << 15) / sum(e)` is a restoring division that shifts the remainder left and compares it against the divisor, and the output is `(sum p * v) >> 15` saturated to int16. The reference is `llmchip_rtl.tile.attention`, checked against `GraphL2.attention` by `test_attention_reference.py`, and the RTL is checked against it by `test_tile_attention.py`; this dedicated multiplier is not the schedule model's shared MAC array, so its cycle count is not yet in the schedule table. The generator also writes the hard-macro view of the slice's bank set (`rom_macro/`: black box, simulation model, provisional LEF and manifest), and the four `rom_bank0..3.hex` files are its contents. The whole model's W4 ROM is about 1.98 Gbit, so a Q-full bundle's hex contents are hundreds of megabytes and its generation is a long run; the registers of each engine are reset, as the rules require.
- **The R4 verification of the tile (R4)**: the `tile_verify` handler of `rtl-taalas` runs the template suite, the functional S-1 slice against `GraphL2`, the clock-domain check and the chained layer harness; the `flow-six` VER stage judges `ver.unit`, `ver.slice`, `ver.cdc`, `ver.layer`, `ver.coverage`, `ver.formal`, `ver.mutation`, `ver.dft` and `rtl.tile.equivalence` with it (`rtl.tile.equivalence`, not `rtl.equivalence`: `flow-default` defines that id on `sim-verilator/verify`). The layer operators are now individually verified against GraphL2: linear (q/k/v/o/gate/up/down), RMSNorm (attn/mlp and per-head q/k), RoPE, KV cache with attention, SiLU gate and the residual adds. The bit-exact linear stage (`tile_linear`) applies the quantisation group mantissas, the matrix exponent, the half-up shift and the bias exactly as `GraphL2.linear`; `tile_slice_top` is the S-1 q_proj slice, and `test_tile_slice.py` drives real `attn_norm` activations and compares four tokens (head 0's q rows) bit for bit. The chained layer harness runs `ver.layer` (`tile_layer_attn_tb.v`) for both Q-full layers: all 17 observation points per layer (attn_norm, q/k/v, q_norm/k_norm, RoPE, attention over the KV caches, o_proj, residual, mlp_norm, gate/up, SiLU, down_proj and the second residual) are bit-exact against `GraphL2`, 34 comparisons in total. `ver.coverage` reports 96% line coverage of the same harness, `ver.formal` proves the residual add's saturating-sum property (with a negative control), and `ver.mutation` runs a 50-mutant MCY campaign on `tile_residual` (100% after equivalence classification, 12 `NOCHANGE`); `ver.dft` is a readiness audit with four negative controls; and `rtl.tile.equivalence` composes the two layer harnesses with a final_norm/lm_head tail: layer 0 residual feeds layer 1, then final_norm and lm_head produce all 128 logits, bit-exact against `GraphL2` (one test, 128 logits). DFT is a readiness audit, not scan insertion or MBIST. The `verif` plugin boundary is the `tile_verify` handler for now.
- **CUDA builds of torch**: on Linux x86-64, `.venv` installs the CUDA 13 build of torch (it falls back to the CPU without a GPU), about 3 GB more than the CPU build.
- **S3 speed and area** (proxy of the reference model, ternary, 2 lanes, 8 MHz, measured 2026-10-20): the old formula said 10.1 tok/s; the exact count of the old design (SPI, no buffers) is 1,024,601 cycles per token, 7.8 tok/s. Quad SPI gives 352,505 cycles, 22.7 tok/s (2.9×); attention buffers 34.6 tok/s; all buffers 43.8 tok/s. 77.7% of the cycles are external memory waits even with quad SPI. Only quad SPI without buffers places on the 1.2 × 2 mm die: synthesized cells of 287k µm² in the 497k µm² core. Placement is at its limit and not monotonic in the density setting: 70% and 85% placed and closed timing (setup slack +22 ns, so about 9.7 MHz would close), 75% and 80% failed detailed placement in the same netlist; the prerun uses 70% (`PRERUN.placementDensityPct`). The old SPI design no longer routes at the former 62% either. With attention buffers (332k µm²) or all buffers (398k µm²) detailed placement fails at every density tried (62–85%), and one lane instead of two did not route. The quad-SPI pads are bidirectional `bi_t` cells whose input is enabled only while they do not drive (IE = ¬OE): the pad library defines its A → PAD timing arcs only for IE low, so with IE tied high the output direction went untimed and STA reported the four pins as unconstrained. The functional pad model for gate-level simulation (`plugins/eda-gf180/sim/pads.v`) had the `bi_t` input path tied to 0, which held only while no pin was bidirectional; it now passes the pad's value while IE is high. The SDF testbench takes the memory pins from the design (SPI or quad SPI). The process library's area estimate does not see this: its control logic is 0.05 mm² (empirical) where synthesis gives about 0.19 mm², and its utilization is 0.7 (empirical) where 0.58 placed and 0.67 did not. Until the library is calibrated against these implementations, `explore`'s fit is optimistic; a design is proven to fit only by `eda-gf180 implement`.
- **Numeric analysis results** (int8, groups of 32, half-up, saturate, fraction bits per layer, 24-bit residual stream; 4 prompts × 8 tokens, 3 passages; on the development machine, 2026-10-19): stories260K perplexity 3.16 → 3.17, next-token agreement 98.6%, 1 s; SmolLM2-135M 12.64 → 12.76, 94.3%, 30 s; Qwen3-0.6B 23.00 → 22.39, 88.8%, 136 s; DeepSeek-R1-Distill-Qwen-1.5B 41.41 → 35.92, 88.8%, 584 s. At int4 SmolLM2 rises to 22.6 and Qwen3 to 28.6. With one 16-bit format per tensor kind SmolLM2 collapses (perplexity about 2.9 million): a few layers carry residual values near 20 000. `numeric.accuracy` asks for next-token agreement ≥ 0.9, which Qwen3 and DeepSeek at int8 miss narrowly.
- **The virtual chips' container cleanup** (found during R2a, fixed 2026-10-25): `llmchip_vchip/bridges/` removed simulator containers with `podman rm -f -t 0`, and Podman 3.4 on this machine has no `rm -t` option, so the command failed at once and the container was not removed; `tests/vchip.spec.ts` gives the containers 30 s to end on their own, which is why it did not show. `remove_container` now kills, then removes (`podman kill`, `podman rm -f`), with a unit test that asserts the order and no container left behind; the SIGTERM path gives each call 1 s, inside the kernel's 2 s cancel grace. The full L3 conformance after the change passes (T-01–T-19, T-06 n/a, 2026-10-25). The benchmark sandbox uses the same order.
- **Virtual-chip test harness** (`llmchip-vchip`, L1 and L3 of the owner's draft design): backends `reference`, `numeric`, `process` and `rtl`. The RTL path covers one generator, `taalas-spatial/1` (the 5K proxy; the generator's shape limits apply); `taalas-rom/1` has no RTL generator yet. No gate-level backend (design T-20, L3+) and no perf backend. The full 20-prompt L3 run is opt-in (`LLMCHIP_VCHIP_L3=1`, about 11 min); with the image the default tests bench 4 prompts. Perplexity needs the `logits` capability. Standalone (`vchip`), runs are not artifacts and it judges by the chip's policy (ADR-010); through the kernel plugin `vchip` (L4), runs and reports are artifacts and the flow's checks judge, but the checks belong to no stage of the flow. The plugin builds chips only from an `llmchip-numeric-spec/1` spec (S2 of a llama2.c model; a graph numeric spec of an imported model is refused) and runs only `taalas-spatial/1` bundles. Its simulator runs in a session and a container of its own: a cancelled run removes it, but after a kernel crash the kernel's orphan cleanup kills only the plugin's process group, so a simulator container can outlive it (`podman ps --filter name=llmchip-vchip`). The Web UI page starts runs and shows reports; there is no separate HTTP API (the kernel API is the API) and no sampling.
- **A failed analysis check opens an incident**, as every failed check does (FR-VER-06), so trying poor schemes on the S2 bench fills the incident list.

### 7.3 Conflicts between code and upstream documents (open, reported, not resolved)

| # | Upstream statement | Code | Scope |
|---|---|---|---|
| C1 | ADR-008 / architecture §4.8: the gateway is the only module holding outbound connections; providers only construct requests | `createProvider` calls `gateway.check`, then `fetch` itself | K8 |
| C2 | Architecture §9.3 and §12.1: containers have a read-only root file system and can write only to `out/` of the run workspace | `Scheduler.containerCommand` passes no `--read-only`; `out/` and `work/` are mounted writable | IF-3 |
| C3 | Architecture §14 layout (`agent-runtime/`, `llm-providers/`, `mcp-server/`, `plugin-sdk-ts/` packages) | agent runtime and providers are in `packages/kernel/src/agents/`, MCP in `packages/cli/src/mcp.ts`, SDK in `packages/plugin-sdk` | layout ("Proposed") |
| C4 | Architecture §6.2: change set writing into the project git working directory "arrives with the generators in M2" | not implemented in M2–M4 | §6 |
| C5 | Architecture §3, §6.1: the agent runtime and LLM providers are in-kernel system plugins | `AgentRuntime` is a plain class built by the `Kernel` constructor; providers are kernel code; nothing goes through `PluginHost` | §6, ADR-001 |
| C6 | Architecture §4.3: a run's outputs are registered all at once, only after the run succeeds (checkpoints excepted, R1) | outputs are stored one by one; an invalid later output leaves the earlier ones in the store, unlisted by the run | K3/K4 |
| C7 | Architecture §5 item 5: shutdown stops the agent runtime before flushing the event store | `Kernel.shutdown` never stops `AgentRuntime`; a running `tasks.run` loop is not ended first. Whether it can append after the database closes has not been checked | K1, §6 |
| C8 | FR-QNT-08: quick-set verdicts, "with few questions they are honestly indeterminate" | FR-QNT-04's rule decides by the drop and its interval. With 5 questions a drop is a multiple of 20 points, so a quick check passes when L0 and L2 are correct on exactly the same questions (drop 0, interval [0, 0]) — e.g. both score 0 of 5 — instead of being indeterminate | FR-QNT-04/08, check standards |


Until the owner decides, keep the code's current behavior and do not build on either side of these items.

## R5 synthesis and DFT

The SYN stage is now wired: `syn-yosys` runs Yosys inside the pinned `iic-osic-tools` image and maps the S-1 slice to sky130A `sky130_fd_sc_hd`, GF180MCU `gf180mcu_fd_sc_mcu7t5v0` and IHP SG13G2 `sg13g2_stdcell`. `syn.synth.<pdk>` checks require a non-zero core area, a cell count and the PDK-tagged metric. Measured on the S-1 slice (2026-10-26): sky130 118,591 um^2 / 12,267 cells / 1,506 flops; GF180MCU 372,025 um^2 / 14,883 cells / 1,506 flops; IHP SG13G2 215,659 um^2 / 15,533 cells / 1,506 flops. OpenSTA reports worst slack at a 100 ns clock of 78.15 ns on sky130A, 45.73 ns on GF180MCU and 87.59 ns on IHP SG13G2, and `syn.timing.<pdk>` requires non-negative WNS. The Fault adapters now run scan-chain insertion on all three PDKs: 1553 internal + 142 boundary flops, total 1583, and `syn.dft.scan.<pdk>` requires a non-empty internal chain. ATPG on the full S-1 cut netlist dumps core inside Fault 0.9.4; `syn.dft.atpg.<pdk>` therefore runs on the documented combinational `tile_residual_dft` target (10,304 stuck-at sites, 20 vectors; coverage 99.16% sky130, 96.79% GF180MCU, 98.65% IHP SG13G2) and a tied-output negative control must fall below 95%. The S-1 full ATPG remains a recorded tool limitation, not a claimed pass: a second tool, Atalanta 2.0 built from source, also crashes on the 61k-fault RMSNorm bench, so no open-source ATPG available here covers the full S-1 netlist.
