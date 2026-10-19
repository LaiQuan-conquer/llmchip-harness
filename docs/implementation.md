# Current Implementation and Extension Guide

English | [中文](implementation.zh.md)

| Item | Content |
|---|---|
| Document version | v0.1 |
| Date | 2026-10-19 |
| Describes | the code at commit `9cc76a0` (M4), plus the flow page, `yarn demo`, model import and graph recovery |
| Upstream documents | [Requirement Analysis](<../Coding Harness MVP Requirement Analysis.md>) v0.6, [Microkernel Architecture](<../Microkernel Architecture.md>) v0.9 |

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
| M2 model to spec (S1–S3, G0–G2) | `9284a99` | Done on stories260K and the proxy model; no trained reference model | `tests/m2-flow.spec.ts` |
| M3 RTL and verification (S4–S5, G3) | `3842361` | Done on the proxy model | `tests/m3-flow.spec.ts` (needs the EDA image) |
| M4 physical, signoff, package (S6–S8, G4) | `9cc76a0` | Done on the proxy model, 1.2×2 mm die | `tests/m4-flow.spec.ts` (opt-in, 39 min measured) |
| M5 migration to the tapeout process | — | Only the assessment and invalidation mechanism (FR-MIG); no NDA PDK | `tests/m4-kernel.spec.ts`, `plugins/estimator/tests/test_migration.py` |
| M6 post-silicon (S9) | — | Not started | — |

So M1 as a whole is **not** accepted: AC-M1-01 (installer) and the desktop shell are missing. See §7.

### 1.2 Implemented

| Area | What works | Entry point |
|---|---|---|
| Kernel daemon | Start, single-owner lock, recovery, ordered shutdown | `harnessd serve`, [kernel.ts](../packages/kernel/src/kernel.ts) `Kernel.start` |
| K1 event store | Append-only SQLite log with a hash chain, projections, replay | [events/store.ts](../packages/kernel/src/events/store.ts) |
| K2 artifact store | Content-addressed, read-only objects, lineage, label propagation | [artifacts/store.ts](../packages/kernel/src/artifacts/store.ts) |
| K3/K6/K7 workflow | Projects, slots, branches, stages, verdicts, snapshots, approvals, conditions, waivers, release, export, migration | [workflow/engine.ts](../packages/kernel/src/workflow/engine.ts) |
| K4 scheduler | Persistent queue, process and container execution, timeouts, cancellation, retries, pause | [runs/scheduler.ts](../packages/kernel/src/runs/scheduler.ts) |
| K5 API policy | Owner token vs external token; method allow-list | [security/policy.ts](../packages/kernel/src/security/policy.ts) |
| K8 egress | Labels, destination limits, NDA fingerprints, declassification, audit | [security/egress.ts](../packages/kernel/src/security/egress.ts) |
| K9 plugin registry | Manifest scan, validation, data import | [plugins/registry.ts](../packages/kernel/src/plugins/registry.ts) |
| K10 config and secrets | Layered config, AES-256-GCM secret file, redaction | [config/](../packages/kernel/src/config/) |
| Agent runtime | Managed agents with tool checks, change sets, handoff, loop and budget stops | [agents/runtime.ts](../packages/kernel/src/agents/runtime.ts), [agents/providers.ts](../packages/kernel/src/agents/providers.ts) |
| IF-1 kernel API | JSON-RPC 2.0 over WebSocket `/v1/rpc`, HTTP `/v1/artifacts` | [server.ts](../packages/kernel/src/server.ts), [client](../packages/client/src/index.ts) |
| IF-2 plugin protocol | JSON-RPC 2.0, one message per line, over stdio | [plugins/process.ts](../packages/kernel/src/plugins/process.ts), SDKs |
| IF-3 tool containers | Podman, no network, read-only inputs and plugin directory | `Scheduler.containerCommand` |
| CLI and MCP | `llmchip` commands, generic `call`, MCP on stdio | [cli/src/main.ts](../packages/cli/src/main.ts), [cli/src/mcp.ts](../packages/cli/src/mcp.ts) |
| Web UI | Ten pages, Chinese and English. The default page, Flow, shows a project's S0–S9 path with results, checks and gates in plain language | [apps/web/src](../apps/web/src) |
| Domain flow | S0–S8 and G0–G4 with bundled plugins (table in §2.4) | [plugins/](../plugins) |
| Real model import and graph recovery | A downloaded model directory (safetensors + config.json + tokenizer.json, or llama2.c) is imported by local path, censused from its headers, estimated, recovered into a computation graph (Llama family: Llama, Mistral, SmolLM, Qwen2, Qwen3, llama2.c), executed as L0 and tested item by item against transformers / the run.c port: weights bit for bit, configuration, parameter count, every layer's hidden state, logits, top-1 and greedy generation per prompt, single-token and long-sequence boundary cases. A project's own llama2.c weights (e.g. the proxy) are imported from its artifacts. The S1 page picks any imported model or a new path, takes custom prompts, and shows every test item | [model-llama/src/llmchip_model/pipeline.py](../plugins/model-llama/src/llmchip_model/pipeline.py) |

### 1.3 Interface only (declared, no behavior)

- Plugin kinds `silicon-test`, `ui-panel`, `llm-provider` and `parser` in `PluginKind` ([kernel.schema.json](../packages/kernel-api/schemas/kernel.schema.json)). No plugin declares them. LLM providers are kernel code, not plugins.
- Stage S9 in [flow-default/flow.json](../plugins/flow-default/flow.json). It has an entry gate (G4) but no handler and no check.
- Checks `sdf.timing` and `external.signoff` in the default flow. They are defined, but no gate uses them. `sdf.timing` requires the capability `sdf-timing-sim`, which no plugin declares, so it can only judge `indeterminate`.
- The `formal` field of the verification report (sim-verilator `verify`). It is always empty (FR-VER-07).
- `PluginHost` for in-kernel Cordis plugins ([plugin-host.ts](../packages/kernel/src/plugin-host.ts)). It works and is tested, but only `KernelOptions.plugins` loads anything into it, and only tests pass that. No product plugin runs in-kernel (see C5 in §7.3).

### 1.4 Not implemented

The Tauri desktop shell, installer, dedicated WSL distro, first-launch onboarding and automatic image pulls. Also: CHARTER.md generation, the sample project, project export and import, the project git working directory (architecture §6.3), S9, model training (FR-QNT-05), graph recovery outside the Llama family and of GGUF, `images/` and `docs/adr/`. §7.1 lists them with requirement IDs.

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
| 12 | K3/K6/K7 | `Workflow` | flow packs, check definitions, `workflow` projection | events, artifacts, registry, scheduler, incidents |
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

`plugins.list` on a fresh home shows the following plugins. Stages and gates come from `flow-default` (flow version 0.4.0).

| Plugin | Runtime | Handlers | Used in | Unit tests |
|---|---|---|---|---|
| `flow-default` | data | flow pack: S0–S9, G0–G4, 16 checks, hard-constraint slots | all | via `tests/*-flow.spec.ts` |
| `process-library` | data | profiles `gf180mcu`, `sephonchitech-180bcd`; `library-180nm`; `package-gf180mcu`; `signoff-gf180mcu` | S0, S3, S6–S8 | via estimator and flow tests |
| `estimator` | process (Python) | `estimate`, `check_views`, `migrate` | S0/G0, stage entry S4/S6/S7, FR-MIG | `test_estimate.py`, `test_migration.py` |
| `model-llama` | process (Python) | `synthesize`, `intake`, `reference`, `import`, `recover`, `run_graph`, `compare` | S0 (import), S1 | `test_model.py`, `test_graph.py` |
| `numeric` | process (Python) | `quantize`, `infer`, `evaluate` | S2/G1 | `test_numeric.py` |
| `arch-taalas` | process (Python) | `design`, `verify_spec` | S3/G2 | `test_arch.py` |
| `rtl-taalas` | process (Python) | `generate`, `check_isa` | S4, S5 | `test_rtl.py` |
| `sim-verilator` | container | `lint`, `qualify`, `verify`, `gatesim`, `sdfsim`, `generate` | S5/G3, S7, Playground | `test_simlib.py` |
| `eda-gf180` | container | `calibrate`, `constraints`, `implement`, `drc`, `lvs`, `sta` | M1.5, S6, S7 | `test_calibrate.py`, `test_physical.py` |
| `packager` | process (Python) | `build`, `check`, `import_external` | S8/G4 | `test_packager.py` |

Test-only plugins in [tests/fixtures/plugins](../tests/fixtures/plugins):

- `stub-tool` (TypeScript) has handlers `report` (params `violations`, `truncate`, `coverage`, `crash`), `qualify`, `sleep`, `hang`, `crash`, `fail` and `escape`.
- `container-stub` (Python in a container) has handlers `probe` (probes the sandbox) and `sleep`.
- `test-flow` is a three-stage flow pack.

What each domain plugin produces:

- **estimator**: feasibility, area, cost and performance estimates (FR-EST), with low/typical/high values, a source and confidence per item, and the missing parameters. `check_views` reports the integration views a calibrated storage implementation has, per stage. `migrate` assesses migration impact against the design's process-capability dependencies (FR-MIG).
- **process-library**: process profiles (with technology data for migration), the implementation library, the GF180 package template and signoff rules. Every value records its source and confidence. Empirical values stay empirical until calibrated.
- **flow-default**: stages S0–S9, gates G0–G4 with their member slots and checks, stage entry checks, and the hard-constraint slots `constraints`, `timing-exceptions` and `signoff-rules`.
- **model-llama**: model import and graph recovery (below), plus llama2.c checkpoint and tokenizer intake into the llama2.c Model IR used by S2–S8. Proxy models (a planned model's structure, fixed seed, untrained, with the bundled `evalset-proxy`). The L0 FP32 reference, a NumPy port of `run.c`, checked against the vendored, compiled `run.c` in `plugins/model-llama/upstream/`.
- **numeric**: quantization (int8/int4/int3/ternary, group scales), the numeric spec (requirements §3.4.2), and the L2 bit-exact integer model with observation-point traces. Also the L2-vs-L0 accuracy evaluation (G1 evidence), with a bundled evaluation set.
- **arch-taalas**: the Taalas-style hardwired template. It derives the chip spec and PPA from a frozen numeric spec, and checks their consistency (G2 evidence).
- **rtl-taalas**: compiles the numeric spec into a microcoded vector engine as one deterministic `rtl.bundle`. The bundle holds:
  - the Verilog core, with data in an external SPI SRAM, and an SPI host interface;
  - ROMs as generated case tables, and the microcode;
  - a Verilator testbench with an SRAM model, and `gl_tb.v` for gate level;
  - the instruction-level simulator `isa.py`.

  The weight mapping rule is reversible and round-trip checked. `check_isa` verifies the microcode against L2.
- **sim-verilator** (Verilator, Icarus Verilog and cocotb in the pinned `iic-osic-tools` image):
  - lint and simulator qualification;
  - the verification plan (regression, directed, boundary, exhaustive, random, coverage; a default plan is bundled), with first-divergence localization (operation, element, cycle, VCD window);
  - gate-level simulation of the routed netlist (L4, Icarus Verilog with the PDK cell models);
  - SDF timing simulation with annotation statistics (supplementary, not qualified);
  - L3 generations for the Chip Playground.
- **eda-gf180** (same image: gf180mcuD PDK, Yosys, OpenROAD, Magic, KLayout, Netgen, OpenSTA):
  - storage implementation calibration (FR-EST-13);
  - constraints from the chip spec;
  - physical implementation with the pad ring (LibreLane Chip flow);
  - DRC, LVS, and 9-corner STA with constraint completeness. Each check runs on exactly the artifacts it judges.
- **packager**: the tapeout package from the process's template, its integrity check, and import of external signoff reports bound to a GDS (FR-PKG, FR-SGN-06).

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
4. `Workflow.loadFlows` reads every `flow.pack` artifact and collects check definitions. A pack with conflicting definitions of one check id is rejected as a `FLOW_INVALID` problem.
5. `Scheduler.recover` finishes any run still recorded as `running` as `interrupted`. It kills the run's orphaned process group (only if `/proc/<pid>/environ` carries `LLMCHIP_RUN=<id>`) and its container. It retries the run if attempts remain, and wipes `runs/`. In-kernel plugins passed in `KernelOptions.plugins` are loaded next (tests only).
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

### 3.2 Path B: from the run to a released gate (S0 → G0 → S1)

The estimate run alone is not evidence. A verdict counts for a gate only if its run's inputs are all among the gate's bound members. These commands were run in sequence:

```sh
yarn llmchip project create chip --title demo                    # project.created (flow "default")
yarn llmchip artifact put req.json --type estimate.request --media-type application/json
yarn llmchip artifact put charter.md --type project.charter
yarn llmchip project bind chip charter <hash>                    # also: estimate-request, process-profile, impl-library
yarn llmchip stage enter chip S1                                 # STAGE_ENTRY_BLOCKED: needs released gates: G0
yarn llmchip run start estimator estimate --input request=<req> --input profile=<p> --input library=<l> --wait
yarn llmchip verdict evaluate estimate.feasibility <run> --project chip   # pass
yarn llmchip gate snapshot chip G0                               # 4 members, 1 evidence, none missing
yarn llmchip call approvals.decide '{"project":"chip","gate":"G0","decision":"approved"}'
yarn llmchip gate release chip G0                                # approved (released)
yarn llmchip stage enter chip S1                                 # stage.entered
yarn llmchip project bind chip charter <new hash>                # approval.invalidated (by kernel:workflow)
yarn llmchip gate release chip G0                                # invalidated; S2 entry is blocked again
```

What happens inside [workflow/engine.ts](../packages/kernel/src/workflow/engine.ts):

- **`Workflow.evaluate`** (K6) checks that the run belongs to the check's plugin and handler. `judge` then applies the check in order. The execution must be accepted, the parse must be `complete`, a `requiredCapability` must be qualified, and the `completeness` criteria must hold. Any failure gives `indeterminate`. A missing metric also gives `indeterminate`. A failed `criteria` gives `fail`. An applicable waiver of the kind the check admits turns `fail` into `waived`. The verdict's `subjects` are the run's input hashes. The engine appends `verdict.recorded`. A `fail` or `indeterminate` opens a `check_failed` incident, and the incident carries `divergence_*` metrics when the run reported them. A pass that follows a non-pass of an older check version opens a `standard_lowered` incident (AC-M1-13).
- **`Workflow.snapshot`** (K7) has two parts. The members are the gate's bound slots, and their hash is the snapshot hash. The evidence is the latest verdict of each gate check whose subjects are non-empty and all lie inside the members. After a confirmed migration that reaches the gate, verdicts recorded before the migration no longer count.
- **`Workflow.decide`** is for humans only. It requires a complete snapshot, and every gate check passing or waived unless the decision is `rejected`. It binds the approval to the snapshot hash and the evidence hash.
- **`Workflow.release`** is the single release check (stage entry, export). The latest approval must match the current snapshot hash and must not be invalidated or rejected, and all its conditions must be closed.
- **`Workflow.bind`** appends `project.slot_bound` and, in the same transaction, `approval.invalidated` for every gate whose snapshot changed. Slots in `hardConstraintSlots` are bound only by humans; anyone else opens a `hard_constraint_change` incident.
- **`Workflow.enterStage`** requires every entry gate released and every entry check (`{check, slot}`) to have a latest passing verdict on the bound artifact. Otherwise it fails `STAGE_ENTRY_BLOCKED` with the reasons, including `views.<stage>.missing` metrics (FR-PHY-08).

`tests/flow-helpers.ts` `throughG2` and `throughG3Proxy` drive the same path through G2 and G3 with the real plugins. `tests/m4-flow.spec.ts` continues it to the G4 export.

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
| Run logs (`run.log`) | `Scheduler.execute` | any client | stored for every run, whatever its execution status |
| Uploads (`PUT /v1/artifacts`) | `KernelServer` for the owner token only | — | external tokens are refused with an incident |
| Run workspace `runs/<id>/` | `Scheduler.execute` creates and removes it | the plugin (in/ read-only) | removed after every run; all of `runs/` is wiped at start |
| Plugin processes and containers | `PluginProcess` (own process group), `podman run --rm --name=llmchip-run-<id>` | — | killed on finish, cancel, timeout or kernel stop; orphans killed at the next start |
| Verdicts, approvals, waivers, constraints, projects | `Workflow` | API | events; approvals are invalidated, never deleted |
| Incidents | `Incidents.open` / `close`, called by `Policy`, `Workflow`, `EgressGateway`, `AgentRuntime` | UI, API | closing needs a cause and an action (FR-AGT-27) |
| Egress decisions | `EgressGateway.check`, before every LLM request and every MCP download | audit | one `egress.decided` per request |
| Handoff to another provider | `AgentRuntime.switchProvider` compares the pack's label with the provider's limit itself | API | stores the pack as `agent.handoff`; a refused switch opens a `handoff_blocked` incident, with no `egress.decided` |
| Tasks, agent steps, change sets | `AgentRuntime` | API, UI | the state lives in events, so a new session is rebuilt from the log (handoff) |
| Secrets | `SecretStore.set` / `remove` (`secret.changed` carries names only) | providers via `secret(name)` | values never in events or logs (`Redactor`) |
| `run/harnessd.json`, lock | `Kernel.start` / `shutdown` | CLI, MCP bridge, tests | deleted at shutdown; a stale lock from a dead pid is taken over |

Event kinds by writer: `kernel.started/stopped`, `projection.rebuilt` (Kernel); `kernel.paused`, `run.*` (Scheduler); `artifact.*` (ArtifactStore); `secret.changed` (SecretStore); `plugin.*` (PluginHost); `verdict.*`, `project.*`, `stage.*`, `approval.*`, `waiver.*`, `constraint.*`, `migration.*`, `package.*` (Workflow); `incident.*` (Incidents); `egress.*`, `declass.*` (EgressGateway); `task.*`, `agent.step`, `changeset.*` (AgentRuntime). Every payload is validated against its schema definition (`KERNEL_EVENT_PAYLOAD_DEFS`) before it is hashed. Unknown kinds are refused.

### 4.2 Cancellation and shutdown

- **One run**: `runs.cancel` → `Scheduler.abort`. The scheduler sends the IF-2 `cancel` notification (the SDKs set `ctx.signal` / `ctx.cancelled`), runs `podman kill` for containers, sends SIGTERM to the group, and sends SIGKILL after `cancelGraceMs`. A queued run that never started is finished as `cancelled` directly.
- **Emergency stop** (FR-AGT-23): `kernel.pause` appends `kernel.paused`. No new run starts and agent loops pause; running runs continue. `kernel.resume` reverses it.
- **Kernel stop** (`Kernel.shutdown`) runs in a fixed order:
  1. delete the runtime file;
  2. close the server;
  3. stop the scheduler (abort every run as `interrupted` and wait for each to record its result);
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

### 5.2 A check, a gate member or a stage entry check

Example: the stage entry check `impl.views.S4`. It is defined in [flow-default/flow.json](../plugins/flow-default/flow.json), judges `estimator/check_views`, and is attached to stage S4 as `entry.checks: [{check: "impl.views.S4", slot: "storage-impl"}]`. The other example is the G0 check `estimate.feasibility`.

1. Make the handler report the metrics the check needs, and nothing judged.
2. Add a check to the flow pack's `checks`:
   - `id`, `version`, `plugin`, `handler`.
   - `completeness`: conditions under which the result means anything, else `indeterminate`.
   - `criteria`: pass or fail.
   - `waiver`: `none`, `risk_acceptance` or `foundry_waiver`.
   - Optional `requiredCapability`, `requireParse` and `acceptExitCodes`.
   - Change `version` whenever a standard changes. That is how a lowered standard is detected (AC-M1-13).
3. Reference it from a gate's `checks` (the gate's `members` slots must be the run's inputs, or the verdict will never count as that gate's evidence), or from a stage's `entry.checks`.
4. A check id may be defined by several flow packs only if every definition is identical. Otherwise the conflicting pack is not loaded, and `plugins list` shows a `FLOW_INVALID` problem naming the check.
5. Tests:
   - Verdict semantics are exercised with `test-flow` in [tests/workflow.spec.ts](../tests/workflow.spec.ts).
   - The G0 check on missing data is covered in [tests/estimator.spec.ts](../tests/estimator.spec.ts).
   - Stage entry on views is covered in [tests/calibration.spec.ts](../tests/calibration.spec.ts) (EDA image).

A new flow pack is a `data` plugin with a `flow.pack` data file ([test-flow](../tests/fixtures/plugins/test-flow)). Projects choose one with `projects.create {flow}`.

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

A stage can also have a detailed demo view, opened from the "Detailed demo" button on its card. Write it as `views/steps/<stage>.tsx` and register it in `DETAILS` in `Flow.tsx`; the open detail is `selection.stage`, so the sidebar lists it under the selected project and the back bar (and the browser's back button) returns to it, as to every page and artifact visited before. Example: [views/steps/S0.tsx](../apps/web/src/views/steps/S0.tsx) reads the stage's `estimate.report` and lays it out as question and answer, inputs, the area and speed calculations, the reverse fit, the verdict and gate, and a form that reruns the estimator outside the project. Build explanations from structured fields, not from a plugin's free text, so that both languages read naturally. `tests/web-ui.spec.ts` opens the S0 and S1 views in both languages, from the flow page and from the sidebar, and returns with the back bar.

### 5.9 An in-kernel system plugin (Cordis)

Implement `InKernelPlugin {id, inject, provides, apply(ctx, kernel)}` ([plugin-host.ts](../packages/kernel/src/plugin-host.ts)) and load it only through `PluginHost.load`. Register everything as a Cordis effect so unloading cleans it up. Today only `KernelOptions.plugins` feeds the host, and only tests pass it ([packages/kernel/tests/plugin-host.spec.ts](../packages/kernel/tests/plugin-host.spec.ts)). Making a product plugin in-kernel also means adding a production loading path. That is a change to the architecture's §5 guards; agree it with the owner first.

---

## 6. Verification

### 6.1 Commands

Measured on the development machine (WSL2, Node 24.22, uv 0.13, Podman 3.4 with the pinned `iic-osic-tools` image present), 2026-10-19:

| Command | Verifies | Observed |
|---|---|---|
| `yarn check:workspace` | packages, references, `workspace:*` deps, vendor manifest and licenses | "5 packages, 9 vendored packages" |
| `yarn check:generated` | `kernel-api/src/generated` matches the schema | "up to date (2 files)" |
| `yarn check:python` | ruff lint and format, pytest of the SDK and all Python plugins | 51 passed |
| `yarn typecheck` | project references, then sources, tests, scripts and the Web UI | clean |
| `yarn build` | `lib/` of every package, `apps/web/dist` | clean |
| `yarn test` | all Vitest specs (below) | 24 files passed, 1 skipped (m4-flow); 93 tests passed, 1 skipped |
| `yarn verify` | all of the above in order; required before every commit | exit 0, about 12 min with the images present |
| `LLMCHIP_FULL_FLOW=1 yarn test tests/m4-flow.spec.ts` | full S0–S8 on the proxy, G4, export | see §6.3 |
| `yarn llmchip events verify` / `artifact verify` | hash chain, stored content against hashes | `ok: N … checked` |
| `yarn demo [--until G0..G4]` | drives project `demo` through the positive path of the full flow against the running kernel, for the Flow page | S0–G4 passed and exported, 2241 s (about 37 min) with containers at 16 CPUs / 12 GB |
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
| `tests/recovery.spec.ts` | acknowledged writes survive repeated SIGKILL | NFR-04 | always |
| `tests/workflow.spec.ts` | indeterminate on incomplete parse, crash or unqualified capability; waivers; approvals, invalidation, conditions, branches | AC-M1-03/05/06 | always |
| `tests/agents.spec.ts` | iteration loop on change sets, permissions, loop and budget stops, fake-PDK egress, handoff, lowered standards | AC-M1-02/07/08/09/13 | always |
| `tests/mcp.spec.ts`, `tests/providers.spec.ts` | MCP boundary; OpenAI-compatible and Anthropic adapters against mock servers | FR-INT-05/06 | always |
| `tests/estimator.spec.ts` | three models, full breakdown; missing data gives "insufficient data" and G0 indeterminate | AC-M1-10 | always; **fails** (does not skip) without `.venv` |
| `tests/m2-flow.spec.ts` | S0 → G2 on stories260K; a new numeric spec invalidates G1/G2 | M2, AC-04 | always; **fails** without `.venv` |
| `tests/m4-kernel.spec.ts` | export of the released snapshot with lineage, hard-constraint slots, migration invalidation | FR-PKG-04, AC-08/10/13 | always |
| `tests/model-import.spec.ts` | import of a tiny Qwen3 in the real Hugging Face layout, census, estimate from the census, graph, L0, the full test suite against transformers; llama2.c against the run.c port; a project's own proxy weights; a model without config.json reported, not guessed | FR-MDL-01/03/05/06 | always; needs `.venv` |
| `plugins/model-llama/tests/test_graph.py` | Llama (tied/untied), Mistral with sliding window, Qwen2, Qwen3 against transformers; llama2.c against the run.c port; duplicated tied output; truncated file; unsupported models | FR-MDL | always |
| `tests/kernel-footprint.spec.ts` | startup time and idle RSS of harnessd | NFR-01 (kernel part) | Linux |
| `tests/web-ui.spec.ts` | catalog parity; every page in both languages in a browser | AC-M1-12 | browser part needs `apps/web/dist` and Chromium |
| `tests/containers.spec.ts` | no network, read-only inputs, container removed on cancel | IF-3 | needs `python:3.12-slim` image |
| `tests/calibration.spec.ts` | every storage candidate ends available or unavailable; stage entry on views | AC-M15-01/02/03 | needs EDA image |
| `tests/m3-flow.spec.ts` | verification plan, G3, injected defect localized | M3, AC-03/09 | needs EDA image |
| `tests/ui-latency.spec.ts` | p95 < 200 ms for UI queries during EDA runs | NFR-03 (kernel side) | needs EDA image |
| `tests/m4-flow.spec.ts` | S0–S8, DRC/LVS/STA, negative timing and SDF cases, G4, export | M4, AC-02/05/06/11/12/14 | only with `LLMCHIP_FULL_FLOW=1` **and** the EDA image |

### 6.3 Opt-in full flow

`LLMCHIP_FULL_FLOW=1 yarn test tests/m4-flow.spec.ts` runs the physical tools. Run it after any change to `rtl-taalas`, `eda-gf180`, `packager` or the signoff checks. Result on 2026-10-19: passed (1 test, 2361 s, about 39 min, with Podman 3.4 on WSL2).

---

## 7. Current limitations

### 7.1 Not implemented

| Gap | Requirements |
|---|---|
| Tauri desktop shell, Windows installer, dedicated WSL distro, first-launch onboarding, automatic image pull | FR-UI-01, FR-UI-03, NFR-08, NFR-13, AC-M1-01, AC-01, ADR-002, ADR-005 |
| CHARTER.md generation and history, stage playbooks, sample project | FR-PRJ-02, FR-PRJ-06, FR-AGT-11 |
| Project git working directory; merging a change set records state only | FR-TRC-09, architecture §6.2–6.3 |
| S9 post-silicon: firmware, host program, L7 in the Playground | FR-SIL-*, M6 |
| Training the reference model (owner-deferred; the proxy stands in) | FR-QNT-05, requirements §11.1 |
| Graph recovery outside the Llama family (GPT-2, MoE, …), of GGUF (census only), and with RoPE scaling (e.g. Llama 3.1/3.2); download from Hugging Face inside the workbench | FR-MDL-01, FR-MDL-02, FR-MDL-03 |
| Project export and import, reproduce command, audit export | FR-PRJ-05, FR-TRC-07, FR-TRC-08 |
| Artifact purge, GC and retention | architecture §4.2, FR-AGT-20 |
| Externally launched coding agents managed by the kernel | FR-AGT-03 |
| `docs/adr/` records, `images/` build definitions | architecture §14, §15 |

### 7.2 Partial

- **Estimator**: multi-chip is a chip count only (FR-EST-06). No sweep or Pareto (FR-EST-08). Calibration does not update the model automatically (FR-EST-10).
- **Calibration** covers SRAM 512×8 only of the four configurations (FR-EST-13).
- **Verification**: only `rtl-functional-sim` is a declared, qualified capability. Gate-level simulation has no capability gate. SDF simulation runs, but `sdf.timing` cannot pass (§1.3, FR-VER-01/03, AC-12 positive case).
- **Storage contents check** (FR-SGN-07): the mapping is reversible and round-trip checked at generation. No signoff check compares macro contents, because the design uses a standard-cell ROM.
- **Physical**: the die is 1.2×2 mm, not 2 mm² (owner decision, requirements v0.6). The design subject is the untrained proxy.
- **Migration**: the assessment and gate invalidation work. Downstream artifacts are not marked invalid (FR-MIG-03, FR-TRC-05). There is no NDA PDK import channel with automatic labelling (FR-INT-04, FR-SEC-01).
- **Agents**: no change set size threshold (FR-AGT-06). No soft-constraint path (FR-AGT-09). No per-role tool profiles or LLMs (FR-AGT-14/19). The declassified handoff pack is suggested, not generated (FR-AGT-13a).
- **UI**: no conditional approval or condition closing in the UI. No incident filters or jump links. No RTL, waveform or layout viewer beyond PNG. Settings lack LLM, quota and egress configuration (FR-UI-09/10/11/13, FR-AGT-25/26).
- **CLI**: approvals, waivers, declassification, export and migration have no dedicated commands. Use `llmchip call` (FR-INT-07).
- **TypeScript plugin SDK** has no tests of its own. It is exercised only through `stub-tool`.
- **Imported Hugging Face models** stop at S1: S2–S8 consume the llama2.c Model IR (`model.ir`), not the recovered graph (`model.graph`). L0 runs the graph without a KV cache (full sequence per step), so long generations on large models are slow.

### 7.3 Conflicts between code and upstream documents (open, reported, not resolved)

| # | Upstream statement | Code | Scope |
|---|---|---|---|
| C1 | ADR-008 / architecture §4.8: the gateway is the only module holding outbound connections; providers only construct requests | `createProvider` calls `gateway.check`, then `fetch` itself | K8 |
| C2 | Architecture §9.3 and §12.1: containers have a read-only root file system and can write only to `out/` of the run workspace | `Scheduler.containerCommand` passes no `--read-only`; `out/` and `work/` are mounted writable | IF-3 |
| C3 | Architecture §14 layout (`agent-runtime/`, `llm-providers/`, `mcp-server/`, `plugin-sdk-ts/` packages) | agent runtime and providers are in `packages/kernel/src/agents/`, MCP in `packages/cli/src/mcp.ts`, SDK in `packages/plugin-sdk` | layout ("Proposed") |
| C4 | Architecture §6.2: change set writing into the project git working directory "arrives with the generators in M2" | not implemented in M2–M4 | §6 |
| C5 | Architecture §3, §6.1: the agent runtime and LLM providers are in-kernel system plugins | `AgentRuntime` is a plain class built by the `Kernel` constructor; providers are kernel code; nothing goes through `PluginHost` | §6, ADR-001 |
| C6 | Architecture §4.3: a run's outputs are registered all at once, only after the run succeeds | outputs are stored one by one; an invalid later output leaves the earlier ones in the store, unlisted by the run | K3/K4 |
| C7 | Architecture §5 item 5: shutdown stops the agent runtime before flushing the event store | `Kernel.shutdown` never stops `AgentRuntime`; a running `tasks.run` loop is not ended first. Whether it can append after the database closes has not been checked | K1, §6 |

Until the owner decides, keep the code's current behavior and do not build on either side of these items.
