# llmchip-harness MVP Requirement Analysis

English | [中文](<Coding Harness MVP Requirement Analysis.zh.md>)

| Item | Content |
|---|---|
| Document version | v0.7 (draft) |
| Date | 2026-10-24 |
| Status | Under review, not yet baselined (v1.0 is the formal baseline) |
| Sibling document | [Microkernel Architecture](<Microkernel Architecture.md>): describes how the system is organized and extended |
| Chinese version | [Coding Harness MVP Requirement Analysis.zh.md](<Coding Harness MVP Requirement Analysis.zh.md>) |

This document answers **"what llmchip-harness must do"**. "How the system is organized and extended" is answered by the architecture document.

### Revision History

| Version | Changes |
|---|---|
| v0.1 | Initial draft |
| v0.2 | Revised per review comments: ① the golden chain distinguishes "effect approximation" from "bit-exact equivalence", introduces the numeric spec, and L0 precision follows the upstream reference implementation; ② simulation backends are declared by capability, timing signoff is based on STA, SDF simulation is downgraded to P1 and its validity is defined; ③ added process migration impact assessment; ④ estimation conclusions add "Conditionally feasible" and "Insufficient data", area density is bound to a specific implementation, added storage implementation calibration; ⑤ approvals are bound to the design snapshot and evidence manifest, internal risk acceptance is distinguished from manufacturing-rule waivers; ⑥ managed agents are distinguished from external clients; ⑦ added a data security chapter (label propagation, egress gateway, declassification rules, fingerprint scanning); ⑧ added a verification plan, distinguished passing tests from formal proof, fixed the decoding rule; ⑨ run results are layered separately from engineering verdicts; ⑩ added out-of-the-box and deployment form requirements |
| v0.3 | Revised rule linkage and acceptance per the second review round: ① check coverage completeness (DP-10); timing signoff must prove constraint completeness; timing exceptions are hard constraints; ② the implementation library adds "integration views and per-stage integration capability", a consistency check between storage contents and quantized weights, and blocks stage entry when views are missing; ③ added release rules for "Conditionally approved"; ④ the skeleton (M1) and real calibration (M1.5) are accepted separately; candidate implementations may end as "Unusable"; ⑤ minimum performance estimation is raised to P0; ⑥ migration acceptance is changed into two cases, with and without dependency; ⑦ cross-model handoff is split into two cases by data permission; ⑧ added reference environment and measurement conditions; ⑨ added document authority and conflict-handling rules (§0.3) |
| v0.4 | ① The storage contents check distinguishes "source consistency" from "content consistency": record the source weight hash, mapping rule version, and per-storage-block content hashes; restore according to the mapping rule before comparing; mapping rules must be reversible; ② requirement ID stability rules (§0.1); ③ minimum classification criteria for hard and soft constraints; lowering standards to pass checks is forbidden (§3.2); ④ NFR-01/03 uniformly reference §10.1; ⑤ distinguish "saving measured records" (P0) from "automatically updating the estimation model" (P1); ⑥ clarified the location of formal documents and the Chinese/English version rules (§0.3) |
| v0.5 | Records the owner's G0 decision (2026-10-18): GF180MCU calibration shows stories260K does not fit in 2 mm², so the reference model becomes a small Llama of about 80K parameters with 2-bit or ternary weights, trained by the workbench at a time the owner schedules; stories260K becomes the comparison model (§11.1) |
| v0.6 | Records the owner's decisions at the start of M4 (2026-10-18, §11.1): ① the only IO library of GF180MCU, `gf180mcu_fd_io`, has 350 µm tall pads, so a 1×2 mm die leaves a core of only about 0.21×1.21 mm, and synthesis shows the inference engine's logic alone does not fit; the prerun die is therefore **1.2×2 mm (2.4 mm²)**, still following the 2 mm Y rule; compressing to 2 mm² is a follow-up task once the Sephonchitech PDK shows the real IO size; ② until the reference model is trained, the design subject of S1–S8 is a **proxy** with the reference model's structure (Llama, ternary weights, fixed seed, untrained, about 5K parameters); its G1 accuracy result has no meaning and must be labelled so; the trained model reruns from S1; ③ the GF180 KLayout deck reports CUP.3 and DCF.1a on the PDK's own `gf180mcu_fd_io` cells (non-CUP bond pads and IO fillers); the owner approved excluding these two rules only inside those cells and their footprints in the signoff rules artifact; excluded markers stay listed in the DRC report and are counted apart, and any violation elsewhere still fails. This is a rule-deck configuration (a hard-constraint change), not a manufacturing waiver |
| v0.7 | Records the owner's decisions of 2026-10-24 (three rounds): ① the design subject is **Qwen2.5-0.5B-Instruct**; **no tapeout**: only the six steps before tapeout (specification → RTL → verification → synthesis → physical design → signoff), on all open-source PDKs (gf180mcuD, sky130A, ihp-sg13g2); the full model goes through specification, RTL and verification, and a Qwen slice aligned to the numeric spec's groups goes through synthesis, physical design and signoff (§11.1); ② all approval gates (G0–G4), design snapshots, approvals, conditional approval, release and export release are removed; each stage is judged by its own checks, and entering a stage needs only its admission checks (§3.7); ③ waivers are removed: a failure is recorded as it is, and after a fix the work and its tests run again; the rule exclusion of CUP.3/DCF.1a on GF180 IO cells stays, as a rule-deck configuration (§3.7.4); ④ the tapeout package (§5.10, including external signoff import FR-SGN-06) and post-silicon testing (§5.12) are deleted; ⑤ every step has two entries, "run the development" and "run all tests", plus project-level entries; the scheduler allocates CPU, memory and GPU; long runs resume from checkpoints (§4, FR-PRJ-07/08, FR-UI-14, FR-TRC-10, NFR-15); ⑥ DFT (scan chains and ATPG) becomes P0, with stuck-at fault coverage of at least 95% (FR-PHY-06); ⑦ model quality is judged by open task benchmarks, with allowed drops of L2 against L0 of 2/3/5 points; the daily quick set has 5 questions per subject or benchmark and the full set its own entry; token-level and per-layer comparisons locate the cause of wrong answers; L0 runs at the upstream native precision; SWE-bench is not used (FR-QNT-04/08/09); ⑧ new design principle DP-11: a signoff check also runs a second independent open-source tool where one exists, and has a negative control; ⑨ acceptance criteria changed accordingly, and the milestones are now R0–R8 (§11.4, §12) |

---

## 0. Document Conventions

### 0.1 Requirement IDs and Priorities

- Requirement IDs have the format `<Category>-<Domain>-<Sequence>`.
  - Category: `FR` denotes a functional requirement, `NFR` a non-functional requirement, `AC` an acceptance criterion.
  - Example: `FR-EST-03`.
- There are three priority levels:
  - **P0**: The MVP must have it; without it the MVP does not stand.
  - **P1**: The MVP should have it; it can be completed late in the MVP.
  - **P2**: Future plan. The MVP only needs to reserve room for it in interfaces and data structures, without implementing it.

**ID stability rules**:

- During the draft phase (before v1.0), IDs may be reorganized and renumbered.
- After the document is published with **v1.0 as the formal baseline**:
  - IDs are **never reused**;
  - When a requirement is deleted, its ID is kept and marked "Deprecated", stating the reason and the replacement;
  - When a requirement is split, new IDs are created for each resulting part; the original ID is marked "Deprecated" and points to the new IDs;
  - When the meaning of a requirement is changed, it is likewise handled as "deprecate the old ID, create a new ID"; if only the wording changes without changing the meaning, the original ID may be kept.
- When tasks, evidence, and incidents reference a requirement ID, they must also record the version or content hash of the requirements document they are based on.

### 0.2 Glossary

| Term | Description |
|---|---|
| Tapeout | Handing the final layout to the foundry to manufacture the chip |
| MPW (Multi-Project Wafer) | Designs from multiple customers are combined on the same wafer to share mask costs; suitable for prototype validation |
| PDK | Process Design Kit, including device models, design rules, DRC/LVS rule decks, standard cell libraries, etc. Commercial PDKs are usually protected by NDA |
| RTL | Register-transfer level hardware description, e.g. Verilog / SystemVerilog |
| Netlist | Post-synthesis description of the circuit connectivity composed of standard cells |
| GDS / GDSII | Layout file format; the final deliverable to the foundry |
| DRC / LVS | Design Rule Check / Layout Versus Schematic; mandatory signoff items before tapeout |
| STA | Static Timing Analysis; the primary means of timing signoff |
| SDF | Standard Delay Format; used to back-annotate timing information into gate-level simulation |
| PEX | Parasitic extraction |
| DFT | Design for Testability |
| Signoff | The final check before tapeout confirming that the design meets all rules and metrics |
| Mask ROM | Mask read-only memory; data is hard-wired into the layout at manufacturing time |
| Macro | A pre-designed hard block, such as ROM or SRAM |
| KV cache | The attention Keys/Values cached during LLM inference; requires read-write storage |
| Numeric Spec | A versioned artifact that specifies all numeric behavior of the chip; the prerequisite for "bit-exact equivalence"; see §3.4 |
| Golden Chain | The level-by-level chain of relationships from the original model to silicon; see §3.4 |
| Artifact | Any file or data produced at any stage in the workbench; immutable and carries lineage |
| Verdict | The conclusion a check definition gives on one run (pass / fail / indeterminate), bound to the hashes of the checked inputs; see §3.9 |
| Step entries | The two entries of each stage, "run the development" and "run all tests"; see §4 |
| Gate (removed) | In earlier versions, a point (G0–G4) that could only be passed with human approval. Removed by the owner on 2026-10-24; see §3.7 |
| Model IR | The workbench's internal unified intermediate representation of models |
| Process Profile | The set of parameters for a given process node and foundry; see §3.8 |
| Implementation Library | The set of parameters for each concrete implementation type under a given process (ROM macro, SRAM macro, standard-cell logic ROM, etc.); see §3.8 |
| Managed agent / External client | The two modes by which an agent connects; see §6.1 |
| Data label | Data classification labels such as Public / Internal / NDA; see §8 |
| MCP | Model Context Protocol; the protocol through which external agents invoke workbench capabilities |
| BCD | A process integrating Bipolar, CMOS, and DMOS devices; Sephonchitech's 180nm process is BCD (its process profile stays as data) |

### 0.3 Document Authority and Conflict Handling

Agents developing the workbench may read multiple documents. To prevent each from understanding the project from a different entry point, the following is specified:

1. **Drafts are not authoritative.** Files under the `drafts/` directory are only discussion drafts and take effect only after being merged into the formal location. The formal location is the two documents in the repository root: the requirements document `Coding Harness MVP Requirement Analysis.md` (describes "what to build") and the architecture document `Microkernel Architecture.md` (describes "how the system is organized and extended").
2. **Authority is divided by domain**, rather than by a simple global precedence order:

   | Domain | Authoritative document |
   |---|---|
   | What the product must do, priorities, acceptance criteria | Requirements document |
   | System structure, module boundaries, interfaces and protocols | Architecture document |
   | Development process, coding constraints, verification requirements, prohibitions | Root `AGENTS.md` |
   | Goals and constraints of an individual chip project | That project's `CHARTER.md` |

   `AGENTS.md` is the **single entry point** for all agents; it must link to the documents above and declare this rule.
3. **Decisions recorded by the owner (ADRs) take precedence over all of the documents above**, and the related documents should be updated accordingly.
4. **When a conflict is found, an agent must not choose one side on its own and continue working.** It must record the conflict as an open issue, escalate it to the owner, and pause work within the scope affected by the conflict.
5. A lower-level document must not redefine content in a higher-level domain. For example, the architecture document cannot modify acceptance criteria, and `CHARTER.md` cannot relax P0 rules in the requirements document.
6. **Chinese and English versions**: Important documents maintain both an English version (`<name>.md`) and a Chinese version (`<name>.zh.md`); **the Chinese version is authoritative**. The two versions must always be consistent: every change updates both versions in the same commit, with the same version number. When the two are found to be inconsistent, follow the Chinese version and escalate per item 4.

---

## 1. Product Vision and Positioning

### 1.1 Background

Public reports show that companies such as Taalas are exploring a new route: hard-wiring the weights of a specific large model **directly into the chip** (for example, stored as mask ROM or via metal-layer programming), and placing only dynamic state such as the KV cache in on-chip SRAM. This removes the inference bottleneck of "moving weights in from external memory", in exchange for order-of-magnitude gains in energy efficiency and speed. The cost is that the chip is tightly bound to the model, so the ability to "go from a model to a chip quickly and cheaply" is critical.

The essence of this route is: **the model is the source code, and the chip is the compiled artifact**. Completing this "compilation" chain spans several domains: AI numerics (quantization, accuracy evaluation), hardware architecture, digital front end (RTL, verification), digital back end (synthesis, place and route, signoff), and tapeout delivery. Today these domains are scattered across different teams, different tools, and different file conventions.

### 1.2 Product Positioning

**llmchip-harness is an integrated R&D workbench sitting between "open-source large models" and "the foundry".** All R&D work before tapeout is done on this workbench.

- **Input**: The weights of any open-source large model, plus the target process, foundry, and budget.
- **Output**:
  - A signed-off design: the specification, RTL and verification conclusions of the full model, and the layout and signoff reports of a slice on open-source processes (the owner decided on 2026-10-24 not to tape out);
  - A complete, traceable **R&D trajectory**;
  - The verdicts of every step's checks, with the history of failures and reruns fully kept.
- **Executors**: R&D work is **done entirely by AI agents**. Humans set goals and hard constraints and handle incidents; each stage is judged by its own checks, and there are no approval gates.

### 1.3 Core Value

1. **End to end**: From weights to GDS and signoff, everything is done inside one workbench, with no manual moving of files between tools.
2. **Clear goals, no drift**: What the chip "should do" is strictly defined by the model and the numeric spec; every level has a well-defined comparison relationship and verdict basis; see §3.4.
3. **Traceable and recoverable**: Any artifact can be traced to all its inputs, tool versions, and decision makers; any verdict is bound to the hashes of the inputs it checked; any task can be resumed after interruption.
4. **Agent-driven, human-supervised**: Agents can be switched freely (DeepSeek, Claude, GPT, local models, etc.), but their behavior is constrained by rules and fully recorded, and incidents are exposed immediately.
5. **Open integration**: Model formats, quantization methods, chip architectures, EDA tools, PDKs, and LLM providers are all integrated as plugins.
6. **Scalable**: The data model and estimation capabilities cover models from hundreds of thousands to trillions of parameters. The MVP only implements small models on a single chip.
7. **Out of the box**: Users do not need to install or configure any dependencies themselves.

### 1.4 Design Principles

| ID | Principle | Meaning |
|---|---|---|
| DP-1 | Function is defined by spec | Model effect is defined by the original model; the chip's exact behavior is defined by the numeric spec. Any deviation must be quantifiable and explainable |
| DP-2 | Everything is an artifact; artifacts are immutable | All intermediate results are read-only artifacts with content hashes. "Modifying" means producing a new version |
| DP-3 | Change the generator, not the generated | RTL, layout, etc. are produced by generators or tools. Agents modify generators, configurations, and constraints, not hand-edit output files |
| DP-4 | State lives in the workbench, not the agent | Agents can be replaced at any time; project state, decisions, and constraints are all stored in the workbench |
| DP-5 | Plugin-based | Every capability other than the kernel is a plugin and can be replaced, added, or disabled |
| DP-6 | Sensitive data never leaves the machine | NDA materials and their derived data are never sent to cloud LLMs |
| DP-7 | Lightweight, stable, recoverable | No call should crash or hang the application. All tasks are resumable |
| DP-8 | Running is not passing | A tool running successfully only means the tool finished. Whether requirements are met is determined by an independent verdict |
| DP-9 | Evidence is bound to its object | Every verdict is bound to the hashes of the inputs it checked. When the object changes, the old verdict does not judge the new object, and the check must run again |
| DP-10 | Not checked is not the same as no problem | Every class of check must prove its own coverage is complete (e.g. constraints complete, rule switches complete, top cell correct). When coverage completeness cannot be proven, the conclusion is "Indeterminate", not "no violations" |
| DP-11 | Signoff needs a second source and a negative control | Where the open-source ecosystem has a second independent tool for a signoff check, both run, and disagreement is "indeterminate"; every signoff check has a negative control: a known injected defect that the check must report. "Nothing was checked" is never "pass" |

---

## 2. Users and Roles

### 2.1 Human Roles

| Role | Responsibilities |
|---|---|
| Project owner (Owner) | Initiates projects, sets goals and budget, approves changes to hard constraints, handles escalated incidents, sets data egress policy |
| Expert reviewer (Reviewer, optional) | Gives professional opinions on specific stages (e.g. timing, layout) |
| Platform administrator (Admin) | Installs and maintains the workbench; configures plugins, toolchains, LLM providers, and keys |

In the MVP, these three roles can be held by the same person (single-user local deployment).

### 2.2 Agent Roles

| Role | Responsibilities |
|---|---|
| Orchestrator | Reads the project charter, decomposes tasks, dispatches them to stage agents, drives the flow, and assembles the check results of each stage |
| Stage Agent | Responsible for the concrete work of a stage, e.g. quantization, architecture, RTL generator development, physical implementation scripts |
| Verifier | Writes and runs tests; maintains the golden chain checks |
| Reviewer Agent | Reviews the executor's changes against the project charter and constraints. **An executor cannot review its own output** |

The rules are:

- The same role can be configured with different LLMs.
- The reviewer is advised to use a different model from the executor, to reduce shared blind spots.
- Agents cannot change hard constraints (they can only propose changes), and cannot turn a failed check into a pass (there are no waivers).

### 2.3 External Agents

External agents (e.g. the user's own Claude Code or Codex) can connect in two ways; see §6.1 for details:

- **Launched under workbench management**: runs as a managed agent, subject to the same constraints as internal agents.
- **As an external client, connected via MCP/API**: the workbench can only control operations initiated through its own API.

### 2.4 Typical User Stories

1. **Quick estimate**: As the owner, I enter "Qwen 0.5B, 4bit, 180nm, budget 10,000 yuan" and within seconds get the conclusion: "Single chip infeasible; weight storage alone requires about 1000mm²". It also gives the range of parameter counts that fit within 2mm², along with optional adjustment suggestions.
2. **One-click project creation**: I create a project from a sample template, confirm the charter, and click "Start". Agents begin working, and I can see what they are doing in the UI at any time.
3. **Checks**: When the accuracy check finishes, I receive a notification. The page shows the scores of the bit-exact model and the original model on the same benchmark questions, the cause located for each wrongly answered question, and risk notes. When the check fails, I see why; the agent changes the numeric scheme, then the work and its tests run again.
4. **Incident handling**: Simulation finds that the layer-3 attention output does not match the bit-exact model. The incident center automatically locates the first divergent operator and cycle. After the agent fails twice to fix it, it escalates to me, and I can see the full context.
5. **Switching agents**: I switch the executor from DeepSeek to Claude. The new agent reads the "handoff pack" and continues working within minutes, without me having to explain again.
6. **Trying the chip**: In the Chip Playground I enter "Once upon a time" and simultaneously see the output of the original model, the bit-exact model, and RTL simulation, confirming that they satisfy their respective relationship requirements.
7. **Switching process**: After getting the foundry PDK, I import it. The workbench gives a migration impact assessment, e.g. "the target process has no ROM macro; restart from S3", and records the stage to restart from; the work and its tests run again from that stage.
8. **Signoff summary**: After the slice's physical design on the three open-source PDKs, I read the signoff summary: which checks cover each class of failure cause, the conclusions of the main tool and the second source, and whether the negative controls worked.

---

## 3. Core Concepts

### 3.1 Project

A project corresponds to **the complete R&D process of one chip**, specifically including:

- One target model;
- One process profile and its implementation library;
- One architecture scheme;
- One numeric spec;
- One project charter;
- All artifacts, events, and verdicts.

A Workspace can contain multiple projects.

### 3.2 Project Charter (CHARTER.md)

The charter is the **project-level AGENTS.md** and the first entry point for any agent entering the project. It contains:

- Project goals and success criteria (e.g. "silicon output matches the bit-exact model token by token");
- Target specs (model, accuracy budget, area, frequency, power, interfaces, budget);
- Constraint list: hard constraints require human approval to change; soft constraints can be proposed for change by agents;
- Each stage's required checks and their current verdicts;
- Key decision records, i.e. the ADR (Architecture Decision Record) index;
- Known issues and prohibitions.

The charter is generated automatically by the workbench from project state; agents can propose changes, and humans approve changes to hard constraints.

**Minimum classification criteria for hard and soft constraints**:

| Category | Criterion | Examples | Who can approve changes |
|---|---|---|---|
| Hard constraint | **Standards that affect acceptance results, and changes to those standards themselves** | Accuracy budget; evaluation dataset and prompt set; numeric spec and observation points; coverage thresholds; verification plan items; target specs (area, frequency, interfaces); timing constraints and timing exceptions; process corners; rule deck versions and rule switches; signoff checklist and signoff rule decks (including rule exclusions); data classification and declassification rules | Humans only |
| Soft constraint | Implementation methods and working habits that do not change any check standard | Choice of implementation method; debugging order; tool parameter suggestions that do not affect check standards (e.g. initial placement density); coding style hints | Reviewer agent |

Rules:

- **When the classification cannot be determined, treat it as a hard constraint.**
- The same tool parameter may fall into different categories depending on its use. For example, STA derating factors and DRC rule switches change check standards and are hard constraints; initial placement density only affects the implementation process and is a soft constraint.
- **Agents must not lower check standards in order to make checks pass.** If the standard of a check changes, and as a result its outcome changes from "fail" to "pass" while the design itself has not changed, the workbench automatically records an incident and hands it to a human for review.

### 3.3 Stages, Tasks, Runs

| Concept | Description |
|---|---|
| Stage | A step in the flow, S0–S7; see §4 |
| Task | A unit of work within a stage, e.g. "implement the RMSNorm module generator" |
| Run | One concrete execution of a tool or agent, with inputs, outputs, logs, duration, and resource consumption |

### 3.4 Golden Chain and Numeric Spec

#### 3.4.1 Two Kinds of Relationship

There are only two kinds of relationship between adjacent levels on the chain, and they **must not be mixed**:

| Relationship | Symbol | Meaning | Verdict method |
|---|---|---|---|
| Effect approximation | ≈ | The two execute **different** numeric rules; only the model effect is required to be within the error budget | Statistical evaluation on a specified dataset and prompt set, e.g. perplexity difference, token agreement rate, sample text comparison. The conclusion holds only for that evaluation set and budget |
| Bit-exact equivalence | ≡ | The two execute the **same version** of the numeric spec and must be bit-for-bit identical at the specified observation points | Bit-by-bit comparison at the specified observation points; any single differing bit is a failure |

**Bit-by-bit comparison has a definite meaning only when both sides execute the same version of the numeric spec.** Therefore, every ≡ relationship must state the numeric spec version it is based on.

#### 3.4.2 Numeric Spec

The numeric spec is a versioned artifact that specifies all numeric behavior of the chip. Its contents include at least:

- **Data formats**: the integer or fixed-point bit width and signedness of each class of tensor (weights, activations, accumulators, KV cache, intermediate results);
- **Scaling**: the granularity of scale factors (per-tensor / per-channel / group-wise), their representation, and where they are applied;
- **Rounding**: the rounding mode, including the tie-breaking rule for "exactly half";
- **Saturation and overflow**: truncation positions, and whether overflow saturates or wraps around;
- **Accumulation**: accumulator bit width and accumulation order. If the accumulator width is specified wide enough to guarantee no overflow, the result may be declared order-independent, but a proof must be attached;
- **Nonlinear functions**: approximation methods for Softmax/exp, SiLU, the reciprocal square root in RMSNorm, RoPE sin/cos, etc., including lookup table contents, polynomial coefficients, piecewise intervals, and input clamping ranges;
- **Decoding rule**: the default is greedy argmax, with ties broken by the smallest token id. If random sampling is supported, the pseudo-random number algorithm, seed, and numeric implementation must be specified;
- **Tokenization and context**: tokenizer version, special tokens, maximum context length, and the behavior when the limit is exceeded;
- **Observation points**: which intermediate quantities (e.g. per-layer output, per-step logits, final token) participate in comparison and at what granularity.

#### 3.4.3 Chain Levels

| Level | Name | Definition | Relationship to the previous level | Evidence type |
|---|---|---|---|---|
| L0 | Upstream reference | The upstream project's native reference implementation and native precision. Precision is recorded as an attribute, not fixed to a particular format. For example, L0 of stories260K is the FP32 inference of llama2.c `run.c`, and L0 of Qwen2.5 is the bf16 inference of transformers | Source of the effect definition | — |
| L1 | Quantization simulation (diagnostic level, optional) | Simulates only the quantization of weights or activations in a floating-point framework, for quickly exploring quantization schemes | ≈ L0 | Statistical evaluation |
| L2 | Bit-exact reference model | The reference implementation of the numeric spec, i.e. **the chip's executable functional spec** | ≈ L0 (**the object of the model quality verdict**: open task benchmarks; see FR-QNT-04) | Statistical evaluation |
| L3 | RTL functional simulation | The behavior of the RTL in a simulator | ≡ L2 (per the observation points of the numeric spec) | Test |
| L4 | Gate-level netlist function | Zero-delay functional behavior of the post-synthesis netlist | ≡ L3 | Formal proof, or zero-delay gate-level simulation test |
| L5 | Post-layout timing | Timing correctness after layout is complete | Timing meets constraints | STA with parasitics is the signoff basis; SDF timing simulation is supplementary evidence (P1) |
| L6 | FPGA prototype (optional, P2) | RTL running on an FPGA | ≡ L2 | Test |
| L7 | Silicon (deleted) | The owner decided on 2026-10-24 not to tape out; this version has no L7 | — | — |

A few rules:

- **The model quality verdict is based on L2, not L1.** L2 includes all errors introduced by fixed-point nonlinearities, rounding, saturation, etc.; L1 is only for exploration.
- The numeric spec is finalized in S2. If a later stage needs to change numeric behavior (e.g. architecture design finds that the accumulator needs to be narrower), a new version of the numeric spec must be produced, and the accuracy check runs again in S2. Verdicts and ≡ evidence on the old version do not judge the new one and must run again.
- **Evidence types must be clearly distinguished**: statistical evaluation, tests, formal proofs, and static analysis are evidence of different strengths. Reports and the UI must not describe "tests passed" as "equivalence proven".
- The status of every link (not checked / pass / fail / indeterminate / invalidated) is **visible at a glance** in the UI.
- After any upstream artifact changes, downstream links automatically become **invalidated**.
- When a link fails, the workbench must be able to locate the **first divergence point** (which level, which operator, which tensor element, which clock cycle).

### 3.5 Artifacts and Lineage

- Every artifact has: a content hash, type, data label (see §8), the run that produced it, the list of input artifacts, the tool and its version, the creator (which agent or human), and a time.
- Artifacts are **immutable**. Modifying an artifact produces a new version; the old version is retained.
- All artifacts form a **lineage graph**. Starting from the GDS, one can trace all the way back to the hash of the weight file.

### 3.6 Trajectory

Trajectory = event timeline + artifact lineage graph + verdict records + records of human decisions (such as hard constraint changes). It answers two questions: "How was this chip built step by step?" and "Who made a given decision, when, based on what evidence, and for which version of the design?"

### 3.7 Checks and Human Decisions (Gates Removed)

On 2026-10-24 the owner removed the approval gates (formerly G0–G4), design snapshots, approvals, conditional approval, release and waivers. This section states the rules that hold after the removal.

#### 3.7.1 Each Stage Is Judged by Its Own Checks

- The flow pack declares the checks each stage requires (§3.9). A verdict is bound to the input hashes of the run it judges (DP-9); when the object changes, the old verdict no longer applies.
- **Entering a stage** needs only the stage's admission checks to pass (FR-PHY-08); no human approval is needed.
- A failed check does not lock the flow: later stages can still run, but the UI keeps showing the upstream checks that did not pass and the downstream conclusions they affect.
- A stage is "done" when every check it requires passes.

#### 3.7.2 Handling Failures

- **There are no waivers.** A failure is a failure: it is recorded as it is and opens an incident (§3.10).
- There is only one way to handle it: find the cause, fix the generator, template, configuration or constraint, then run the work and its tests again. Every earlier failed verdict stays in the event log and can be traced.
- Lowering a check's standard so that a failure becomes a pass is handled per §3.2: it is a hard constraint change, needs human approval, and is recorded as an incident.

#### 3.7.3 Decisions That Still Need a Human

| Decision | Basis |
|---|---|
| Changes to hard constraints (including check standards, signoff rule decks, timing exceptions) | §3.2, FR-SGN-02b |
| Dangerous operations | §6.6 |
| Process migrations that restart from S3 or an earlier stage | FR-MIG-04 |

#### 3.7.4 A Rule-Deck Configuration Is Not a Waiver

The signoff rule deck is itself a hard constraint. The GF180 rule exclusion the owner approved on 2026-10-18 and confirmed on 2026-10-24 (CUP.3 and DCF.1a inside the PDK's own `gf180mcu_fd_io` cells and their footprints) is a rule-deck configuration: the excluded markers are still fully listed in the DRC report and counted apart, and any violation elsewhere fails as usual. It is not a waiver of a design defect.

### 3.8 Process Profile and Implementation Library

A **Process Profile** describes a given process node and foundry, mainly including:

- Devices and voltage scheme (core voltage, IO voltage), metal layers;
- Available standard cell libraries and IO libraries, and their voltages and process corners;
- Foundry rules: unit price, minimum area, rounding rules, size constraints (e.g. the Y dimension of Sephonchitech (十方尺) 180nm BCD is 2/3/5mm), sample quantity;
- Associated PDK plugin, signoff checklist, and signoff rule deck;
- Associated implementation library entries, and each entry's **availability status**: Available / Pending confirmation / Unavailable.

An **Implementation Library** describes each **concrete** storage or logic implementation under that process. Area density is **bound to the implementation, not just to the process node**. Example entries:

- "GF180MCU · OpenRAM ROM generator (experimental) · version X"
- "GF180MCU · gf180mcu_fd_ip_sram 512×8 macro"
- "GF180MCU · standard-cell logic ROM (cell library Y · synthesis script version Z)"
- "Sephonchitech 180nm BCD · foundry ROM compiler (pending confirmation)"

Each entry includes: unit area (per bit or per gate), peripheral circuit overhead, capacity range and shape limits, timing parameters, interface form, and **data source and confidence**:

| Confidence | Source |
|---|---|
| Empirical value | Public information or empirical estimate |
| Datasheet value | Read from the PDK or IP datasheet |
| Calibrated value | Obtained by this workbench through actual synthesis and place and route on that PDK (see FR-EST-13) |
| Measured value | Obtained from the measured records of this workbench's physical design and signoff (FR-PHY-04) |

**A calibrated area value does not mean "can be integrated into the chip and signed off".** Each implementation library entry must also register its **integration views and per-stage integration capability**:

| Stage of use | Required views (examples) |
|---|---|
| S4/S5 RTL functional simulation | Behavioral simulation model with storage contents (Verilog) |
| S6 synthesis and STA | Timing model (Liberty), with process corners consistent with project requirements |
| S6 place and route | Physical abstract (LEF: pins, obstructions) |
| S7 DRC | Layout (GDS), plus evidence that the macro itself has no DRC violations |
| S7 LVS | LVS netlist (CDL/SPICE) |

For each view, record: whether it is complete, its version, the run that generated it, and evidence. Rules:

- **Same version, same contents.** All views of the same macro must come from the same version of the generator or IP and correspond to **the same storage contents**. For a ROM that stores weights, these contents are the quantized weight artifact and must be bound by hash.
- **Missing views block stage entry.** Before entering a stage, the workbench automatically checks the views required by that stage. If views are missing or versions are inconsistent, entry into the stage is prohibited, with an explanation of what is missing.
- For example: if a ROM generator only outputs GDS, SPICE, and logs, it has at most partial integration capability for S7. Until the simulation model, Liberty, and LEF are supplied, it cannot be used for S4–S6.

When a standard-cell logic ROM is used, the integration views come from the standard cell library itself; consistency of storage contents is guaranteed level by level by RTL simulation (L3), gate-level checks (L4), and LVS.

### 3.9 Verdict Layers

The result of each run is recorded in four layers; **success at a lower layer does not mean the upper layer passes**:

| Layer | Content | Values |
|---|---|---|
| ① Execution status | Whether the tool or agent ran to completion normally | Not started / Running / Exited normally / Non-zero exit / Timed out / Crashed / Cancelled |
| ② Parse status | Whether the output report was fully parsed; the parser version is recorded | Complete / Incomplete / Failed |
| ③ Check verdict | The conclusion of each check item against constraints | Pass / Fail / Indeterminate ("Waived" in earlier records is kept only as history) |
| ④ Stage conclusion | Aggregated from the verdicts of the checks the stage requires | Incomplete / Passed / Needs attention (a failure or an indeterminate verdict exists) |

Rules:

- A zero exit code only determines layer ①.
- When parsing is incomplete or fails, all related checks are "Indeterminate" and **cannot count as passing**.
- A check verdict of "Pass" requires all four conditions to hold simultaneously: evidence is complete, evidence is bound to the hashes of the checked inputs, the tool used actually has the capability required by the check (see FR-VER-01), and the check conditions match the constraint requirements.
- **Coverage completeness (DP-10)**: each class of check defines its own "coverage completeness conditions", which are checked together with the verdict. If they are not met, the conclusion is "Indeterminate". Examples:

  | Check | Coverage completeness conditions (examples) |
  |---|---|
  | STA | All clocks are defined; all input and output ports are constrained; there are no unconstrained timing endpoints; all timing exceptions are in the registered list; all process corners specified by the charter are covered; both setup and hold are checked |
  | DRC | The specified version of the rule deck is used; rule switches match the process profile requirements; the check targets the correct top cell and the complete layout |
  | LVS | The top cells of layout and netlist correspond correctly; the netlist compared is the one currently bound to the project |
  | Simulation | All items in the verification plan are executed; coverage reaches the thresholds |

### 3.10 Incidents

An incident is an anomaly that needs to be seen, including:

- Tool crashes or timeouts;
- Verification failures, checks that are "Indeterminate";
- Constraint violations;
- Budget (time, tokens, cost) overruns;
- Agents spinning in place, i.e. the same class of failure recurring;
- Permission denials, out-of-scope writes;
- Data egress interceptions;
- After an upstream artifact changes, the old verdicts no longer apply to the new artifact.

Each incident is linked to the event, run, and artifacts that triggered it, and the handling process and conclusion are recorded.

---

## 4. End-to-End Flow

```
S0 Feasibility & cost estimation ─▶ S1 Model ingestion ─▶ S2 Numeric co-design ─▶ S3 Architecture design & exploration ─▶
S4 Hardware generation ─▶ S5 Functional verification ─▶ S6 Physical implementation (synthesis, place and route) ─▶ S7 Signoff
(No tapeout: owner decision 2026-10-24)
```

The flow **allows rollback**. For example, when S6 finds the area is over budget, the flow can return to S3 to adjust the architecture, or to S2 to reduce bit widths; when switching the target process, the migration impact assessment (§5.11) decides which stage to restart from. Rollback produces new artifact versions; the old trajectory is fully retained, and the old verdicts no longer apply to the new versions, so the related checks must run again.

| Stage | Goal | Main inputs | Main artifacts | Chain level | Engineering step |
|---|---|---|---|---|---|
| S0 Feasibility & cost estimation | Determine "whether this model can be done in this process within this budget" | Model metadata, process profile, implementation library, budget | Feasibility report, cost estimate, initial charter | — | SPEC |
| S1 Model ingestion | Convert weights in any format into a unified representation | Weight files, tokenizer | Model IR, structure report, L0 reference outputs | L0 | SPEC |
| S2 Numeric co-design | Determine all numeric behavior of the chip within the accuracy budget | Model IR, accuracy budget | Numeric spec, L2 bit-exact model, accuracy report (optional L1) | L1, L2 | SPEC |
| S3 Architecture design & exploration | Under the numeric spec, determine how the chip computes and stores | Numeric spec, L2, process profile, implementation library, architecture plugin | Chip specification, storage implementation selection, PPA estimates | — | SPEC |
| S4 Hardware generation | Generate RTL and weight storage data | Specification, numeric spec, quantized weights | RTL, storage data, lint report | L3 | RTL |
| S5 Functional verification | Demonstrate per the verification plan that the RTL is bit-exact with L2 | RTL, L2, verification plan | Test reports, coverage, L3 ≡ L2 evidence | L3 | VER |
| S6 Physical implementation | Get from RTL to layout | RTL, PDK, constraints | Netlist, place-and-route results, timing/power/area reports | L4 | SYN, PD |
| S7 Signoff | Confirm the layout meets all rules | Layout, netlist, PDK rules | DRC/LVS/antenna/density reports, STA reports, gate-level simulation results | L4, L5 | SGN |
| S8 Tapeout package (deleted) | The owner decided on 2026-10-24 not to tape out | — | — | — | — |
| S9 Post-silicon testing (deleted) | Same as above | — | — | — | — |

**Six engineering steps and two entries** (owner decision 2026-10-24; implemented in R1–R7 of §11.4):

| Engineering step | Stages | Contents |
|---|---|---|
| SPEC Specification | S0–S3 | Feasibility, model ingestion and L0, numeric spec and L2, benchmark evaluation, chip specification and transaction-level model, slice definition, DFT specification |
| RTL | S4 | Template RTL and the generated top level, ROM contents |
| VER Verification | S5 | L3 ≡ L2, formal verification, coverage, mutation testing |
| SYN Synthesis | First half of S6 (a step of its own) | Netlist, scan chains and test vectors, formal equivalence |
| PD Physical design | Second half of S6 | Floorplan, power grid, placement, clock tree, routing, IR drop |
| SGN Signoff | S7 | DRC, LVS, STA, gate-level simulation, ROM readback, GDS XOR and more, each with a second source and a negative control (DP-11) |

Every step has two entries: **run the development** (runs the step's generators and tools as the pipeline the flow pack declares) and **run all tests** (runs every check the step requires and gives a summary). At project level there are also "develop all six steps" and "test all six steps". See FR-PRJ-07, FR-PRJ-08, FR-UI-14.

---

## 5. Functional Requirements

### 5.1 Projects and Workspaces (PRJ)

| ID | Priority | Requirement |
|---|---|---|
| FR-PRJ-01 | P0 | Create a project: select the model, process profile, and architecture plugin; can be created from a sample template |
| FR-PRJ-02 | P0 | Automatically generate and maintain the project charter (CHARTER.md), and display the charter change history |
| FR-PRJ-03 | P0 | Project overview: stage progress, golden chain status, the verdict status of each stage's required checks (pass / fail / indeterminate / not run), budget consumption, unhandled incidents |
| FR-PRJ-04 | P1 | Design branches: explore multiple schemes in parallel within the same project (e.g. 4bit vs. ternary), compare them, and select one |
| FR-PRJ-05 | P1 | Project export and import: full archive including artifacts, events, and charter, restorable on another machine. Archives follow the data label rules of §8 |
| FR-PRJ-06 | P0 | Built-in sample project (see §11.1) that users can run with one click |
| FR-PRJ-07 | P0 | **Step pipelines**: the flow pack declares two pipelines for each stage (engineering step), "development" and "required tests"; the kernel executes them as persistent pipeline runs: nodes run in dependency order (independent ones in parallel), all state is recorded in the event log, after a workbench restart the pipeline continues where it stopped, and it can be cancelled or resumed from a failed node. The required tests reuse an existing successful run on the same inputs for each check, otherwise start a new run, and end with a summary of pass, fail, indeterminate and not run, with reasons |
| FR-PRJ-08 | P0 | **Project-level entries**: run the development of all six steps in order, or run the required tests of all six steps |

### 5.2 S0 Feasibility, Area, and Cost Estimator (EST)

The workbench must help users determine "whether it is suitable for tapeout" and "roughly how much it costs".

| ID | Priority | Requirement |
|---|---|---|
| FR-EST-01 | P0 | **Forward estimation**: input the model (parameter count and structure, or directly specify a model file / model ID), weight bit width, activation bit width, context length, KV cache location (on-chip / off-chip), architecture template, process profile, and the implementation library entry selected for each class of storage; output the results of FR-EST-02 |
| FR-EST-02 | P0 | **Complete area model**. The estimate must itemize: weight storage (per the selected implementation), KV cache, activation and temporary buffers, compute units, control and interfaces, storage peripheral circuits, routing utilization factor, power grid, IO ring, seal ring, and the die size after satisfying the foundry size rules. Each item states its source and confidence. **Items with missing data cannot be treated as zero** and must be reflected in the conclusion |
| FR-EST-03 | P0 | **Cost estimation**: compute the tapeout cost per the foundry pricing rules (unit price, minimum area, rounding rules), and list cost items not covered by the estimate (packaging, test board, etc.) |
| FR-EST-04 | P0 | **Reverse estimation**: given an area or budget and a selected implementation, compute the range of parameter counts that fit (listed separately for different bit widths) |
| FR-EST-05 | P0 | **Feasibility conclusion** has four kinds: **Feasible**; **Conditionally feasible** (the preconditions must be listed, e.g. "the foundry provides a ROM macro with density no worse than X"); **Infeasible**; **Insufficient data** (the missing parameters must be listed, along with how to obtain them, e.g. "ask the foundry" or "run calibration"). Actionable adjustment directions are also given, e.g. reduce bit width, shorten context, move KV off-chip, use a smaller model, increase area, use a more advanced process |
| FR-EST-06 | P1 | **Multi-chip estimation**: when the model exceeds the single-chip limit (including the reticle size limit), estimate the number of chips needed and the partitioning (by layer or by tensor), as well as inter-chip bandwidth requirements. Covers up to trillion-parameter scale |
| FR-EST-07 | P0 | **Minimum performance estimation**: for a given context length, target frequency, and compute parallelism, estimate compute cycles per token, latency per token, and **off-chip bandwidth requirements** (e.g. when the KV cache is placed in off-chip SPI SRAM, compare the per-token read/write volume against the interface bandwidth limit). Also indicate whether the performance bottleneck is compute or the off-chip interface. Results serve as evidence for the S3 checks |
| FR-EST-08 | P1 | **Sweep comparison and fine-grained estimation**: parameter sweeps over model × bit width × process × implementation × parallelism, compared in tables and charts; includes fine-grained power estimation |
| FR-EST-09 | P0 | Results are given as ranges (lower bound / typical / upper bound), and the calculation process is shown to ensure explainability |
| FR-EST-10 | P1 | **Automatic calibration**: based on measured records saved in S6 and S7 (FR-PHY-04), automatically update the estimation parameters in the implementation library to improve the accuracy of subsequent estimates. Parameter versions before and after the update are both retained |
| FR-EST-11 | P0 | The estimator can be **used standalone, independent of a project** (quick estimate), and an estimation result can be turned into a new project with one click |
| FR-EST-12 | P0 | Estimation reports can be exported (Markdown/PDF) and used as evidence for the S0 check |
| FR-EST-13 | P0 | **Storage implementation calibration**: on a specified PDK, use the real tool flow to synthesize and place-and-route storage blocks of different sizes (e.g. standard-cell logic ROM, ROM generator, tiled SRAM macros), obtain "calibrated values" and write them into the implementation library, and also check that implementation's integration views (§3.8). Calibration tasks can be run with one click |
| FR-EST-14 | P0 | **Calibration conclusion**: the calibration of each candidate implementation ends with one of two conclusions: **Usable** (with calibrated values and integration view check results), or **Unusable** (with failure evidence, e.g. generation failure, missing views, DRC failure). "Unusable" is a legitimate conclusion and does not leave the calibration task unable to ever complete |
| FR-EST-15 | P0 | **Storage prerequisite**: at least one "Usable" storage implementation must meet the project's needs (capacity, area, complete integration views); otherwise the estimate concludes "Infeasible" or "Insufficient data" and lists the reasons |

### 5.3 S1 Model Ingestion (MDL)

| ID | Priority | Requirement |
|---|---|---|
| FR-MDL-01 | P0 | Support loading safetensors, PyTorch checkpoints, and the llama2.c format. GGUF is P1 |
| FR-MDL-02 | P1 | Download models from Hugging Face; support offline mirrors or local directories |
| FR-MDL-03 | P0 | Recognize the model structure and convert it to Model IR. The MVP supports the Llama family (RMSNorm, RoPE, GQA, SwiGLU) and the Qwen2 structure the reference project uses (QKV biases, tied embeddings). Other structures (MoE, etc.) are extended via plugins |
| FR-MDL-04 | P0 | Process the tokenizer together with the model, ensuring tokenization is consistent between the chip side and the reference side |
| FR-MDL-05 | P0 | Generate a structure report: number of layers, dimensions, types and counts of each operator, parameter distribution, compute per token |
| FR-MDL-06 | P0 | Run L0 reference inference using the upstream native implementation and native precision, and record the precision format (bf16 for Qwen2.5). Produce a reference output set (fixed prompt set and fixed decoding rule) |
| FR-MDL-07 | P0 | Record model license information, and flag compliance requirements in open-source releases |

### 5.4 S2 Numeric Co-design (QNT)

| ID | Priority | Requirement |
|---|---|---|
| FR-QNT-01 | P0 | Quantization methods are plugin-based. The MVP has built-in post-training quantization (RTN, per-channel / group-wise), with bit widths 8 / 4 / 2 / ternary |
| FR-QNT-02 | P0 | Produce the **numeric spec**, fully covering every item of §3.4.2. The numeric spec is machine-readable and carries a version number |
| FR-QNT-03 | P0 | Produce the **L2 bit-exact reference model**: strictly executes the numeric spec and can export intermediate results at all observation points for bit-by-bit comparison downstream |
| FR-QNT-04 | P0 | **Model quality evaluation**: judged by open task benchmarks: L0 and L2 answer the same questions with the numeric spec's decoding rule (greedy), and each answer is scored. Benchmarks: HumanEval+, MBPP+ (scored by unit tests, with the code run in a container without network), GSM8K, MMLU, CMMLU, C-Eval (validation set), IFEval. For each benchmark, compute the score difference L2 − L0 on the same questions and its paired bootstrap 95% confidence interval; allowed drop Δ: 2 points for MMLU, CMMLU, C-Eval, 3 for GSM8K, IFEval, 5 for HumanEval+, MBPP+. A point drop above Δ is "fail"; a point drop within Δ whose interval's lower bound is below −Δ (too few questions to conclude) is "indeterminate"; otherwise "pass". L0's scores are recorded as the baseline and compared with the model's officially published values; a large gap means the evaluation setup must be fixed first. Perplexity and token agreement are diagnostics only. SWE-bench is not used: measured, its prompts (with the files to edit) have a median of 11,962 Qwen tokens, and only 2% fit in 2,048, beyond the chip's context. L1 (optional) is used for fast exploration before finalization |
| FR-QNT-05 | P2 | **Small model training**: train a small Llama model of specified size from scratch with the llama2.c training flow. After the owner made Qwen2.5-0.5B-Instruct the reference model on 2026-10-24, the MVP no longer needs it |
| FR-QNT-06 | P1 | Quantization-aware training, distillation |
| FR-QNT-07 | P0 | The numeric spec, L2, and the model quality evaluation report serve as evidence for the S2 checks. Once the numeric spec is modified, the verdicts and ≡ evidence that depend on it do not judge the new spec and must run again |
| FR-QNT-08 | P0 | **Quick set and full set**: the daily quick set draws questions from each benchmark with a fixed random seed: 5 per subject for multiple-choice benchmarks, 5 per generation benchmark; every step's "run all tests" uses it, its verdicts are recorded as quick-set verdicts, and with few questions they are honestly "indeterminate". The full set has all questions and its own entry, and is the only basis of the formal model quality verdict. Evaluation may use the host GPU; L0 runs at the upstream native precision (bf16 for Qwen2.5), and its results are cached by (model, question, evaluation setup) |
| FR-QNT-09 | P0 | **Diagnosis of wrongly answered questions**: for each question L0 answers correctly and L2 wrongly, find the first token where the two answers differ, compare the deviation of L2 from the floating-point values at every layer's observation points at that position, and name the layer and tensor kind the error mainly comes from, as the basis for changing the numeric spec |

### 5.5 S3 Architecture Design and Exploration (ARC)

| ID | Priority | Requirement |
|---|---|---|
| FR-ARC-01 | P0 | **Architecture plugin interface**: every chip architecture is provided as a plugin and declares: supported model structures, parameter space, outputs, and **dependencies on process capabilities** (e.g. "requires a ROM macro with capacity of at least N bits", "requires an SRAM macro", "requires a 1.8V standard cell library") |
| FR-ARC-02 | P0 | **Built-in "Taalas-style hard-wired" parameterized template**: hard-wired weight storage plus a compute array. Tunable parameters include number of compute units, datapath bit width, weight storage implementation, KV cache location, off-chip interface (e.g. SPI/UART), clock |
| FR-ARC-03 | P1 | **"Generic base + model-specific layer" partitioning**: in the design data, separate the model-independent parts (compute array, control, interfaces) from the model-dependent parts (weight data), and report which design data or mask layers need to change when switching to a different model. The physical scheme that truly achieves "change only a few mask layers" is P2 |
| FR-ARC-04 | P1 | **Design space exploration**: sweep template parameters and output a Pareto front based on area, performance, and power models, for agents and humans to choose from |
| FR-ARC-05 | P0 | Produce the **chip specification**, in both a human-readable and a machine-readable version: functional description, interfaces and timing, memory map, selected storage implementation, performance and area targets. The specification references the numeric spec version and must not redefine numeric behavior |
| FR-ARC-06 | P0 | Architecture design must comply with the frozen numeric spec. When numeric behavior needs to change, return to S2 per FR-QNT-07 |
| FR-ARC-07 | P0 | **Observability design**: the specification must include debug and readback interfaces (e.g. reading intermediate registers, single-stepping) that correspond to the observation points in the numeric spec, so that verification reads every observation point without a back door |
| FR-ARC-08 | P2 | Multi-chip architecture: implementation of inter-chip interconnect and model partitioning |

### 5.6 S4 Hardware Generation (RTL)

| ID | Priority | Requirement |
|---|---|---|
| FR-RTL-01 | P0 | Generators produce synthesizable RTL (Verilog/SystemVerilog) and weight storage data from the specification, numeric spec, and quantized weights |
| FR-RTL-02 | P0 | Weight-to-storage mapping is plugin-based: supports forms such as ROM macro initialization data, ROM generator input, and standard-cell constant logic, corresponding to implementation library entries. Each mapping is a versioned **mapping rule** that must provide both a forward mapping (logical weights → storage block contents) and an inverse restoration (storage block contents → logical weights), and must pass a round-trip test of "forward then restore equals the original weights" |
| FR-RTL-03 | P0 | Automatically run lint checks and coding standard checks after generation |
| FR-RTL-04 | P0 | Comply with DP-3: agents must not directly modify generated RTL; they may only modify generators, templates, or configurations. A manual patch is a hard constraint change: it needs human approval and is recorded as an explicit artifact |

### 5.7 S5 Functional Verification (VER)

| ID | Priority | Requirement |
|---|---|---|
| FR-VER-01 | P0 | **Simulation and verification backends are declared by capability**. Capability types include: RTL functional simulation, gate-level zero-delay functional simulation, SDF timing simulation, formal equivalence checking. A backend can only produce evidence corresponding to capabilities it has declared and that have been validated; each piece of evidence records the capability type used |
| FR-VER-02 | P0 | MVP backend configuration: **RTL functional simulation** uses Verilator (P0); **gate-level zero-delay functional simulation** uses Verilator or Icarus Verilog running the netlist with standard cell functional models (P0); **formal equivalence checking** uses Yosys ecosystem tools (P1); candidate backends for **SDF timing simulation** are simulators that support `$sdf_annotate`, such as Icarus Verilog, which must first pass capability validation (P1) |
| FR-VER-03 | P0 | **SDF evidence validity**: SDF timing simulation evidence must come with back-annotation statistics, including the proportion of cells and paths successfully annotated and the constructs that were ignored. When back-annotation is incomplete or ignored, the evidence is judged "Indeterminate" and **cannot count as passing** |
| FR-VER-04 | P0 | The **verification plan** is an artifact, written by the verifier and reviewed by the reviewer, including at least the following categories:<br>① **Regression set**: a fixed question set of no fewer than 20 questions, drawn with a fixed seed from the open task benchmarks of FR-QNT-04, each generating no fewer than 64 tokens. Its role is regression testing, not proof of completeness;<br>② **Directed tests**: reset (including reset mid-inference), power-on initial state, single token and empty input, behavior when reaching and exceeding the maximum context, interface protocol (handshake, backpressure, illegal input), debug readback interface;<br>③ **Numeric boundary tests**: saturation and overflow, maximum and minimum values, rounding ties, extreme Softmax distributions, interval boundaries and switch points of nonlinear approximations;<br>④ **Exhaustive tests**: for units with a small input space (e.g. lookup tables or nonlinear units with 8-bit input), exhaustive comparison against L2;<br>⑤ **Constrained random tests**: random tensors and random token sequences, compared bit by bit against L2;<br>⑥ **Coverage targets**: code coverage and functional coverage points, with thresholds written into the charter |
| FR-VER-05 | P0 | cocotb-based test framework: module-level tests, plus a full-chip "generate text from a given prompt" test. All comparisons are done bit by bit at the observation points of the numeric spec, and decoding follows the decoding rule of the numeric spec |
| FR-VER-06 | P0 | **Divergence localization**: when a comparison fails, automatically report the first divergent layer, operator, element, and clock cycle, together with the relevant waveform excerpt |
| FR-VER-07 | P0 | Verification reports must state results separately by evidence type: "tests passed" (with coverage scope) and "formally proven" must not be mixed |
| FR-VER-08 | P2 | FPGA prototype verification backend: deploy the RTL to an FPGA development board and run it, as the optional L6 |
| FR-VER-09 | P0 | Verification plan completion status and verification reports serve as evidence for the S5 checks |

### 5.8 S6 Physical Implementation (PHY)

| ID | Priority | Requirement |
|---|---|---|
| FR-PHY-01 | P0 | Accomplished through EDA adapters: synthesis (Yosys), floorplanning, placement, clock tree, routing (OpenROAD), static timing analysis (OpenSTA) |
| FR-PHY-02 | P0 | Generate the IO ring and pad arrangement from the process profile and foundry rules; the die size complies with the foundry size rules |
| FR-PHY-03 | P0 | Support integrating memory macros (ROM/SRAM), including macros provided by the foundry or generated by open-source generators. Only macros whose integration views required for the stage are complete and version-consistent can be used (§3.8) |
| FR-PHY-08 | P0 | **Stage entry check**: before entering each of the stages S4–S7, automatically check the integration views of all implementation library entries used in that stage. If the check fails, entry is prohibited and the missing or inconsistent views are listed |
| FR-PHY-04 | P0 | Output area, utilization, timing, and power reports, and save the actual measured values as **measured records**, linked to the implementation library entries used and the run that produced them, for comparison with estimates. Automatically updating the estimation model from these records belongs to FR-EST-10 (P1) |
| FR-PHY-05 | P0 | Layout visualization: view the floorplan and layout in the UI (integrate KLayout or a built-in viewer) |
| FR-PHY-06 | P0 | **DFT**: insert scan chains during synthesis and verify them by simulation (Fault `chain`), generate and compact ATPG test vectors (Fault `cut`/`atpg`), with stuck-at fault coverage of at least 95% (owner decision 2026-10-24) and every uncovered fault point listed; physical design reorders the scan chains by placement; signoff runs the test vectors in zero-delay and SDF gate-level simulation of the routed netlist, checks scan-mode timing, and recomputes the coverage on the final netlist. Applies to the slice that goes through physical design. Transition-delay faults are beyond Fault and are recorded honestly as a tool limitation |
| FR-PHY-07 | P1 | Automatic tuning of physical implementation parameters (agent-driven multi-round attempts, with comparable results) |

### 5.9 S7 Signoff (SGN)

| ID | Priority | Requirement |
|---|---|---|
| FR-SGN-01 | P0 | DRC, LVS, antenna, and density checks. With open-source PDKs, use KLayout, Magic, Netgen, with two independent tools wherever possible per DP-11 (e.g. the two DRC engines KLayout and Magic) |
| FR-SGN-02 | P0 | **Timing signoff**: after parasitic extraction, use STA to check all process corners specified by the charter, checking both setup and hold, as the signoff basis for L5. SDF timing simulation serves as supplementary evidence per FR-VER-02/03 (P1) |
| FR-SGN-02a | P0 | **Timing constraint completeness**: timing signoff must also prove that constraints are complete, including at least: all clocks are defined (including generated clocks); all input and output ports have delay constraints, derived from the interface timing in the specification; there are no unconstrained timing endpoints (e.g. using OpenSTA's unconstrained check); there are no combinational loops. If any item is not met, the timing check is judged "Indeterminate" and **signoff is not allowed**, even if the report shows no violations |
| FR-SGN-02b | P0 | **Timing exception registration**: all timing exceptions (false path, multicycle path, case analysis, disable timing, etc.) must be registered in a list, each with a stated rationale. Timing exceptions are **hard constraints**: agents may propose them, and humans approve them. If an unregistered exception appears in the constraint files, signoff is blocked |
| FR-SGN-03 | P0 | Gate-level zero-delay functional simulation regression and (P1) formal equivalence checking, as evidence for L4 |
| FR-SGN-04 | P0 | Generate the signoff checklist per the process profile, give a verdict for each item, and attach evidence. Each piece of evidence is bound to the hash of the object checked |
| FR-SGN-05 | — | Deleted (owner decision 2026-10-24): waivers are removed; see §3.7.2 |
| FR-SGN-06 | — | Deleted (owner decision 2026-10-24): there is no tapeout, so no signoff reports from foundries or commercial tools are imported |
| FR-SGN-07 | P0 | **Storage contents consistency**: when weights are written from the quantized artifact into storage, they may undergo legitimate transformations such as partitioning (into banks), bit-width packing, transposition, and padding, so the generator's input file is not necessarily byte-identical to the quantized weight file. The consistency check distinguishes "source consistency" from "content consistency" and records three items separately: the **source weight hash** (quantized weight artifact), the **mapping rule and its version** (FR-RTL-02), and the **content hash of each storage block**. Check method: extract the storage block contents from each view of each storage macro, **restore them into logical weights** per the mapping rule, then compare bit by bit against the source weights; padding bits must equal the fill value specified by the mapping rule. Specifically including: ① the contents in the simulation model; ② the contents read back from the LVS netlist or layout; ③ simulation results using the macro are consistent with L2. If any item is missing or inconsistent, signoff is prohibited |

### 5.10 S8 Tapeout Package (PKG, Deleted)

| ID | Priority | Requirement |
|---|---|---|
| FR-PKG-01 | — | Deleted (owner decision 2026-10-24): there is no tapeout and no tapeout package |
| FR-PKG-02 | — | Deleted (owner decision 2026-10-24): there is no tapeout and no tapeout package |
| FR-PKG-03 | — | Deleted (owner decision 2026-10-24): there is no tapeout and no tapeout package |
| FR-PKG-04 | — | Deleted (owner decision 2026-10-24): there is no tapeout and no tapeout package |
| FR-PKG-05 | — | Deleted (owner decision 2026-10-24): there is no tapeout and no tapeout package |

### 5.11 Process Migration (MIG)

| ID | Priority | Requirement |
|---|---|---|
| FR-MIG-01 | P0 | **Migration impact assessment**: when switching the target process (e.g. from GF180MCU to SKY130, or to a foundry process), compare the two processes' profiles, implementation libraries, and PDKs, covering at least: standard cell libraries (voltage, track height, process corners), storage macro availability and specs, IO libraries, metal layers, voltage domains, clock and timing conditions, rule deck formats and tool compatibility |
| FR-MIG-02 | P0 | For each difference, give an impact level and the **earliest stage that must be restarted** (S2 numerics / S3 architecture / S4 generation / S6 implementation), determined automatically in combination with the process capability dependencies declared by the architecture plugin (FR-ARC-01) |
| FR-MIG-03 | P0 | After the assessment result is confirmed, the earliest stage to restart is recorded; the work from that stage on must be developed and tested again, and the old verdicts do not judge the artifacts for the new process |
| FR-MIG-04 | P0 | The migration impact assessment report serves as evidence. Migrations that require restarting from S3 or an earlier stage need the owner's decision |
| FR-MIG-05 | P1 | A pre-assessment against the "intended target process" can already be done at S0 and S3, to expose migration risks as early as possible |

### 5.12 S9 Post-Silicon Testing (SIL, Deleted)

| ID | Priority | Requirement |
|---|---|---|
| FR-SIL-01 | — | Deleted (owner decision 2026-10-24): there is no tapeout and no silicon |
| FR-SIL-02 | — | Deleted (owner decision 2026-10-24): there is no tapeout and no silicon |
| FR-SIL-03 | — | Deleted (owner decision 2026-10-24): there is no tapeout and no silicon |
| FR-SIL-04 | — | Deleted (owner decision 2026-10-24): there is no tapeout and no silicon |
| FR-SIL-05 | — | Deleted (owner decision 2026-10-24): there is no tapeout and no silicon |

### 5.13 Chip Playground (PLY)

| ID | Priority | Requirement |
|---|---|---|
| FR-PLY-01 | P0 | Enter a prompt, select the chain levels to run (L0/L1/L2/L3), and **compare side by side** the outputs of each level |
| FR-PLY-02 | P0 | For ≈ relationships, show the effect difference; for ≡ relationships, show token-by-token agreement and mark the positions of mismatches |
| FR-PLY-03 | P1 | Show the cycle count per token, and the estimated latency and throughput at the target frequency |
| FR-PLY-04 | P1 | Inspect intermediate tensors and differences layer by layer |

### 5.14 Integration and Plugins (INT)

| ID | Priority | Requirement |
|---|---|---|
| FR-INT-01 | P0 | Plugin types include at least: model loader, quantizer, numeric spec and bit-exact model, architecture template, RTL generator, simulation and verification backend, EDA adapter, PDK, process profile and implementation library, signoff check, LLM provider, UI panel |
| FR-INT-02 | P0 | **EDA adapters** declare tools in a uniform way: inputs, outputs, parameters, version, capabilities, runtime environment (container image), result parser. Open-source and commercial tools use the same interface |
| FR-INT-03 | P1 | Commercial EDA adapters (Synopsys, Cadence, Siemens): the MVP only defines the interface and the license configuration method; implementation is not required |
| FR-INT-04 | P0 | **PDK plugins**: the MVP has all open-source PDKs built in: GF180MCU, SKY130, IHP SG13G2. Support local import of NDA PDKs, automatically applying the NDA label on import |
| FR-INT-05 | P0 | **LLM provider plugins**: DeepSeek, Anthropic Claude, OpenAI GPT, and any OpenAI-compatible interface (including locally deployed models). Each provider is marked "local" or "cloud" |
| FR-INT-06 | P0 | **MCP service**: externally exposes capabilities such as project queries, task operations, artifact reading, and the estimator, subject to permission, audit, and data label constraints (see §6.1, §8) |
| FR-INT-07 | P0 | **CLI**: every core operation available in the UI is also available in the CLI, for scripting and invocation by external agents |
| FR-INT-08 | P0 | Toolchains are distributed as container images with pinned versions, automatically fetched and updated by the workbench (see NFR-13) |
| FR-INT-09 | P0 | Plugins can be written in any language and communicate with the workbench via a unified protocol. The MVP supports at least TypeScript plugins and Python plugins |

---

## 6. Agent System Requirements (AGT)

This is the core that distinguishes llmchip-harness from traditional EDA platforms. The goal is: **any agent can keep iterating on development without drifting from the goal.**

### 6.1 Two Connection Modes

| Mode | Definition | Scope the workbench can enforce |
|---|---|---|
| **Managed agent (Managed)** | An agent running in the workbench's controlled execution environment, including: the internal agent runtime; and external coding agents launched under workbench management (e.g. Claude Code, Codex CLI), which run in a sandbox that mounts only the project working directory, with network egress going through the egress gateway | All constraints of §6.2–§6.7, guaranteed by the execution environment |
| **External client (External Client)** | An agent the user runs on the host machine, connected to the workbench via MCP/API | **Limited to operations initiated through the workbench API**: which artifacts can be read, which tasks can be initiated, data label filtering of returned content. The workbench **cannot** restrict the client's other capabilities on the host machine, such as files and terminal |

| ID | Priority | Requirement |
|---|---|---|
| FR-AGT-01 | P0 | The UI, documentation, and MCP connection instructions must clearly state the control boundary of the external client mode, and must not claim that "external clients can only operate through the workbench" |
| FR-AGT-02 | P0 | Default permissions for external clients: read-only, plus initiating tasks. They cannot modify constraints or policies; NDA content is not returned to them by default (see §8) |
| FR-AGT-03 | P1 | Managed launch of external coding agents: the user selects Claude Code / Codex, etc., and the workbench launches it in a sandbox, making it a managed agent |

### 6.2 Iteration Loop

Every iteration of a managed agent must go through all of the following six steps, leaving a record of each step in the event log:

| Step | Meaning in the workbench | Requirement |
|---|---|---|
| ① Clarify the goal | Take the goal and acceptance criteria of this task from the charter and the current stage | FR-AGT-04 (P0): a task must be linked to a goal or requirement ID in the charter; a task without a goal cannot start |
| ② Bound the structure | Make explicit the scope that may be changed this time (which generators, configurations, files) and the parts that must not be touched | FR-AGT-05 (P0): a task declares its write scope; the workbench enforces it at execution time; out-of-scope writes are rejected and recorded as incidents |
| ③ Implement in small steps | Make only one small, verifiable change at a time | FR-AGT-06 (P0): each change forms an independent changeset that can be rolled back individually. Changes exceeding the size threshold must be split |
| ④ Review | The reviewer agent reviews the change against the charter and constraints | FR-AGT-07 (P0): an executor cannot review its own changes. Review comments and conclusions are recorded |
| ⑤ Actual verification | Run the corresponding tests and update the golden chain | FR-AGT-08 (P0): a change must be verified by actually running, and must obtain a "pass" verdict per §3.9 before it can be merged. An agent claiming "done" does not count as done |
| ⑥ Update constraints | Codify what was learned this time | FR-AGT-09 (P0): conclusions from verification failures or incidents are written back to the charter or stage playbook in the form of "constraint proposals". Soft constraints are approved by the reviewer, hard constraints by humans |

### 6.3 Two Levels of Entry Point

| ID | Priority | Requirement |
|---|---|---|
| FR-AGT-10 | P0 | **Developing the workbench itself**: the `AGENTS.md` in the repository root is the agent entry point for developing llmchip-harness itself; it specifies product goals, module boundaries, coding constraints, verification requirements, and prohibitions, and likewise follows the loop of §6.2 |
| FR-AGT-11 | P0 | **Chip projects**: the project charter `CHARTER.md` is the agent entry point for a chip project. Each stage plugin provides its own **stage playbook**, describing the stage's methods, common pitfalls, and acceptance criteria |

### 6.4 Handoff Readiness

| ID | Priority | Requirement |
|---|---|---|
| FR-AGT-12 | P0 | **Handoff pack**: the workbench can at any time generate a summary of the project's current state, including the charter, current stage and tasks, golden chain status, unhandled incidents, recent decisions, the constraint list, and suggested next steps. Any new agent can continue working after reading it. The handoff pack follows the data label rules of §8 |
| FR-AGT-13 | P0 | An agent session can be interrupted at any time and later continued by the same or another agent based on the handoff pack, without losing progress (DP-4) |
| FR-AGT-13a | P0 | **Data permission check for handoff**: before switching an agent or its LLM, the workbench compares the data labels required for the handoff against the permitted level of the target LLM provider. If permissions suffice, the handoff proceeds normally; if permissions are insufficient (e.g. the project has entered the NDA phase while the target is a cloud model), **the switch is blocked** and two options are offered: switch to a local model, or use a registered declassification rule (FR-SEC-04) to generate a declassified handoff pack that hands over only the declassified scope |
| FR-AGT-14 | P0 | Each role can be configured with an LLM independently, and it can be switched while the project is in progress |

### 6.5 Drift Prevention

| ID | Priority | Requirement |
|---|---|---|
| FR-AGT-15 | P0 | Every change must state which goal or requirement ID it serves; review checks whether it drifts |
| FR-AGT-16 | P0 | **Spin detection**: when the same class of failure occurs N times in a row (configurable), or a task's time or consumption exceeds a threshold, automatically pause and escalate to a human |
| FR-AGT-17 | P1 | The orchestrator periodically outputs progress reports: what has been completed, what remains to reach the goal, and what the risks are |

### 6.6 Security and Permissions (Applies to Managed Agents)

| ID | Priority | Requirement |
|---|---|---|
| FR-AGT-18 | P0 | Managed agents **cannot directly access the host file system or shell**; they can only operate within the project sandbox through tools provided by the workbench |
| FR-AGT-19 | P0 | **Capability-based permissions**: each role and each task receives only the minimal tool set and read/write scope needed to complete that task |
| FR-AGT-20 | P0 | **Artifacts cannot be deleted**: an agent's "delete" is only a mark. Actual cleanup can only be initiated by a human and has a retention period |
| FR-AGT-21 | P0 | **Dangerous operations require human approval**: modifying hard constraints, modifying the workbench's own configuration or plugins, modifying the data egress policy, runs that exceed budget. Note: **egress of NDA data is not among the approvable items**; see FR-SEC-05 |
| FR-AGT-22 | P0 | **Resource quotas**: each task and project has limits on time, CPU, memory, disk, tokens, and cost; exceeding a limit automatically pauses it |
| FR-AGT-23 | P0 | **Emergency stop**: a human can pause all agents at any time with one click, preserving the current state |
| FR-AGT-24 | P1 | Treat all external content such as model files, documents, and tool outputs as "data" rather than "instructions", to defend against prompt injection |

### 6.7 Incident Exposure

| ID | Priority | Requirement |
|---|---|---|
| FR-AGT-25 | P0 | Incidents defined in §3.10 are recorded automatically and prominently displayed in the UI, filterable by project, stage, type, and severity |
| FR-AGT-26 | P0 | From an incident, one click jumps to the related events, run logs, artifacts, and agent conversation excerpts |
| FR-AGT-27 | P0 | When an incident is closed, the cause and handling measures must be recorded, and whether to generate a constraint proposal is decided per §6.2 ⑥ |

---

## 7. Traceability and Recoverability (TRC)

| ID | Priority | Requirement |
|---|---|---|
| FR-TRC-01 | P0 | **Append-only event log**: records all agent, tool, and human operations, including time, actor, action, inputs, outputs, reason, and resource consumption |
| FR-TRC-02 | P0 | Content-addressed artifact store and lineage graph (see §3.5) |
| FR-TRC-03 | P0 | **Task recoverability**: after an application crash, power loss, or restart, unfinished tasks can continue from the most recent checkpoint, or be explicitly marked as failed and retried |
| FR-TRC-04 | P0 | **Checkpoints and rollback**: can return to any historical checkpoint. Rollback is done by creating a new branch, without destroying history |
| FR-TRC-05 | P0 | **Invalidation propagation**: when an upstream artifact changes, downstream artifacts, chain evidence, and verdicts are automatically marked as no longer applying to the new inputs (see §3.4.3, §3.7.1) |
| FR-TRC-06 | P0 | **Layered verdict recording**: each run records the four layers of status separately per §3.9, displayed separately in the UI |
| FR-TRC-07 | P1 | **Reproducibility**: re-executing from the lineage manifest yields the same artifacts (tool versions pinned, random seeds fixed). When reproduction is not possible, the differences must be explicitly reported |
| FR-TRC-08 | P1 | Audit export: export the complete trajectory for a specified time period or project |
| FR-TRC-09 | P0 | The workbench's code, generators, and configurations are managed with git. Generator code written by agents in chip projects is likewise placed under version control |
| FR-TRC-10 | P0 | **Checkpoints of long runs**: a plugin can register checkpoint artifacts during a run (e.g. the database after each physical design step, a long simulation split by prompt); when a run is interrupted or fails, a retry or a resume of the pipeline node hands the latest checkpoint to the handler, which continues from there instead of starting over |

---

## 8. Data Security (SEC)

PDKs and library files are under NDA; tool logs, error messages, generated files, and report excerpts may all leak this content. Therefore, labeling only the raw PDK is not enough; **derived data** must be controlled.

| ID | Priority | Requirement |
|---|---|---|
| FR-SEC-01 | P0 | **Data labels**: every artifact, log, run output, event, and agent context fragment carries a label: Public / Internal / NDA. Label levels are extensible. Importing an NDA PDK or foundry materials automatically applies the NDA label |
| FR-SEC-02 | P0 | **Label propagation**: the label of derived data equals the highest level among all its input labels, and is never automatically lowered. When a tool runs with NDA data as input, its stdout/stderr, reports, generated files, and error messages all inherit the NDA label. If an agent context contains even one NDA fragment, the request is treated as NDA |
| FR-SEC-03 | P0 | **Unified egress gateway**: all content sent to LLM providers, MCP external clients, and any network destination must pass through the single egress gateway. The gateway decides to release or intercept based on the label of each content fragment and the permitted level of the destination. Cloud LLMs by default only allow "Public" and "Internal" |
| FR-SEC-04 | P0 | **Declassification rules**: lower-level data can only be extracted from NDA artifacts through registered declassification rules. A rule precisely specifies the structured fields that may be extracted (e.g. total area, worst timing slack value, total violation count), not free text. Rules are set by the owner and recorded; agents cannot add or modify rules. New artifacts generated by declassification record lineage. **The default rule set is empty**, i.e. by default no NDA-derived content is sent out; the workbench provides recommended templates, and the owner decides whether to enable them |
| FR-SEC-05 | P0 | **NDA rules cannot be overridden by ordinary approval**: the "dangerous operation approval" of FR-AGT-21 does not apply to NDA data. NDA-level content can only be sent to LLM providers marked "local". Modifying declassification rules is a policy change; it only affects subsequent requests and is recorded |
| FR-SEC-06 | P0 | **Fingerprint scanning (second line of defense)**: when NDA materials are imported, automatically extract distinctive content fingerprints (e.g. layer names, rule names, cell names, characteristic text snippets). The egress gateway scans all outbound requests for fingerprints; on a hit it intercepts and records an incident, as a backstop for missed labels |
| FR-SEC-07 | P0 | **Egress audit**: all outbound requests keep an audit record, including the destination, the label of each content fragment, and the release or intercept conclusion |
| FR-SEC-08 | P0 | **Interception test suite**: a built-in synthetic "fake PDK" with recognizable sensitive content. Tests cover all model request paths: agent conversations, tool result return, error messages, summary generation, handoff packs, MCP responses, verifying that every path intercepts |
| FR-SEC-09 | P0 | In the local UI, humans can view NDA data. When later moved to server deployment, client connections must be authenticated and use encrypted transport (see NFR-08) |

---

## 9. UI and Interaction (UI)

### 9.1 Form

| ID | Priority | Requirement |
|---|---|---|
| FR-UI-01 | P0 | Provide a **desktop application** (referencing the interaction style of the Codex app / DeepSeek Harness) as well as browser access. Users can launch it with a double-click |
| FR-UI-02 | P0 | **The UI language supports Chinese and English**, and users can switch at any time |
| FR-UI-03 | P0 | First-launch onboarding: automatically check and prepare the runtime environment (see NFR-13), configure LLMs, run the sample project with one click |
| FR-UI-04 | P0 | The UI is never blocked by any long-running task: all long tasks have progress and real-time logs, and can be cancelled |

### 9.2 Main Views

| ID | Priority | View | Content |
|---|---|---|---|
| FR-UI-05 | P0 | Main layout | Left column: project and session list; middle column: agent workflow (conversation, reasoning summary, tool calls, results, scrolling in real time); right column: context panel (artifacts, golden chain, trajectory) |
| FR-UI-06 | P0 | Project overview | Stage progress bar, golden chain indicator lights, the verdict status of each stage's required checks, budget, incident count |
| FR-UI-07 | P0 | Trajectory view | Timeline and an interactive artifact lineage graph. Clicking any node shows details and allows navigation |
| FR-UI-08 | P0 | Estimator | Form, result ranges, calculation process, four feasibility conclusions, supports scheme comparison |
| FR-UI-09 | — | Approval center | Deleted (owner decision 2026-10-24): approval gates are removed |
| FR-UI-10 | P0 | Incident center | See §6.7 |
| FR-UI-11 | P0 | Artifact browser | Report rendering, RTL code viewing, waveform viewing (integrate GTKWave/Surfer or built-in), layout viewing. The four-layer verdict status of each run is displayed separately |
| FR-UI-12 | P0 | Chip Playground | See §5.13 |
| FR-UI-13 | P0 | Settings | LLM providers and role assignment, plugin management, process profiles and implementation libraries, toolchains, quotas, data egress policy and declassification rules |
| FR-UI-14 | P0 | Step pages and the two entries | One page per stage (engineering step): input artifacts, the two buttons "run the development" and "run all tests", the pipeline node graph (state, duration, resources, live log, cancel, resume from a node), the step's own result views, the check list (verdict, evidence, second source, negative control), and the upstream checks that did not pass. The project overview has the two buttons "develop all six steps" and "test all six steps" |

---

## 10. Non-Functional Requirements (NFR)

| ID | Priority | Category | Requirement |
|---|---|---|---|
| NFR-01 | P0 | Lightweight | The desktop application itself (excluding EDA tool images and models) has a small installer; startup time and idle memory usage meet the target values specified in §10.1 |
| NFR-02 | P0 | Stability | A crash of a plugin, EDA tool, or agent must not crash the workbench kernel or UI. Heavy computation is isolated from the workbench process |
| NFR-03 | P0 | Responsiveness | With background tasks at full load, the response time of UI operations meets the target values specified in §10.1 |
| NFR-04 | P0 | Recoverability | After power loss or the process being killed, committed events and artifacts are not lost, and tasks can be resumed per FR-TRC-03 |
| NFR-05 | P0 | Private deployment | All components are deployed locally. Apart from cloud LLM APIs actively configured by the user, there are no external dependencies. When using local LLMs, it can run fully offline |
| NFR-06 | P0 | Data security | Data labels, propagation, egress gateway, and audit are enforced per §8 |
| NFR-07 | P0 | Key management | API keys and license configurations are stored encrypted and are not written into logs or artifacts |
| NFR-08 | P0 | Deployment form | MVP: the workbench kernel runs in WSL2 on Windows, and the Windows desktop application connects to the local kernel; native Linux x86_64 is also supported. P2: the kernel is deployed on a private server, and clients on multiple computers connect to the same workbench, which then requires authentication and encrypted transport. macOS is P2 |
| NFR-09 | P0 | Extensibility | The plugin API is versioned and remains compatible. Model IR and the estimator support, in their data structures, up to trillion-parameter, multi-chip scale |
| NFR-10 | P1 | Reproducibility | Toolchain image versions are pinned and random seeds are fixed (see FR-TRC-07) |
| NFR-11 | P0 | Compliance | Track the licenses of model weights, datasets, EDA tools, and PDKs, and flag them at release and delivery |
| NFR-12 | P1 | Observability | The workbench's own logs, metrics, and health status can be viewed, to aid troubleshooting |
| NFR-13 | P0 | **Out of the box** | Users do not need to manually install any dependencies. The installer and first-launch onboarding automatically prepare the entire runtime environment: the WSL runtime, container runtime, Python runtime, and EDA tool images. For steps that genuinely cannot be completed automatically (e.g. "enable WSL", which requires administrator privileges or a reboot), a **one-click action** and clear prompts must be provided; users must not be required to search the web or consult documentation themselves. An offline installer is provided for environments without network access |
| NFR-14 | P0 | Environment self-check and repair | The workbench can self-check the runtime environment at any time (container runtime, image integrity, disk space, etc.), and offers a one-click fix when a problem is found |
| NFR-15 | P0 | Resource scheduling | The scheduler allocates the machine's resources by the CPU, memory and GPU each handler declares, and queues runs beyond the limits; only one run uses the GPU at a time; container runs are limited in CPU and memory |

### 10.1 Reference Environment and Measurement Conditions

To ensure that performance and installation metrics such as NFR-01, NFR-03, AC-M1-01, and AC-01 can be accepted repeatably, the following conditions are used uniformly. All values in the table are target values, to be confirmed after architecture evaluation.

| Item | Specification |
|---|---|
| Reference machine | Windows 11 + WSL2; 8 physical cores; 16 GB memory; NVMe SSD; hardware virtualization enabled in BIOS |
| Memory measurement scope | The sum of the resident memory (RSS) of the workbench kernel process and the desktop application process. **Excludes** EDA tool containers, Python compute processes, local LLMs, and the overhead of the WSL virtual machine itself |
| Definition of "idle" | 1 project open, no running tasks, UI resting on the project overview |
| Startup time | From double-clicking the application until the main UI is operable. Two cases: WSL environment already running, target ≤ 5 seconds; WSL environment not running (cold start), target ≤ 15 seconds |
| Load condition for UI responsiveness | 2 EDA tool tasks and 1 agent streaming output running simultaneously, with background tasks saturating all CPU except 2 cores. The metric is p95 |
| Installation duration | Timed from running the installer, **excluding** download time and reboot time |
| The only permitted manual prerequisite step | If hardware virtualization is not enabled in the BIOS, the software cannot enable it on the user's behalf. In this case the installer must detect it and give clear, machine-specific instructions |

---

## 11. MVP Scope

### 11.1 MVP Reference Project

| Element | Choice | Notes |
|---|---|---|
| Comparison model | `stories260K` (llama2.c, about 260K parameters, Llama 2 structure, MIT license) | dim 64, 5 layers, 8 attention heads, 4 KV heads, maximum context 512, ships with the `tok512` tokenizer. L0 is the FP32 inference of `run.c`. Used for demonstration and comparison in software (S1–S3 and simulation); by the calibration results it does not fit in 2 mm² |
| Reference model (design subject) | **Qwen2.5-0.5B-Instruct** (`Qwen/Qwen2.5-0.5B-Instruct`, Qwen2 structure: 24 layers, hidden 896, 14 Q heads / 2 KV heads, intermediate 4864, vocabulary 151,936, tied embeddings, about 494M parameters, Apache-2.0 license) | Decided by the owner on 2026-10-24, replacing the self-trained model of about 80K parameters decided on 2026-10-18. The full model goes through specification, RTL and verification. Its W4 weights are about 1.98 Gbit, which at the calibrated GF180 standard-cell ROM density (about 2.6 µm²/bit) needs about 5,100 mm², several times the single-die reticle limit; no open-source process can take the full model through physical design, so a slice carries synthesis, physical design and signoff |
| Slice and proxy | ① **Qwen slice**: a slice cut from the reference model's real weights and aligned to the numeric spec's group boundaries (grown in levels from one attention head), using the same templates and the same numeric spec as the full model; ② the proxy of about 5K parameters (Llama structure, ternary weights, fixed seed, untrained) | The slice goes through synthesis, physical design and signoff on the three open-source PDKs; its outputs are intermediate values that exist in L2, so they compare bit for bit. The proxy and the existing engines stay as the fast regression of every commit; its accuracy results mean nothing |
| How the model is chosen | Decided by the owner (2026-10-24) | The storage implementation calibration (FR-EST-13) decides the size of the slice, not the model |
| Architecture | Built-in Taalas-style hardwired parameterized template: ROM arrays with parallel MACs and a fixed schedule (`taalas-tile/1`, implemented in R2); the spatial engine `taalas-spatial/1` stays for the proxy | Weights hardwired in on-chip ROM; the location of the KV cache is decided in S3 |
| Processes | **All open-source PDKs**: GF180MCU (gf180mcuD), SKY130 (sky130A), IHP SG13G2 | The slice goes through synthesis, physical design and signoff on each PDK. No tapeout |
| Candidate storage implementations on GF180 | ① Standard-cell logic ROM: integration views come from the standard cell library; expected to be usable; ② OpenRAM's GF180 ROM generator: officially marked experimental, mainly outputs GDS, SPICE, logs, and configuration; the simulation model, Liberty, and LEF must be supplied separately, otherwise it cannot be used for S4–S6; ③ Official SRAM macro `gf180mcu_fd_ip_sram`: four configurations, 64/128/256/512 × 8 | Each candidate must go through FR-EST-13/14 calibration and reach a conclusion of "Usable" or "Unusable". Area density is based on calibrated values. The standard-cell ROM density of SKY130 and IHP SG13G2 must be calibrated likewise |
| Die size | Decided in S3 from the slice size and each PDK's pad ring | The former 1.2×2 mm prerun constraint (owner decision 2026-10-18) applies only to the proxy's vector engine |
| Tapeout target | None (the owner decided on 2026-10-24 not to tape out) | The Sephonchitech 180nm BCD process profile stays as data and can still be used by the migration assessment (FR-MIG) |
| Toolchain | Yosys, eqy, SymbiYosys, MCY, OpenROAD/LibreLane, OpenSTA, KLayout, Magic, Netgen, Verilator, Icarus Verilog, cocotb, pyuvm, Fault, lm-evaluation-harness, EvalPlus | All open source; EDA tools are provided as container images pinned by digest |

### 11.2 MVP Includes

- All P0 requirements in Chapters 5–10.
- The full model through specification, RTL and verification; the slice through synthesis, physical design and signoff on GF180MCU, SKY130 and IHP SG13G2 (S0–S7).
- Process migration impact assessment (FR-MIG P0), for switching between open-source processes.
- DFT (FR-PHY-06), model quality evaluation on open task benchmarks (FR-QNT-04/08/09), step entries and pipelines (FR-PRJ-07/08, FR-UI-14).

### 11.3 MVP Excludes (P2 or Later Versions)

- Implementation of multi-chip partitioning. Multi-chip estimation is P1 (FR-EST-06).
- Concrete implementations of commercial EDA adapters (only the interface is defined).
- FPGA prototype verification backend.
- Tapeout, the tapeout package and post-silicon testing (owner decision 2026-10-24).
- The physical implementation of "change only a few mask layers to switch models".
- Package design, production test (ATE) solutions.
- Server deployment, multi-user collaboration, permission groups.
- MPW shuttle schedule management. The process profile only keeps pricing and size rules.
- Quantization-aware training and distillation of large models.

### 11.4 Milestones (Without Dates)

| Milestone | Content | Completion criteria |
|---|---|---|
| M1 Skeleton | Kernel, event log, artifact store, plugin mechanism, task scheduling and recovery, verdict layers, desktop application, out-of-the-box installation, LLM provider integration, egress gateway, estimator (using empirical values) | Passes the §12.1 skeleton acceptance. **Does not require** any real physical flow to run through |
| M1.5 Storage calibration | Connect real tools on GF180MCU and complete FR-EST-13/14 | Passes the §12.2 calibration acceptance; reference model determined |
| M2 Model to spec | S1–S3 | Obtain L0, the numeric spec, and L2; the accuracy and chip specification checks pass |
| M3 RTL and verification | S4–S5 | Verification plan fully completed, L3 ≡ L2 |
| M4 Physical implementation and signoff | S6–S7 (GF180MCU) | Obtain a GDS that passes DRC/LVS with no STA violations (the planned tapeout package was deleted with S8) |
| M5 Migration to the tapeout process | Deleted (owner decision 2026-10-24): no tapeout | — |
| M6 Post-silicon testing | Deleted (owner decision 2026-10-24): no tapeout | — |
| R0 Documents and kernel cleanup | Revise the requirements, architecture, AGENTS.md and implementation guide per the decisions of 2026-10-24; remove approval gates, waivers and the tapeout package | Same version numbers in both languages; `yarn verify` passes |
| R1 Pipelines and resources | Step pipelines, the two entries, resource scheduling, resuming from checkpoints | The tests of FR-PRJ-07/08, FR-UI-14, FR-TRC-10, NFR-15 pass |
| R2 Specification | L0/L2 with KV cache, fast L2, calibrated quantization, benchmark evaluation and diagnosis, `taalas-tile/1` design, slice and transaction-level model, SKY130/IHP process profiles and calibration | Every check of the reference model's specification step has a conclusion |
| R3 RTL | Template library, generator, the three ROM views, three linters | Both the full model and the slice generate, and the RTL checks pass |
| R4 Verification | Unit, formal, per-layer, full-chip, directed, coverage, mutation, CDC verification | All verification of the slice passes, with a mutation detection rate of at least 95%; the full model is bit-exact on all 24 layers |
| R5 Synthesis and DFT | Synthesis, formal equivalence with two engines, scan chains and ATPG, three PDKs | The slice's synthesis checks (including DFT and negative controls) pass on the three PDKs |
| R6 Physical design | Physical design in steps, hierarchy, IR drop, three PDKs | The slice is routed on the three PDKs and the physical design checks pass |
| R7 Signoff | Every signoff check with its second source and negative control, ROM readback, GDS XOR, signoff summary | Every signoff check of the slice on the three PDKs has a conclusion, and the signoff summary passes |
| R8 Growing the slice | Grow the slice level by level, each level through all six steps | All tests of the six steps pass at each level, or the limiting level and step are recorded honestly |

---

## 12. MVP Acceptance Criteria (AC)

Acceptance is divided into three groups by milestone: §12.1 skeleton (M1), §12.2 storage calibration (M1.5), §12.3 full MVP acceptance (after M2–M4 are complete). Performance and installation metrics are measured under the conditions of §10.1. Negative acceptance (the "must block" kind) is as important as positive acceptance: it proves the rules are actually enforced, not merely written in documents.

### 12.1 Skeleton Acceptance (M1)

This group only accepts the mechanisms of the workbench itself. Stub tools (plugins that simulate tool output) or simple real tools suffice; **no real chip physical flow is required to run through**.

| ID | Acceptance criterion |
|---|---|
| AC-M1-01 | Installation: on the reference machine, the user completes all dependency preparation solely through the installer and the one-click actions in first-launch onboarding, without manually installing any software and without consulting external documentation |
| AC-M1-02 | Scheduling and recording: a managed agent executes a simple task containing multiple tool calls in the sandbox; the event log, artifacts, lineage, and four-layer verdict status are fully recorded and can be viewed in the UI |
| AC-M1-03 | Verdict layers: when a stub tool exits normally but outputs a truncated report, the related checks are judged "Indeterminate" and cannot become passing |
| AC-M1-04 | Recovery: forcibly terminate the workbench process while a task is running; after restart the state is intact and the task can be resumed or retried |
| AC-M1-05 | Deleted (owner decision 2026-10-24): approval gates are removed |
| AC-M1-06 | Deleted (owner decision 2026-10-24): conditional approval was removed with the gates |
| AC-M1-07 | Security: when a managed agent writes files outside its scope, deletes artifacts, or executes unauthorized commands, all are intercepted and recorded as incidents; when an external client initiates an operation beyond its permissions via MCP, it is rejected |
| AC-M1-08 | Data security: run the interception test suite (FR-SEC-08); all requests on all model request paths that contain "fake PDK" sensitive content are intercepted, and audit records are complete |
| AC-M1-09 | Handoff: (a) when the target LLM has the required data permissions, after switching to another vendor's LLM, the new agent continues and completes the task based on the handoff pack; (b) when the handoff content carries the NDA label and the target is a cloud model, the switch is blocked, with a prompt to switch to a local model or generate a declassified handoff pack |
| AC-M1-10 | Estimator: using empirical values, for the three models stories260K, 0.5B, and 8B under a 180nm process profile, give the complete area breakdown, cost range, minimum performance estimate, and one of the four feasibility conclusions; when data is missing, the conclusion is "Insufficient data" and the missing parameters are listed |
| AC-M1-11 | Performance: meet the target values of NFR-01 and NFR-03 under the conditions of §10.1 |
| AC-M1-12 | After switching the UI between Chinese and English, all UI text displays correctly |
| AC-M1-13 | **Prevent lowering of standards**: a check originally fails; without the design changing, an agent modifies its check standard (e.g. lowers the coverage threshold) so that it passes: the modification is identified as a hard constraint change, requires human approval, and is recorded as an incident |

### 12.2 Storage Calibration Acceptance (M1.5)

| ID | Acceptance criterion |
|---|---|
| AC-M15-01 | On GF180MCU, calibration is completed for every candidate storage implementation listed in §11.1, each ending as "Usable" (with calibrated values and integration view check results) or "Unusable" (with failure evidence) |
| AC-M15-02 | At least one "Usable" implementation meets the reference project's needs, and the reference model is determined on that basis (since 2026-10-24 the owner names the reference model, and the calibration decides the slice size); otherwise a clear reason is given |
| AC-M15-03 | For a candidate implementation missing a required view, the stage entry check (FR-PHY-08) blocks it from entering the corresponding stage and lists the missing views |

### 12.3 Full MVP Acceptance

| ID | Acceptance criterion |
|---|---|
| AC-01 | On the reference machine, the sample project is launched within 30 minutes of running the installer (excluding download and reboot time) |
| AC-02 | The sample project automatically goes through S0–S7 on GF180MCU: agents execute, each stage is judged by its own checks, and no human approval is needed |
| AC-03 | Under the same version of the numeric spec, all items of the verification plan (FR-VER-04) are executed: in the regression set, directed tests, numeric boundary tests, exhaustive tests, and constrained random tests, the L3 and L2 observation points match bit by bit, and coverage reaches the charter thresholds. The verification report states results separately by evidence type |
| AC-04 | On the full benchmark set, the score drop of L2 relative to L0 on every benchmark is within the allowed value of FR-QNT-04, with enough questions to conclude; every question L0 answers correctly and L2 wrongly has a diagnosis (FR-QNT-09) |
| AC-05 | The final GDS has zero DRC violations under GF180MCU rules (no waivers; the approved rule exclusion on the PDK's own IO cells is counted apart per §3.7.4) and LVS matches; STA with parasitics has no setup or hold violations at all process corners specified by the charter, the constraint completeness checks (FR-SGN-02a) all pass, and there are no unregistered timing exceptions |
| AC-06 | **Negative acceptance (timing)**: after deliberately deleting a necessary constraint (e.g. an input delay constraint or a clock definition), the timing check is judged "Indeterminate", signoff is blocked, and the UI does not show a pass. When an unregistered false path is added, signoff is likewise blocked |
| AC-07 | **Storage contents consistency**: if weights are stored in storage macros, all checks of FR-SGN-07 pass; after deliberately modifying 1 bit of the macro contents, the check fails and blocks signoff; when only the packing format of storage blocks changes while the logical weights are unchanged, the check still passes. If weights use a standard-cell logic ROM, this is covered by AC-03 and AC-05 |
| AC-08 | Starting from the GDS, one can trace in the UI back to the weight file hash, and to all intermediate runs, tool versions, and verdicts |
| AC-09 | After deliberately injecting an RTL defect, the verification failure is recorded as an incident and automatically localized to the first divergent operator and cycle |
| AC-10 | Deleted (owner decision 2026-10-24): approval gates and export are removed |
| AC-11 | Deleted (owner decision 2026-10-24): external signoff import (FR-SGN-06) is deleted |
| AC-12 | **SDF evidence validity**: a simulation result whose SDF back-annotation was ignored or incomplete cannot count as passing SDF timing verification |
| AC-13 | **Migration assessment (with dependency)**: when the current design's weight storage depends on a ROM macro, given a simulated target process profile that "has no ROM macro", the migration impact assessment indicates the need to roll back to S3 (or earlier), records that restart stage, and needs the owner's decision |
| AC-14 | **Migration assessment (without dependency)**: when the current design uses a standard-cell logic ROM and does not depend on a ROM macro, the same "no ROM macro" target process profile **should not** require restarting from the architecture stage or earlier; the assessment only reports the differences that are actually affected |
| AC-15 | **The two entries**: after starting "run the development" and "run all tests" in any stage, kill the kernel; after a restart the pipeline continues from the interrupted node, and the test summary lists every check's verdict and reason |
| AC-16 | **DFT**: on every open-source PDK the slice gets scan chains verified by simulation, stuck-at fault coverage of at least 95%, and every test vector gives the expected outputs on the routed netlist |
| AC-17 | **Negative controls**: every signoff check reports its negative control (a known injected defect); when any negative control is not reported, that check is "indeterminate" |

---

## 13. Risks and Open Items

### 13.1 Risks

| Risk | Impact | Mitigation |
|---|---|---|
| The open-source PDKs have no ROM compiler | Weights can only use standard-cell ROM; the area is large, and the full 0.5B model cannot go through physical design | The full model goes as far as verification, and the slice carries physical design and signoff (§11.1); the slice grows in levels |
| Differences between the three open-source PDKs (cell libraries, IO, rule deck coverage) | The same slice concludes differently on different PDKs | Migration impact assessment (FR-MIG); the signoff summary marks single-source or not-applicable checks per PDK |
| Uncertain density of storage implementations | Large deviation in estimation conclusions | Storage implementation calibration (FR-EST-13) completed with priority in M1.5 |
| Incomplete integration views for storage macros | The macro is generated successfully but cannot be used for simulation, synthesis, or place and route | The implementation library registers integration views, stage entry check (FR-PHY-08); fall back to standard-cell logic ROM if necessary |
| Incomplete timing constraints | Some paths are never checked at all, yet "no violations" is shown | Constraint completeness checks and timing exception registration (FR-SGN-02a/02b), and the negative acceptance AC-06 |
| Open-source signoff tools do not cover everything (e.g. OpenSTA has no crosstalk analysis) | Signoff conclusions are optimistic | Second sources and negative controls (DP-11); OCV derating and margin; stated honestly in the signoff summary |
| Quality loss of Qwen2.5-0.5B after quantization | Benchmark scores drop beyond the allowed values | Mixed precision and calibrated quantization; diagnosis locates the layer and tensor; the criteria are not lowered |
| KV cache location | On-chip SRAM area is tight; placing it off-chip requires test board and interface support | An explicit decision item in S3, quantitatively compared by the estimator |
| Insufficient capability of the SDF timing simulation backend | Lack of supplementary timing evidence | Timing signoff is based on STA; the SDF backend must first pass capability validation (FR-VER-02/03) |
| Conflict between NDA and cloud LLMs | Risk of breach of contract | §8 label propagation, egress gateway, fingerprint scanning; optional fully local LLMs |
| Agent capability and reliability | Slow progress, repeated failures | Small-step iteration, spin detection, human backstop |
| LLM API costs | Costs exceed expectations | Quotas (FR-AGT-22); choose cost-effective models per role |
| Duration of the full benchmark set | Daily testing slows down | Daily quick set (5 questions per subject or benchmark); the full set has its own entry; use the host GPU; resume from checkpoints |
| WSL not enabled | First installation requires administrator privileges and a reboot | One-click enablement with clear prompts (NFR-13) |

### 13.2 Open Items

- There is no tapeout, so the foundry inquiry ([drafts/Foundry_Inquiry_Email_zh.md](drafts/Foundry_Inquiry_Email_zh.md)) is on hold.
- The model quality criteria (the allowed drop of each benchmark) were decided by the owner (FR-QNT-04) and are written into the charter.
- Parameters of stories260K such as vocabulary size and FFN hidden dimension are automatically read and confirmed by model ingestion in S1.

---

## Appendix A: Area and Cost Estimation Examples

The following values are **only for illustrating orders of magnitude and the estimation method**. They use empirical parameters and do not represent datasheet values or calibrated values. Formal conclusions are based on the estimator's calculations under specific implementation library entries.

### A.1 Estimation Parameters (180nm, Empirical Values)

| Parameter | Value | Bound implementation |
|---|---|---|
| ROM macro density | About 0.5 µm²/bit | Hypothetical foundry ROM compiler (availability pending confirmation) |
| Standard-cell logic ROM density | About 3–5 µm²/bit | Highly uncertain; logic simplification during synthesis may change this value significantly; must be calibrated |
| SRAM density (including peripheral circuits) | About 5 µm²/bit | Hypothetical SRAM macro |
| Core area of a 2mm² chip (1mm × 2mm) after deducting the IO ring | About 1.0–1.2 mm² | Depends on the IO library; pending confirmation |
| Single-chip area limit (reticle size) | About 850 mm² | — |
| Sephonchitech unit price | ¥5000/mm², minimum 2mm² | — |

### A.2 Storage Breakdown Calculation

This section only calculates storage area. **Storage area alone cannot determine whether the whole chip is feasible.**

| Item | Calculation | Result |
|---|---|---|
| stories260K weights, 4bit, ROM macro | 1.04 Mbit × 0.5 µm² | About 0.52 mm² |
| stories260K weights, 4bit, standard-cell logic ROM | 1.04 Mbit × 3–5 µm² | About 3.1–5.2 mm² |
| stories260K KV cache, per token | 5 layers × 2 (K, V) × kv_dim 32 × INT8 | 320 bytes |
| KV cache, 64 tokens, on-chip SRAM | 20,480 bytes = 163,840 bit × 5 µm² | About 0.82 mm² |
| KV cache, 512 tokens, on-chip SRAM | 163,840 bytes = 1,310,720 bit × 5 µm² | About 6.55 mm² |

### A.3 Whole-Chip Conclusion Examples (stories260K, 2mm²)

| Scheme | Known parts | Unknown parts | Conclusion |
|---|---|---|---|
| Weights in ROM macro, KV off-chip | Weights 0.52 mm², core area 1.0–1.2 mm² | Compute units, activation buffers, control, interfaces, storage peripheral circuits, routing utilization | **Conditionally feasible**. Preconditions: ① the foundry provides a ROM macro with density no worse than about 0.5 µm²/bit; ② the remaining parts total no more than about 0.5 mm², pending calibration |
| Weights in standard-cell logic ROM, KV off-chip | Weights alone require 3.1–5.2 mm², exceeding the core area | — | **Infeasible** based on empirical values. Before calibration, the formal conclusion is "Insufficient data", and FR-EST-13 must be run |
| Any weight storage method, KV on-chip 64 tokens | KV about 0.82 mm²; together with weights, exceeds the core area | — | **Infeasible** |

### A.4 How Large a Model Fits in 2mm² (Assuming 0.6–0.8 mm² Available for the Weight Area)

| Storage implementation | Bits storable | Parameters at 4bit | Parameters at 2bit / ternary |
|---|---|---|---|
| ROM macro (about 0.5 µm²/bit) | About 1.2–1.6 Mbit | About 300K–400K | About 600K–800K |
| Standard-cell logic ROM (about 3–5 µm²/bit) | About 0.12–0.27 Mbit | About 30K–70K | About 60K–130K |

### A.5 Larger Models (4bit Weights, Weight Storage Only, ROM Macro)

| Model | Weight bits | Weight storage area | Conclusion |
|---|---|---|---|
| 0.5B | About 2 Gbit | About 1,000 mm² | Exceeds the single-chip limit; infeasible at 180nm |
| 8B | About 32 Gbit | About 16,000 mm² | Requires about 19 or more full-size chips |
| 1.5T | About 6 Tbit | About 3,000,000 mm² | Not practically meaningful at 180nm; requires an advanced process plus multiple chips |
