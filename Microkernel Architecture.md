# llmchip-harness Microkernel Architecture

English | [中文](<Microkernel Architecture.zh.md>)

| Item | Content |
|---|---|
| Document version | v0.4 (draft) |
| Date | 2026-10-18 |
| Status | Under review, not yet baselined (v1.0 is the formal baseline) |
| Sibling document | [Coding Harness MVP Requirement Analysis](<Coding Harness MVP Requirement Analysis.md>): describes "what to build" (corresponds to v0.4) |
| Chinese version | [Microkernel Architecture.zh.md](<Microkernel Architecture.zh.md>) |

This document answers **"how llmchip-harness is organized and extended"**. "What must be built, and to what extent" is answered by the requirements document. Per the rules of §0.3 of the requirements document: this document must not modify requirements or acceptance criteria; when the two conflict, agents must escalate and must not choose on their own.

---

## 0. Document Conventions

### 0.1 Numbering

| Prefix | Meaning | Example |
|---|---|---|
| AP | Architecture principle | AP-3 |
| K | Kernel component | K4 Scheduler & Process Supervisor |
| IF | External or cross-process interface | IF-2 Plugin protocol |
| ADR | Architecture Decision Record | ADR-001 |

The numbering stability rules are the same as §0.1 of the requirements document: after the v1.0 baseline, numbers are not reused, deletions are marked "Deprecated", and splitting or changing meaning creates new numbers.

### 0.2 Traceability

Every kernel component and key design states the requirement IDs it satisfies. The complete mapping is in the §13 requirements traceability matrix.

### 0.3 Terminology

This document uses the terminology of §0.2 of the requirements document, with the following additions:

| Term | Description |
|---|---|
| harnessd | The workbench's kernel process; a resident background Node.js process |
| In-kernel plugin | A TypeScript plugin running inside the harnessd process; used only for reviewed system plugins shipped with the product |
| Out-of-process plugin | A plugin running in a separate process or container; can be written in any language and communicates with the kernel via the plugin protocol (IF-2) |
| Run workspace | A directory created temporarily for a single run (Run); inputs are mounted read-only and outputs are collected from a fixed directory |
| Flow pack | A plugin that defines stages, gates, stage entry conditions, and rollback rules. S0–S9 are provided by the default flow pack |
| Projection | A read-only view derived from the event log, e.g. project state, golden chain state; can be discarded and rebuilt at any time |
| PEP / PDP | Policy Enforcement Point / Policy Decision Point |
| TCB | Trusted Computing Base, i.e. the minimal set of code on which the system's security depends |

---

## 1. Architecture Drivers

The architecture is driven by the following requirements. Each corresponds to a problem that must be solved structurally, rather than by "being careful during implementation".

| Driver | Source requirements | Structural answer |
|---|---|---|
| No call crashes or hangs the application | NFR-02, NFR-03, DP-7 | The kernel does no heavy computation; all tools, computation, and untrusted code run out of process (AP-5) |
| Recoverable after power loss or crash | NFR-04, FR-TRC-03 | Write the event first, then change state; state is derived from events; run outputs are committed all at once only on completion (AP-3) |
| End-to-end traceability | FR-TRC-01/02, AC-08 | Append-only event log + content-addressed artifact store + lineage graph |
| Agents are safe and controllable | FR-AGT-18~23, AC-M1-07 | All side effects pass through a single policy enforcement point (AP-4) |
| NDA data never leaves the machine | FR-SEC-01~09, AC-M1-08 | Labels are computed and propagated by the kernel; all outbound traffic passes through the single egress gateway |
| Running is not passing; not checked is not the same as no problem | DP-8, DP-10, §3.9 | Plugins only produce metrics; verdicts are made by the kernel's verdict engine (AP-7) |
| Approvals are bound to a definite version of the design | DP-9, §3.7 | A snapshot is a manifest of artifact hashes; approvals, evidence, and invalidation propagation are all based on hashes |
| Every capability is pluggable | DP-5, FR-INT-01, NFR-09 | Microkernel: the kernel only provides mechanisms; all domain knowledge lives in plugins and flow packs (AP-1, AP-2) |
| Lightweight, out of the box | NFR-01, NFR-13 | Tauri shell; a single kernel process + SQLite; a dedicated WSL distro with the entire runtime environment preinstalled |
| Can later move to a server and multi-user use | NFR-08 | Clients and kernel communicate via a network-style API from the start (IF-1) |

---

## 2. Architecture Principles

| ID | Principle | Meaning |
|---|---|---|
| AP-1 | Minimal kernel | The kernel contains only generic mechanisms: events, artifacts, workflow, scheduling, policy, verdicts, snapshots, labels, plugin host. **No** chip, model, or EDA domain knowledge **appears** in the kernel |
| AP-2 | Separate mechanism from policy | The kernel provides "how to execute"; plugins and configuration provide "what to execute and by what standard". For example, gate definitions, signoff checklists, and coverage completeness conditions all come from flow packs and the charter, not hard-coded in the kernel |
| AP-3 | Single write path | Every state change is expressed as an event appended to the event log. Project state, golden chain state, etc. are all projections of events |
| AP-4 | Single enforcement point | All side effects, such as file writes, tool runs, network egress, and approvals, must pass through the kernel's capability broker (PEP), be decided by the policy engine (PDP), and be recorded |
| AP-5 | Out-of-process execution | Heavy computation, EDA tools, third-party plugins, and untrusted code all run outside the kernel process. The kernel's event loop only does I/O and coordination |
| AP-6 | Contract before implementation | The kernel API, plugin protocol, and artifact types are all defined with language-independent schemas (JSON Schema) and carry version numbers. TypeScript types are generated from the schemas and do not depend on any framework types |
| AP-7 | Do not trust plugin conclusions | Plugins can only produce artifacts and metrics; they cannot directly write "pass". Verdicts are made by the verdict engine based on check definitions, metrics, evidence binding, and coverage completeness conditions |
| AP-8 | Deny by default | Permissions, egress, and label downgrades are all denied by default; release requires an explicit authorization or rule |

---

## 3. System Overview

```
┌──────────────── Clients (display and interaction only) ────────────────────────────────────────────────────┐
│  Tauri desktop app (Windows)    Browser    CLI (llmchip)    External agent (MCP)                           │
└───────────────┬───────────────────────────────┬───────────────────────────────┬────────────────────────────┘
                │ IF-1 Kernel API: JSON-RPC 2.0 over WebSocket + HTTP (artifact upload/download)
┌───────────────▼────────────────────────────────────────────────────────────────────────────────────────────┐
│ harnessd (Node.js · TypeScript · in-kernel plugins assembled with Cordis, see ADR-001)                     │
│                                                                                                            │
│  Microkernel                                                                                               │
│   K1 Event Store    K2 Artifact Store   K3 Workflow Engine      K4 Scheduler & Process Supervisor          │
│   K5 Policy Engine  K6 Verdict Engine   K7 Snapshot & Approval  K8 Labels & Egress Gateway                 │
│   K9 Plugin Host    K10 Config & Secrets                                                                   │
│                                                                                                            │
│  In-kernel system plugins (shipped with the product, reviewed)                                             │
│   Agent runtime · LLM providers · MCP service · API gateway · Default flow pack · Projections              │
└───────┬────────────────────────────────────────────────────────┬───────────────────────────────────────────┘
        │ IF-2 plugin protocol (JSON-RPC over stdio)             │ IF-3 tool runs (Podman containers)
┌───────▼──────────────────────────────────────────────┐  ┌──────▼───────────────────────────────────────────┐
│ Out-of-process domain plugins (any language)         │  │ Tool containers (versions pinned by image digest)│
│ Model ingestion · Quantization & numeric spec · L2   │  │ Yosys · OpenROAD · OpenSTA · KLayout ·           │
│ Estimator · Architecture templates · RTL generators  │  │ Magic · Netgen · Verilator · Icarus ·            │
│ Parsers · Post-silicon testing ……                    │  │ cocotb · OpenRAM ……                              │
└──────────────────────────────────────────────────────┘  └──────────────────────────────────────────────────┘
                 All running in the dedicated WSL distro llmchip-harness (Windows)
```

A typical work process is as follows:

1. An agent in the Agent runtime requests an action via a tool call, e.g. "run synthesis".
2. The request enters the capability broker; the policy engine (K5) checks permissions, scope, and quotas, and writes the decision and reason into the event log (K1).
3. The workflow engine (K3) creates a run (Run); the scheduler (K4) prepares a run workspace for it, mounts the input artifacts read-only, and starts the tool in a container.
4. After the tool finishes, K4 collects the outputs; the artifact store (K2) saves them by content hash and records lineage; labels are computed by K8 from the input labels.
5. A parser plugin parses the reports into metrics; the verdict engine (K6) derives the four-layer verdicts from the check definitions.
6. Projections are updated, and the UI sees the changes in real time via subscription.

---

## 4. Kernel Components

### 4.1 K1 Event Store

**Responsibility**: The single source of all facts. Satisfies FR-TRC-01, NFR-04.

- **Storage**: SQLite in WAL mode, synced to disk on commit. The kernel is the only writer.
- **Event record**:

  | Field | Description |
  |---|---|
  | seq | Globally increasing sequence number |
  | id | Globally unique ID (ULID) |
  | ts | Time |
  | project | Owning project (may be empty, indicating a workspace-level event) |
  | actor | Actor: human, agent (including role, LLM provider, and model name), plugin, kernel |
  | kind | Event type. Kernel types such as `artifact.created`, `run.state`, `verdict.recorded`; plugin types are namespaced |
  | payload | JSON, validated against the schema registered for that event type, with a schema version |
  | causation / correlation | Causal chain: which event caused it, and which operation it belongs to |
  | label | Data label (K8) |
  | req_ref | The requirements document version or hash it is based on (when needed) |
  | prev_hash / hash | Hash chain, used to detect tampering or corruption |

- **Large data does not go into events**: logs, reports, and files are stored in the artifact store; events only reference hashes.
- **Projections**: project state, golden chain, incident list, pending approvals list, etc. are all projections and can be discarded and rebuilt from events at any time. Projection updates are done in the same transaction as the event write, or caught up at startup.
- **Real-time notification**: after an event is committed, it is pushed to subscribers (UI, Agent runtime) via an in-process bus. Lost pushes do not affect correctness; subscribers can catch up by seq.

### 4.2 K2 Artifact Store

**Responsibility**: Stores all artifacts, guaranteeing immutability and complete lineage. Satisfies FR-TRC-02, DP-2, FR-AGT-20.

- **Content addressing**: files are stored by SHA-256 under `store/objects/` and set to read-only after writing. Directory artifacts (e.g. an RTL directory, a PDK) are represented by a Merkle manifest, and the manifest itself is also an artifact.
- **Metadata** (stored in SQLite): type, media type, label, the run that produced it, input artifacts, tool image digest, creator, the requirements document version it is based on.
- **Lineage graph**: composed of "run inputs → run → run outputs". Trace queries (AC-08) are performed directly on this graph.
- **Deletion**: agents and plugins can only emit "mark deleted" events. Actual cleanup can only be initiated by a human and is performed by garbage collection after a retention period. Artifacts referenced by any valid snapshot, approval, or tapeout package cannot be collected.
- **External large files**: model weights and PDKs are imported only once. On import, the hash is computed, labels are applied (an NDA PDK is automatically NDA), and a fingerprint index is built (K8).

### 4.3 K3 Workflow Engine

**Responsibility**: Advances project → stage → task → run in a recoverable way. Satisfies FR-TRC-03/04/05, FR-PHY-08, FR-MIG-03.

- **Persistent state machines**: projects, stages, tasks, and runs are all event-driven state machines; the current state is a projection of events. The kernel only knows generic states, e.g. pending, running, pending approval, completed, invalidated.
- **The flow is defined by the flow pack**: stages S0–S9, gates G0–G4, transitions between stages, rollback rules, and **stage entry conditions** (e.g. the integration views check of FR-PHY-08, "approval valid") are all declared by the flow pack. The kernel executes them; the verdict engine and policy engine evaluate the conditions. A future multi-chip flow can be added as another flow pack.
- **Atomic commit of runs**: a run's outputs are registered as artifacts all at once only after the run ends successfully. On a crash midway, partial results stay in the run workspace and do not enter the artifact store, so there is no such thing as a "half-written artifact".
- **Invalidation propagation**: the kernel maintains three kinds of dependency edges: artifact → derived artifact, evidence → checked object, approval → snapshot. When any object gets a new version, the affected set is computed along the dependency edges and invalidation events are emitted. The rollback starting point given by the migration impact assessment (FR-MIG) is also executed via the same mechanism.
- **Branching and rollback**: a project branch is a named pointer to a set of stage states. Rolling back to a historical checkpoint = creating a new branch from that checkpoint (FR-TRC-04, FR-PRJ-04); history is not modified.

### 4.4 K4 Scheduler & Process Supervisor

**Responsibility**: Executes runs, manages the lifecycle of all child processes and containers, and ensures the kernel is not dragged down. Satisfies NFR-02, NFR-03, FR-AGT-22/23.

- **Task queue**: stored in SQLite, not lost after a kernel restart. Queued by resource class (CPU, memory, future EDA licenses) and quota.
- **Three executors**:

  | Executor | Purpose | Isolation |
  |---|---|---|
  | Out-of-process plugin process | Domain plugins written in Python and other languages | Separate process, communicating via IF-2 |
  | Tool container | EDA tools | Podman container with CPU and memory limits, network disabled by default (implemented in M1.5, when Podman is installed) |
  | In-kernel execution | Only for extremely lightweight, non-blocking system operations | None, so its scope is strictly limited |

- **Run workspace**: every run gets a new temporary directory. Input artifacts are mounted read-only (hard links or bind mounts), and the tool can only write to the `out/` directory. After the run ends, the kernel collects the outputs from `out/` and commits them to K2.
- **Supervision**: heartbeats, timeouts, cancellation (SIGTERM first, SIGKILL after a timeout), pause on resource overrun. At kernel startup, orphan processes and containers are cleaned up.
- **Logs**: logs of a running run are pushed to the UI as a temporary stream; after the run ends, the full log is saved as an artifact, with its label inherited per K8 rules.
- **Emergency stop**: a global pause flag, checked by the scheduler and the Agent runtime at every scheduling point. When paused, the current state is preserved and committed data is not killed.

### 4.5 K5 Policy Engine & Capability Broker

**Responsibility**: Makes an allow, deny, or requires-approval decision for every action, and enforces it. Satisfies FR-AGT-01/02, FR-AGT-05, FR-AGT-18~23, the constraint classification of §3.2, AC-M1-13.

- **Single entry**: every action, whether from an agent tool call, MCP, the UI, the CLI, or a plugin callback, is first expressed as `action = {actor, capability, resource, parameters}` and handed to the policy engine for a decision; the decision and reason are written into the event log.
- **Capability model**:
  - Each role and each task receives a set of capability grants, including available tools, writable scope (path patterns in the project working directory), and quotas.
  - The tools of a managed agent are all kernel-defined operations, e.g. reading artifacts, submitting changesets, starting runs. **There is no raw shell**; "executing commands" can only be done in containers via declared tool adapters.
  - External clients use a separate capability profile: read-only by default, plus initiating tasks, and cannot approve (FR-AGT-02).
- **Constraint registry**: every constraint in the charter is registered with a category (hard / soft), classified per the minimum classification criteria of §3.2 of the requirements document; **when undeterminable, it is a hard constraint**. Modification requests for hard constraints are always converted into human approvals.
- **Lowered-standard detection**: when the check definition of a check changes, and at the same time its verdict changes from "fail" to "pass" while the hash of the checked object has not changed, an incident is automatically generated and handed to a human for review (AC-M1-13).
- **Dangerous operation list**: the operations listed in FR-AGT-21 always require human approval. NDA egress is not among the approvable items (handled by K8).
- **Policy itself is an artifact**: policy rules are versioned, and modifications are events, so it is traceable "which version of the rules a given decision was based on".

### 4.6 K6 Verdict Engine

**Responsibility**: Turns "run results" into "engineering verdicts" and ensures verdicts are trustworthy. Satisfies DP-8, DP-10, §3.9, FR-VER-01/03, FR-SGN-02a, FR-TRC-06.

- **Inputs**:
  1. The run record (layer ① execution status, written by K4);
  2. The output of the parser plugin (layer ② parse status, metrics, parser version);
  3. The check definition: verdict criteria, required tool capabilities, coverage completeness conditions; comes from the flow pack, signoff checklist, or charter, and is a hard constraint;
  4. The hash of the checked object in the current snapshot.
- **Output**: a verdict record (layer ③), with value Pass / Fail / Waived / Indeterminate, with a reason, and bound to the object hash, check definition version, and requirements document version. The stage conclusion (layer ④) is aggregated from verdicts and combined with approvals from K7.
- **Rules**:
  - Plugins cannot write verdict records; they can only produce metrics (AP-7).
  - If any input is missing, parsing is incomplete, coverage completeness conditions are not met, or tool capabilities do not match, the verdict is "Indeterminate".
  - **Tool capability qualification**: every capability declared by a backend (e.g. "SDF timing simulation") must come with a qualification test. The capability is recognized only after the qualification test passes on that tool image. The qualification test is itself a check, with the id `<plugin>.qualify.<capability>`; its passing verdict is the qualification evidence. For example, the qualification test for the SDF capability deliberately introduces a timing violation and checks whether the simulation can detect it.

### 4.7 K7 Snapshot & Approval

**Responsibility**: Turns "which version of the design was approved" into a machine-checkable fact. Satisfies §3.7, FR-PKG-04, FR-SGN-05/06, AC-10, AC-11, AC-M1-05/06.

- **Snapshot**: member artifacts are selected according to the gate definition (from the flow pack), producing a sorted hash manifest; the hash of the manifest is the snapshot hash.
- **Evidence manifest**: the set of verdict records the approval is based on, likewise identified by hash.
- **Approval state machine**: Approved / Conditionally approved · conditions pending / Conditionally approved · all conditions closed / Rejected / Invalidated; rules are in §3.7.3 of the requirements document. When a condition is closed, it is validated that: the closure evidence has received a "Pass" verdict, and the snapshot hash has not changed.
- **Release check**: `can_release(gate, snapshot)` is the single check function before advancing the flow and exporting the tapeout package. It simultaneously checks the approval state, whether the snapshot hash matches exactly, and whether any unclosed blocking conditions exist.
- **Waivers**: internal risk acceptance and manufacturing-rule waivers are two different record types. A manufacturing-rule waiver must reference a "foundry written acceptance" artifact, and that artifact is bound to the GDS hash, rule deck version, violation type, and location.
- **External reports**: an imported external signoff report is a kind of artifact; on import, the object hash it claims to have checked, the rule version, tool version, and check conditions are registered. The verdict engine uses it only when this information exactly matches the current snapshot (FR-SGN-06).

### 4.8 K8 Data Labels & Egress Gateway

**Responsibility**: Ensures NDA data and its derived data do not leave the machine. Satisfies FR-SEC-01~09, FR-AGT-13a, DP-6.

- **Label lattice**: Public < Internal < NDA, extensible. Every artifact, event, log, and context fragment has a label.
- **Labels are computed by the kernel**: when a run is committed, K8 sets the label of all outputs to the highest level among all input labels. Plugins and tools cannot lower labels on their own. An agent request is assembled from multiple context fragments, and the request's label is the highest among them.
- **Single egress gateway**:
  - The gateway is the only module in the kernel that holds the capability for outbound network connections. LLM provider plugins only construct requests; **actual sending is done through the transport channel provided by the gateway**.
  - Out-of-process plugins and tool containers have no network by default; when network is needed (e.g. downloading a model), it can only go through the gateway proxy and is checked by the same rules.
  - Every destination is registered with its highest permitted label: cloud LLMs default to "Internal", local models can be NDA, external MCP clients default to "Internal".
  - Every request sent to a destination, and every response (for MCP clients), is checked: the label of a content fragment must not exceed the level permitted by the destination.
- **Declassification**: a declassification rule is an artifact that only the owner can create, precisely specifying which structured fields may be extracted from which kind of NDA artifact. Declassification is performed by the kernel, producing new lower-level artifacts and recording lineage. The default rule set is empty.
- **Fingerprint scanning**: when NDA materials are imported, the kernel extracts distinctive fragments (layer names, rule names, cell names, characteristic text) and builds a fingerprint index. The gateway does multi-pattern matching on all outbound content; on a hit it intercepts and generates an incident. This is a second line of defense beyond the labeling mechanism.
- **Audit**: every outbound request produces an egress event recording the destination, the labels of each fragment, and the release or intercept conclusion.

### 4.9 K9 Plugin Host

**Responsibility**: Discovers, validates, loads, and supervises plugins. Satisfies DP-5, FR-INT-01/02/09, NFR-09.

- **Plugin manifest** (`llmchip-plugin.json`):

  | Field | Description |
  |---|---|
  | id, version | Plugin identifier and version |
  | kinds | Plugin types, taken from the list in requirement FR-INT-01 |
  | runtime | `inproc-ts` (system plugins only) / `process` (any language) / `container` |
  | entry | Startup entry point |
  | kernelApi | Required kernel API version range |
  | contributes | Provided handlers, tools, check definitions, parsers, artifact type schemas, UI panels |
  | capabilities | Declared tool capabilities, and the qualification test for each capability |
  | requiresProcessCapabilities | Dependencies on process capabilities (FR-ARC-01), used by the migration impact assessment |
  | permissions | Permissions needed at runtime, e.g. whether network is required |
  | configSchema | JSON Schema of the plugin configuration |

- **Two plugin tiers**:

  | Tier | Where it runs | Scope | Impact of a crash |
  |---|---|---|---|
  | In-kernel plugin | Inside the harnessd process, assembled with Cordis | Limited to reviewed system plugins shipped with the product | Cordis confines load failures to that plugin, but runtime blocking or resource leaks can affect the kernel, so strict review is mandatory |
  | Out-of-process plugin | Separate process or container | The default for all domain plugins and third-party plugins | Only affects the current run; the plugin process is restarted by K4 |

- **Compatibility**: at load time, the kernel API version range and schema versions are checked. Incompatible plugins are refused, with a reason given.

### 4.10 K10 Config & Secrets

**Responsibility**: Manages layered configuration and secrets. Satisfies NFR-07.

- **Configuration layers**: built-in defaults → workspace configuration → project configuration. Each layer is validated against a schema. Configuration changes are events.
- **Secrets**: API keys and license configurations are stored encrypted; the encryption key is provided by the operating system's credential store (on Windows, the Tauri shell retrieves it from Credential Manager and hands it to the kernel at startup). Until the desktop shell exists (M1d), the kernel reads it from the environment variable `LLMCHIP_MASTER_KEY`; without it, a key file with mode 0600 is generated under `secrets/`.
- **Redaction**: logs and events pass through a secret-redaction filter before being written; secrets never enter artifacts or LLM context.

---

## 5. Where Cordis Fits in the Architecture (ADR-001)

**What Cordis is**: a TypeScript plugin and service composition framework. It handles: plugins register the services they provide and declare the services they depend on, and start only once dependencies are ready; when a plugin is unloaded, the listeners, timers, and services it registered are automatically cleaned up in reverse order; when a plugin fails to load, only that plugin is marked as failed and the process does not crash.

**What it does not handle**: cross-process and cross-language operation, tool crash isolation, task cancellation and timeouts, persistence, sandboxing and permissions. These are precisely the hardest parts of this workbench and must be implemented by the kernel itself (K1–K9).

**Decision (status: accepted; M1a prototype validation passed, subject to the constraints in item 6)**: the owner decided on 2026-10-18 to vendor the source first; the manifest is in `vendor/README.md`.

1. Cordis is used **only** as a tool inside harnessd for assembling kernel components and in-kernel system plugins.
2. **Kernel interfaces do not depend on Cordis**: the kernel API, plugin protocol, and artifact types are all defined in a separate `kernel-api` package (AP-6), in which no Cordis types appear. Out-of-process plugins are completely unaware of Cordis's existence.
3. Based on the already-patched Cordis fork in the local `deepseek-harness` project (MIT license), copied as source into `vendor/`, **with the version locked**, and recording its origin and the list of local modifications.
4. Critical paths do not depend on Cordis's event mechanism: event writes, verdicts, and approvals call kernel components directly. The reason is that in Cordis's synchronous event dispatch, after one listener throws an exception, subsequent listeners are not executed.
5. **Unload order is managed explicitly**: the kernel shutdown order is fixed as: stop accepting new requests → the scheduler stops dispatching and waits for or aborts runs → stop the Agent runtime → flush the event store → close the database. It does not rely on Cordis's concurrent unloading.
6. **Plugin host guards** (Cordis behaviors found in the M1a prototype validation, handled by `PluginHost`):
   - Cordis waits for a plugin's setup to settle before disposing it, so a hung plugin would block shutdown forever. Loading and cleanup therefore have timeouts; a timeout is recorded as `plugin.failed`.
   - When a service provider is unloaded directly, Cordis runs the provider's own cleanup before deactivating the plugins that depend on it. In-kernel plugins must therefore declare `provides` and `inject`, and the host unloads a provider's dependents before the provider.
   - Plugins are unloaded one at a time, in reverse load order.
   - Cordis cannot contain an uncaught exception thrown from an asynchronous callback of an in-kernel plugin. This is one reason only reviewed system plugins may run in the kernel (§4.9).

**Exit criteria for M1 prototype validation**:
- Unload order test: shut down the kernel while a run is in progress; no events are lost and no resources are closed prematurely.
- Fault isolation test: an in-kernel plugin fails to load or throws an exception at runtime; the kernel and other plugins are unaffected.
- Size and startup time meet §10.1 of the requirements document.

**Validation results (M1a, 2026-10-18)**:

| Exit criterion | Result | Evidence |
|---|---|---|
| Unload order | Passed: a plugin in cleanup can still use the services and storage it depends on; `kernel.stopped` is written after every `plugin.unloaded`; unloading a provider directly unloads its dependents first | `packages/kernel/tests/plugin-host.spec.ts` |
| Fault isolation | Passed: plugins that throw synchronously, reject asynchronously or hang while loading are contained and recorded, and the kernel keeps serving; a plugin whose cleanup hangs does not block shutdown | Same |
| Size and startup | Passed: Cordis build output 256 KB (748 KB for all 9 vendored packages), import takes about 7 ms; harnessd starts in about 185 ms and idles at about 83 MB resident memory | `tests/kernel-footprint.spec.ts` |

**Fallback**: if a problem is found later that these guards cannot solve, replace Cordis with a small in-house registry (service registration, dependency waiting, reversible registration). Because of item 2, the replacement only affects the assembly code inside harnessd.

---

## 6. Agent Runtime

The Agent runtime is an in-kernel system plugin. Its work is mainly waiting for streaming output from LLMs, which is I/O-bound and does not block the kernel; all tool execution is handed to K4. Satisfies the FR-AGT items of §6.

### 6.1 Composition

- **Agent instance** = role configuration + LLM binding (provider, model) + capability grants + context builder. Each role can be configured with an LLM independently and switched while the project is in progress (FR-AGT-14).
- **Context builder**: assembles requests from the charter, stage playbooks, current task, and related artifacts and events. Every fragment carries a label, and a request is sent only after K8 checks it. External content (model files, documents, tool outputs) is wrapped in a "data" format and not treated as instructions (FR-AGT-24).
- **Tool set**: contains only kernel-defined operations, filtered by K5 according to grants.

### 6.2 How the Iteration Loop Is Enforced

The six-step loop of §6.2 of the requirements document is implemented architecturally as a changeset flow enforced by the kernel:

| Step | Implementation in the architecture |
|---|---|
| ① Clarify the goal | When creating a task, a charter goal or requirement ID must be referenced, otherwise K5 rejects it |
| ② Bound the structure | A task declares its write scope; K5 checks the scope on every write, and out-of-scope writes are rejected and generate an incident |
| ③ Implement in small steps | An agent can only use the "submit changeset" tool to write changes to a changeset branch of the project's git working directory. Changesets exceeding the size threshold are rejected |
| ④ Review | A changeset must have a review record from a **different agent instance** before it can proceed |
| ⑤ Actual verification | The verification runs associated with the changeset must receive a "Pass" verdict from K6 |
| ⑥ Update constraints | When incidents and failures are closed, the Agent runtime generates constraint proposals; soft constraints are approved by the reviewer, hard constraints are routed to human approval |

Only when all six steps are complete does the kernel merge the changeset into the project mainline. This is checked by the kernel, not left to agent self-discipline.

### 6.3 Project Working Directory

- Each project has a git working directory that stores agent-written generator code, constraint files, `CHARTER.md`, and stage playbooks (FR-TRC-09).
- The contents of the working directory are packaged as an artifact before every run, so the generator code used by each run has a definite hash.
- Generated RTL, netlists, layouts, etc. are **not** written back to the working directory; they are only saved as artifacts (DP-3).

### 6.4 Handoff and Switching

- The **handoff pack** is a projection: generated from the charter, current stage and tasks, golden chain state, unhandled incidents, recent decisions, the constraint list, and suggested next steps. It is itself also a labeled artifact (FR-AGT-12).
- **Data permission check before switching**: before switching an agent or LLM, K8 compares the label of the handoff pack with the level permitted for the target provider. If permissions are insufficient, the switch is blocked and two options are offered: "switch to a local model" or "generate a declassified handoff pack per a declassification rule" (FR-AGT-13a).
- **Session interruption**: an agent's state exists only in events and artifacts (DP-4), so interruption at any time loses no progress.

### 6.5 Spin Detection and Incidents

Incident rules run as subscribers on the event stream. For example, when "the same failure signature occurs N times in a row" or "a task's time or token consumption exceeds a threshold", the task is paused, an incident is generated, and it is escalated to a human (FR-AGT-16).

### 6.6 External Agents

- **External clients**: the MCP service plugin maps MCP requests to kernel API calls, using the external client capability profile, and responses are checked by K8. The UI and connection instructions clearly state the control boundary (FR-AGT-01).
- **External coding agents launched under management (P1)**: the workbench launches Claude Code, Codex CLI, etc. in a container that mounts only the changeset branch of the project working directory, with network only via the egress gateway proxy. Their output must likewise go through the loop of §6.2, so they become managed agents (FR-AGT-03).

---

## 7. Domain Plugins and Extension Points

### 7.1 Plugin Types and Contracts

Every plugin type is defined by a contract of "input artifact types → output artifact types". The table below lists the main extension points of the MVP:

| Plugin type | Input → Output | Default execution mode | MVP implementation |
|---|---|---|---|
| Flow pack | — → stages, gates, entry conditions, snapshot member definitions | In-kernel (declarative data) | Default flow S0–S9, G0–G4 |
| Model loader | Weight files, tokenizer → Model IR, structure report | Out-of-process (Python) | llama2.c, safetensors, PyTorch |
| Quantizer / numeric spec | Model IR → numeric spec, L2 bit-exact model, accuracy report | Out-of-process (Python) | RTN quantization + fixed-point numeric spec |
| Small model training | Dataset, size → model weights | Out-of-process (Python) | Based on the llama2.c training flow |
| Estimator | Model metadata, process profile, implementation library → estimation report | Out-of-process (Python) | Area, cost, minimum performance estimation |
| Architecture template | Numeric spec, process profile, implementation library → chip specification | Out-of-process | Taalas-style hard-wired template |
| RTL generator / storage mapping | Specification, quantized weights → RTL, storage block contents; mapping rules must provide inverse restoration | Out-of-process | Generator for the corresponding template |
| Simulation and verification backend | RTL or netlist, tests → simulation results; declares capabilities and qualification tests | Container | Verilator, Icarus, cocotb |
| EDA adapter | Design artifacts, constraints → tool outputs | Container | Yosys, OpenROAD, OpenSTA, KLayout, Magic, Netgen, OpenRAM |
| Parser | Tool reports → metrics (including parse completeness) | Out-of-process | Paired with each EDA adapter |
| PDK / process profile / implementation library | — → process data, implementation entries, integration views, signoff checklist | Data plugin | GF180MCU, Sephonchitech (十方尺) (pricing and size rules) |
| Signoff check | — → check definitions (criteria, capability requirements, coverage completeness conditions) | Data plugin | DRC, LVS, STA, antenna, density, storage contents consistency |
| Tapeout package template | Snapshot → tapeout package manifest | Data plugin | GF180 rehearsal template |
| Post-silicon testing | Specification → firmware, host PC program, test vectors; test results → comparison | Out-of-process | RP2040-class host controller |
| LLM provider | Request → streaming response (sent via K8) | In-kernel | DeepSeek, Claude, GPT, OpenAI-compatible interfaces |
| UI panel | Projection → view | Front end | Estimator, golden chain, layout viewer, etc. |

### 7.2 Core Artifact Types

Artifact types are managed by a schema registry (AP-6), and plugins can register new types. The core types of the MVP include: Model IR, numeric spec, L2 model, accuracy report, chip specification, mapping rule, RTL tree, netlist, layout (GDS), constraint set, timing exception list, verification plan, tool report, metrics, verdict record, snapshot manifest, evidence manifest, approval record, waiver record, external report, implementation library entry, migration impact assessment report, tapeout package, handoff pack.

### 7.3 Where the Golden Chain Lands in the Architecture

- Each level in the chain is a kind of artifact (L0 reference outputs, L2 model, L3 simulation results, …), and the relationship between levels is a check definition: the ≈ relationship corresponds to a statistical evaluation check, and the ≡ relationship corresponds to a bit-by-bit comparison check at the observation points of the numeric spec.
- The golden chain state is a projection: computed from the latest verdict records and invalidation states of each link.
- For divergence localization (FR-VER-06), the comparison check plugin produces a "first divergence point" metric, which the UI uses to jump to the corresponding waveform and tensor.

---

## 8. Data and Storage Layout

In the dedicated WSL distro, the workbench's home directory structure is as follows:

```
~/.llmchip/
  config/                 Layered configuration
  secrets/                Encrypted secret files
  db/harness.sqlite       Events, metadata, projections, task queue
  store/objects/          Content-addressed artifact store (read-only files)
  projects/<id>/worktree/ Project git working directory (generators, constraints, CHARTER.md)
  runs/<run-id>/          Temporary run workspaces (in/ mounted read-only, out/ for outputs)
  plugins/                Installed plugins
  images/                 Podman image storage (managed by Podman)
```

- All data is placed in WSL's Linux file system, **not under `/mnt/c`**, to ensure file I/O performance and permission semantics.
- Project export (FR-PRJ-05) = event subset + referenced artifacts + working directory, filtered according to the label rules of §8 of the requirements document.

---

## 9. Interfaces

### 9.1 IF-1 Kernel API

- **Protocol**: JSON-RPC 2.0 over WebSocket for calls and subscriptions; HTTP for uploading and downloading large artifacts.
- **Authentication**: in local deployment, a random token is generated at each startup and handed to local clients. In future server deployment, it is replaced by formal authentication with TLS enabled (NFR-08); the API itself does not need to change.
- **Consistency**: the desktop application, browser, CLI, and MCP service all use the same API, so anything the UI can do the CLI can certainly do (FR-INT-07).
- **Subscription**: clients subscribe to changes in events and projections by seq, and after a disconnect continue from the last seq.
- **Internationalization**: messages returned by the kernel use message codes plus parameters rather than pre-written sentences, and the UI renders them in the current language (FR-UI-02).

### 9.2 IF-2 Plugin Protocol

- **Transport**: JSON-RPC 2.0, one JSON message per line, over stdin/stdout; stderr carries free-form log lines, which the kernel records in the run log. Each run starts its own plugin process, which leads its own process group, so cancellation or a timeout terminates the whole group. Plugin processes receive no secrets in their environment.
- **Commands in the plugin manifest**: the kernel substitutes `${python}`, `${node}` and `${pluginDir}` in `entry.command`. Plugins with `runtime: data` have no code and only provide data files (for example flow packs, process profiles and implementation libraries); the kernel imports the data files as artifacts at registration, so every use is traceable by hash.
- **Main methods**: `initialize` (exchange versions and capabilities), `invoke` (execute a handler; inputs and outputs are artifact references and paths in the run workspace), `progress` and `log` (notifications), `cancel`, `shutdown`.
- **Data passing**: large data is passed via files in the run workspace, not transmitted inline in the protocol.
- **SDK**: a Python SDK (`python/llmchip-sdk`) and a TypeScript SDK (`packages/plugin-sdk`) are provided, encapsulating protocol details; the SDKs redirect a handler's standard output to stderr so that the protocol stream cannot be corrupted. Other languages can implement it according to the protocol documentation.
- **Results**: `invoke` returns only output files, metrics and the parse status, never verdicts (AP-7). Outputs must lie in the run workspace's `out/` and match the types declared in the manifest; otherwise the run fails and no output is registered.

### 9.3 IF-3 Tool Runs

- Each EDA adapter declares: image digest, command template, input/output mapping, resource requirements, parser.
- Containers have no network and a read-only root file system by default; only `out/` of the run workspace is writable.
- Tool images are version-pinned by digest to ensure reproducibility (NFR-10).

### 9.4 Front End and Desktop Shell

- **Front end**: a single web front end, with static assets served by harnessd, shared by the browser and the desktop application.
- **Tauri shell (Windows)**: responsible only for the following, containing no business logic:
  - Installation and first-launch onboarding;
  - Ensuring the WSL distro and harnessd are running;
  - Retrieving secrets from the system credential manager and handing them to the kernel;
  - Tray, notifications, window management.
- Closing the window does not stop harnessd; tasks keep running.

---

## 10. Deployment and Installation

### 10.1 MVP: Windows + WSL2

```
Windows
 ├─ Tauri desktop app (WebView2 renders the front end)
 │      │ localhost: IF-1
 └─ WSL2: dedicated distro "llmchip-harness"
        ├─ harnessd (Node.js runtime shipped with the distro)
        ├─ Python runtime (out-of-process plugins)
        ├─ Podman (daemonless, runs tool containers)
        └─ ~/.llmchip/ (§8)
```

**Why a dedicated WSL distro**: the installer imports a prebuilt distro that already has harnessd, Node.js, the Python runtime, and Podman installed. As a result:
- Users do not need to install Docker or configure any environment (NFR-13);
- It does not interfere with the user's existing WSL distros;
- Uninstalling only requires deleting this distro, cleanly and completely.

**Installation flow**:

1. Detect hardware virtualization. If it is not enabled in the BIOS, give machine-specific instructions; this is the only case requiring manual handling by the user (§10.1 of the requirements document).
2. Detect WSL. If not enabled, enable it with one click (requires administrator privileges, may require a reboot).
3. Import the dedicated distro.
4. Obtain tool images: pulled on demand when online, imported from the offline installer when offline. All images are verified by digest.
5. Start harnessd and enter first-launch onboarding: configure LLMs, run the environment self-check, run the sample project with one click.

### 10.2 Native Linux

Install a package with the same contents directly, without the WSL layer.

### 10.3 Future: Private Server (P2)

Deploy harnessd and `~/.llmchip/` on a private server, with clients on multiple computers connecting to the same workbench. Because the client–kernel interface has been a network-style API from the start (IF-1), only the following need to be added:
- Formal authentication and permission groups;
- TLS encrypted transport;
- Resource management for tool containers on the server.

### 10.4 Upgrades and Self-Check

- The kernel, plugins, and tool images are versioned separately. Database schema changes are done via migration scripts, with an automatic backup before migration.
- The environment self-check (NFR-14) checks: container runtime, image integrity, disk space, database integrity (hash chain verification). When a problem is found, a one-click fix is offered.

---

## 11. Reliability and Fault Model

| Fault | Scope of impact | System behavior |
|---|---|---|
| EDA tool crashes or reports an error | Current run | Layer ① is recorded as non-zero exit or crash; the verdict is "Indeterminate" or "Fail"; can be retried |
| Tool or plugin hangs | Current run | Triggered by heartbeat or timeout; K4 terminates the process; recorded as timed out |
| Out-of-process plugin crashes | Current run | K4 restarts the plugin process; the run can be retried |
| In-kernel plugin fails to load | That plugin | Cordis marks it as failed and the kernel keeps running; an incident is generated |
| LLM API failure or rate limiting | Current agent step | Retry with a backoff strategy; when a threshold is exceeded, pause the task and generate an incident |
| UI closed | None | harnessd and tasks keep running; after reopening, the subscription continues from seq |
| harnessd crash, power loss, or WSL stopped due to Windows hibernation | Runs in progress | After restart, the recovery flow below is executed |
| Disk full | New runs | The scheduler checks free space before dispatching; when insufficient, it pauses and generates an incident; committed data is unaffected |
| Tool image corrupted | Runs using that image | Digest verification fails, the run is refused, and a one-click fix is suggested |

**Kernel startup recovery flow**:

1. Open the database and verify the tail of the event hash chain.
2. If projections lag behind events, catch up or rebuild projections from events.
3. Reconcile runs: runs marked "running" in the database that have no live process are marked "interrupted".
4. Clean up orphan processes, containers, and leftover run workspaces.
5. Handle interrupted runs per the retry policy: if the plugin supports checkpoints, continue from the checkpoint, otherwise re-execute. Because inputs are immutable and runs are reproducible, re-execution is safe.
6. If the emergency stop flag is still active, stay paused; otherwise resume scheduling and the Agent runtime.

---

## 12. Security Model Summary

### 12.1 Trust Boundaries

| Zone | Trust level | Notes |
|---|---|---|
| Human (local UI) | Trusted | Approvals and policy changes can only be initiated by humans |
| Kernel (K1–K10) | TCB | Kept minimal; the fundamental basis of security |
| In-kernel system plugins | Trusted (reviewed) | Limited to plugins shipped with the product |
| Out-of-process plugins | Semi-trusted | Separate process, no network, can only access the kernel via IF-2 |
| Tool containers | Untrusted | No network, read-only root file system, can only write to `out/` of the run workspace |
| LLM providers | External | Can only receive content that has been checked by K8 |
| External MCP clients | External | The workbench can only control operations via IF-1; responses are checked by K8 |

### 12.2 How Managed Agent Constraints Are Guaranteed

A managed agent itself does not execute any code; it can only produce "tool call requests". Every request goes through a K5 decision, and side effects are executed by K4 (runs), K2 (artifacts), K3 (workflow), or K8 (egress). Therefore:
- An agent cannot write files bypassing the scope check;
- An agent cannot delete artifacts;
- An agent cannot send content directly to the network.

External coding agents launched under management run in containers, and the container boundary plays the same role.

---

## 13. Requirements Traceability Matrix

| Requirement area | Main architectural parts responsible |
|---|---|
| §3.4 Golden chain, FR-VER | §7.3, K6, simulation and verification backend plugins |
| §3.7 Approvals, snapshots, waivers; FR-PKG-04 | K7, K3 (invalidation propagation) |
| §3.8 Implementation library and integration views; FR-PHY-08 | Implementation library data plugins, K3 (stage entry), K6 |
| §3.9 Verdict layers; DP-8, DP-10 | K6, K4 (execution status), parser plugins |
| FR-EST | Estimator plugin, implementation library data plugins |
| FR-MIG | Migration assessment (flow pack + process capability dependencies declared by plugins), K3 |
| FR-SGN-02a/02b | Signoff check definitions, K6, K5 (timing exceptions are hard constraints) |
| FR-SGN-07, FR-RTL-02 | Storage mapping plugin (including inverse restoration), storage contents consistency check |
| §6 Agent system | §6, K5 |
| §7 Traceability and recoverability | K1, K2, K3, §11 |
| §8 Data security | K8 |
| §9 UI | §9.1, §9.4 |
| NFR-01/03 | §9.4, §4.4 (the kernel does no heavy computation) |
| NFR-02/04 | AP-5, §4.4, §11 |
| NFR-08 | §10 |
| NFR-13/14 | §10.1, §10.4 |
| FR-INT | §4.9, §7, §9.2, §9.3 |

---

## 14. Code Repository Organization (Proposed)

```
llmchip-harness/
  packages/
    kernel-api/        Schemas and generated types for the kernel API, plugin protocol, artifact types (no Cordis dependency)
    kernel/            harnessd: K1–K10
    client/            Kernel API client library (Node.js and browsers), used by the CLI, the UI and tests
    agent-runtime/     Agent runtime (in-kernel system plugin)
    llm-providers/     LLM provider plugins
    mcp-server/        MCP service plugin
    cli/               CLI client
    plugin-sdk-ts/     TypeScript plugin SDK
  apps/
    web/               Web front end (Chinese and English)
    desktop/           Tauri shell
  python/
    llmchip-sdk/       Python plugin SDK
  plugins/             Domain plugins: flow-default, model-loader, quant, estimator,
                       arch-taalas, rtl-gen, sim-verilator, sim-icarus, eda-yosys,
                       eda-openroad, eda-klayout, pdk-gf180mcu, silicon-test ……
  images/              Build definitions for tool containers and the WSL distro
  vendor/              Vendored Cordis and companion library sources (ADR-001 item 3, manifest in vendor/README.md)
  docs/
```

---

## 15. Architecture Decision Records (ADR)

| ID | Decision | Status |
|---|---|---|
| ADR-001 | Cordis is used only for assembly inside harnessd, and kernel interfaces do not depend on it; based on the deepseek-harness fork with the version locked (§5) | Accepted (M1a prototype validation passed, 2026-10-18) |
| ADR-002 | The desktop shell uses Tauri | Accepted (owner decision) |
| ADR-003 | The kernel and front end use TypeScript; compute-intensive plugins use Python; plugins communicate via a unified protocol and can be written in any language | Accepted (owner decision) |
| ADR-004 | The MVP kernel runs in WSL2, and the Windows desktop application connects to the local kernel; it can later be deployed to a private server | Accepted (owner decision) |
| ADR-005 | Use a dedicated WSL distro with built-in Podman as the container runtime; users do not need to install Docker | Proposed |
| ADR-006 | The event store uses SQLite (WAL, synchronous=FULL) through Node.js's built-in `node:sqlite`, with no native add-on; artifacts use a content-addressed store on the file system | Accepted (M1a) |
| ADR-007 | The out-of-process plugin protocol uses JSON-RPC 2.0, one message per line, over stdio | Accepted (M1b) |
| ADR-008 | Single egress gateway: LLM provider plugins do not hold network connections directly | Proposed |
| ADR-009 | Verdicts are made by the kernel's verdict engine; plugins only produce metrics | Accepted (M1b) |

The full record of each ADR (context, alternatives, rationale, consequences) will be placed under `docs/adr/` after development starts.

---

## 16. Open Questions and M1 Prototype Validation Items

| Question | Validation method |
|---|---|
| Whether the Cordis fork can meet the unload order and fault isolation requirements | Exit criteria of ADR-001 (§5) |
| Stability of rootless Podman in WSL2, and performance of run workspace mounts | Run a set of small Yosys/OpenROAD tasks on the reference machine |
| Behavior of the localhost connection between Tauri (Windows) and harnessd in WSL after hibernation, wake-up, and WSL restart | Automated test: repeatedly hibernate and wake, checking connection recovery |
| Write throughput of the event store under high-frequency logging | Load test: multiple runs outputting logs simultaneously, confirming that logs go to artifacts and events only record summaries |
| Impact of fingerprint scanning on egress latency | Build an index with the synthetic "fake PDK" and measure scan time for large requests |
| Whether the kernel's idle memory can meet §10.1 of the requirements document | Measure on the reference machine per the measurement scope of §10.1 |
