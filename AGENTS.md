# AGENTS.md

This is the monorepo for **llmchip-harness**: a private-deployment workbench that takes the weights of an open-source LLM and produces a foundry-ready tapeout package for a chip with the model's weights hardwired into silicon. All research and development work in the workbench is done by AI agents; humans approve at gates.

This file is the **single entry point** for every agent working on this repository. Read it before doing anything else.

## Design Documents

- [Coding Harness MVP Requirement Analysis](<Coding Harness MVP Requirement Analysis.md>) ([中文](<Coding Harness MVP Requirement Analysis.zh.md>)) — what the product must do: requirements, priorities, acceptance criteria.
- [Microkernel Architecture](<Microkernel Architecture.md>) ([中文](<Microkernel Architecture.zh.md>)) — how the system is organized and extended: kernel components, plugins, protocols, ADRs.
- [Current Implementation and Extension Guide](docs/implementation.md) ([中文](docs/implementation.zh.md)) — what the code does today: implementation scope, modules and dependencies, a traced run path, ownership of data and resources, extension recipes with real examples, verification, limitations and open conflicts. **Read it before changing anything under `packages/`, `plugins/` or `apps/`.**

## Authoritative documents

Authority is divided by domain (see requirements §0.3):

| Domain | Authoritative document |
|---|---|
| What the product does, priorities, acceptance criteria | Coding Harness MVP Requirement Analysis |
| System structure, module boundaries, interfaces, protocols | Microkernel Architecture |
| Development process, coding constraints, verification rules, prohibitions | this file |
| Goals and constraints of a single chip project | that project's `CHARTER.md` |

Rules:

- Recorded owner decisions (ADRs) override all documents above; the affected documents must then be updated.
- Files under `drafts/` are local discussion material, are not committed, and carry no authority.
- The implementation guide (`docs/implementation.md`) is descriptive and carries no authority. If it disagrees with the code, the guide is out of date: fix it. Known conflicts between the code and the requirements or architecture are listed in its §7.3 and stay open until the owner decides. Do not build on either side of them.
- A lower-domain document must not redefine a higher-domain topic. For example, the architecture must not change acceptance criteria.
- **If you find a conflict, do not pick a side.** Record it as an open issue, report it to the owner, and pause work within the scope of the conflict.

Key documents exist in both English (`<name>.md`) and Chinese (`<name>.zh.md`). **The Chinese version is authoritative.** Every change must update both versions in the same commit, and both must carry the same version number. If you find a mismatch, follow the Chinese version and report the mismatch as a conflict.

## Iteration loop

Every change goes through all six steps. Skipping a step is not allowed.

1. **Define the goal.** Link the task to a requirement ID (for example `FR-EST-07`) or a charter goal. A task with no goal does not start.
2. **Bound the structure.** Declare which modules and files you will touch and which you will not. Stay inside that scope.
3. **Implement in small steps.** One verifiable change per change set, independently revertible.
4. **Review.** A different agent reviews the change against the requirements, the architecture, and this file. Never review your own change.
5. **Verify for real.** Run `yarn verify` and any other relevant tools, and check the results. Claiming completion is not verification. A zero exit code is not a pass (DP-8); "nothing was checked" is not "no violations" (DP-10). Tests that need a container image or `LLMCHIP_FULL_FLOW=1` skip visibly when their condition is not met. Report which tests were skipped, and run the ones your change touches (implementation guide §6).
6. **Update constraints.** Turn what you learned from failures and incidents into proposed constraints in this file or in the relevant playbook. The owner approves changes to hard constraints. Anything that affects an acceptance result is a hard constraint; when in doubt, treat a constraint as hard. Update the implementation guide in the same change set when the change alters what it describes (see "Implementation guide" below).

## Architecture

The kernel and UI are TypeScript. The desktop shell will be Tauri (ADR-002; not built yet, the Web UI runs in a browser). Compute-heavy plugins (quantization, bit-accurate models) are Python. EDA tools run in containers. Plugins in any language talk to the kernel through one unified protocol.

**Cordis** and its companion libraries are vendored as source under `vendor/` (ADR-001 item 3, owner decision). Cordis may only be used to assemble `harnessd` internals and in-kernel system plugins. `@llmchip/kernel-api` and the plugin protocol must never depend on it (AP-6), and out-of-process plugins must not see it. ADR-001 is accepted with the plugin host guards of architecture §5 item 6: load in-kernel plugins only through `PluginHost`, declare every Cordis service a plugin provides (`provides`) or needs (`inject`), and never put Cordis events on a critical path. Keep Cordis usage behind kernel-owned interfaces so it can be replaced.

## Engineering

### Layout

| Path | Contents |
|---|---|
| `packages/<name>/` | One TypeScript workspace package, named `@llmchip/<name>` |
| `apps/web/` | Web UI (React, Vite), served by harnessd; texts in `src/i18n.ts` in Chinese and English |
| `packages/<name>/src/` | Source; `src/index.ts` is the package entry point |
| `packages/<name>/tests/` | Vitest tests, named `*.spec.ts` |
| `packages/<name>/lib/` | Build output (ignored by git) |
| `vendor/<name>/` | Vendored third-party source with its upstream npm name; see `vendor/README.md` |
| `plugins/<id>/` | Out-of-process and data plugins, each with an `llmchip-plugin.json` manifest; bundled with the workbench |
| `python/<name>/` | Python packages of the uv workspace, such as the plugin SDK |
| `tests/` | Repository-level and end-to-end tests; `tests/fixtures/plugins/` holds test-only plugins, `tests/fixtures/models/` the stories260K comparison model |
| `scripts/` | Repository tooling scripts |
| `docs/` | The implementation guide (`implementation.md`, `implementation.zh.md`) |

Current packages (responsibilities, entry points and allowed dependencies: implementation guide §2):

- `kernel-api` — language-neutral contracts: kernel API, plugin protocol, artifact types. Must not depend on Cordis (AP-6). The source of truth is the JSON Schema in `packages/kernel-api/schemas/`; `src/generated/` is produced from it by `yarn gen:schemas` and must not be edited by hand.
- `kernel` — `harnessd`, the kernel daemon: K1–K10, the API server (IF-1), the in-kernel plugin host, and the agent runtime with its LLM providers.
- `plugin-sdk` — TypeScript SDK for out-of-process plugins (IF-2).
- `client` — kernel API client for Node.js and browsers.
- `cli` — the `llmchip` command-line client, including `llmchip mcp`.

Python (uv workspace, root `pyproject.toml`): `python/llmchip-sdk`, the Python SDK for out-of-process plugins (IF-2).

Bundled plugins (what each produces: implementation guide §2.4): `estimator`, `process-library` (data), `flow-default` (data), `model-llama`, `numeric`, `arch-taalas`, `rtl-taalas`, `packager` (Python), and `sim-verilator`, `eda-gf180` (containers).

Vendored packages: `cordis`, `cosmokit`, `schemastery`, and the Cordis plugins `@cordisjs/plugin-{loader,include,group,timer,hmr,logger-console}`.

Planned directories still to be created: `apps/desktop/` and `images/` (architecture §14), and `docs/adr/` (architecture §15). Create them only when the corresponding work starts.

### Implementation guide

[docs/implementation.md](docs/implementation.md) holds the detail of what is implemented and how to extend it. This file keeps only the rules.

- Update the guide, both languages, in the same commit when your change does any of these:
  - changes a module's responsibility or dependency direction (§2);
  - changes the traced run path or its failure handling (§3);
  - changes who writes, owns or releases a resource (§4);
  - adds or changes an extension mechanism (§5);
  - changes a verification command or the conditions under which a test runs or skips (§6);
  - opens or closes a limitation or conflict (§7).

  Update "Describes" in its header to the commit it now matches.
- Every extension recipe in the guide names a real example in the code and the test that proves it. When you add a recipe, add both.
- Commands written in the guide or the README must have been run. Record the observed result, and the date for timings.

### Kernel and plugin rules

- Dependency direction:
  - Front ends (CLI, Web UI, MCP bridge) reach the kernel only through `@llmchip/client`.
  - Plugins never import the kernel; they speak IF-2 only.
  - Among production sources, only `packages/kernel/src/plugin-host.ts` imports Cordis.
- Kernel state changes only through `EventStore.append` or `EventStore.transaction`. Every other table belongs to a `Projection` and is written only in its `apply`. Bump a projection's `version` whenever its `setup` or `apply` changes.
- Every kernel API method and event payload is defined in the kernel schema first, then in `KernelMethods`/`KERNEL_METHOD_DEFS` or `KernelEventPayloads`/`KERNEL_EVENT_PAYLOAD_DEFS`. Owner-only is the default; add a method to `EXTERNAL_METHODS` in `security/policy.ts` only when external clients may call it.
- A gate check must consume the gate's slotted artifacts as run inputs, so that its verdict counts as evidence for exactly that snapshot. Each signoff check runs on exactly the artifacts it judges.
- Change a check definition's `version` whenever its standards change; that is how a check that passes only after a standard change is detected (AC-M1-13).
- Data in `process-library` records its source and confidence for every value; empirical values stay empirical until calibrated.

### Toolchain

- Node.js 24 LTS (see `.nvmrc`) and Yarn 4 via corepack (`packageManager` in `package.json`).
- TypeScript with project references. Node runs `.ts` sources directly; there is no separate transpiler.
- Each package exports `"."` with the conditions `development` → `./src/index.ts`, `types` → `./lib/index.d.ts`, `default` → `./lib/index.js`. Type checking, tests and `yarn harnessd` resolve the `development` condition, so they work without a build.
- Relative imports inside a package use the `.ts` extension; the build rewrites them to `.js`.
- Python 3.12 and its tools are managed by [uv](https://docs.astral.sh/uv/) (`uv` must be on `PATH`). `yarn setup:python` creates `.venv/` from `uv.lock`; the kernel uses `.venv/bin/python` for Python plugins when it exists. Lint and format with ruff, test with pytest.
- Vendored packages use syntax Node cannot run directly, so they export only `types` and `default`, both into `lib/`. Everything that imports them (tests, type checking, `harnessd`) uses their build output; `yarn typecheck` and `yarn test` build all project references first. After changing vendored code, run `yarn build` before `yarn harnessd`.

### Commands

| Command | Purpose |
|---|---|
| `yarn install` | Install Node.js dependencies |
| `yarn setup:python` | Create the Python environment (`.venv/`) from `uv.lock` |
| `yarn check:python` | Lint, format-check and test the Python code |
| `yarn check:workspace` | Check that tooling configuration matches the packages that exist |
| `yarn gen:schemas` | Regenerate `kernel-api` types from its JSON Schema |
| `yarn check:generated` | Check that the generated `kernel-api` files are up to date |
| `yarn typecheck` | Build project references, then type-check sources, tests and scripts |
| `yarn test` | Build project references, then run all tests |
| `yarn build` | Compile every package to `lib/` and build the Web UI to `apps/web/dist/` |
| `yarn ui` | Open the Web UI of the running kernel in the browser (`llmchip ui`) |
| `yarn demo [--until G0..G4]` | Drive project `demo` through the default flow on the running kernel, for the Web UI's Flow page (S5 onwards needs the EDA image) |
| `yarn demo --model PATH` | Start a project from a downloaded model on this machine: import, census, estimate, graph recovery, L0, comparison with transformers, and an int8 numeric analysis (S2) |
| `yarn clean` | Remove build output |
| `yarn harnessd serve [--home DIR]` | Run the kernel daemon from source (home defaults to `$LLMCHIP_HOME` or `~/.llmchip`) |
| `yarn llmchip <command>` | Run the CLI from source against a running kernel; `yarn llmchip --help` lists commands |
| `yarn verify` | Run check:workspace, check:generated, check:python, typecheck, build and test in order |

`yarn verify` must pass before every commit.

### Adding a package

1. Create `packages/<name>/` with `package.json` (name `@llmchip/<name>`, `"private": true`, `"type": "module"`, the `exports` shape above), `tsconfig.json` extending `../../tsconfig.base.json`, and `src/index.ts`.
2. Add `{ "path": "./packages/<name>" }` to `tsconfig.build.json`.
3. For every `@llmchip/*` package imported from `src/`, add it to `dependencies` as `"workspace:*"` and add a matching entry to `references` in the package's `tsconfig.json`.
4. Run `yarn install`, then `yarn verify`. `yarn check:workspace` reports anything missed in steps 1–3.

A package that imports a vendored package follows the same rules: declare it as `"workspace:*"` and add a reference to `../../vendor/<dir>`.

### LLM providers and agents

- Providers are configured in `<home>/config/config.json` under `llm.providers` (schema `LlmProviderConfig`): `openai-compatible` (DeepSeek, GPT, local OpenAI-compatible servers), `anthropic`, or `scripted` (deterministic, for tests and offline demos). Set `location` to `local` only for models running on this machine; only local providers may receive NDA data.
- Store API keys with `llmchip secret set <name>` and reference them by `apiKeySecret`. Never put keys in configuration files or code.
- Node's `fetch` ignores proxy environment variables; on a machine behind a proxy, start harnessd with `NODE_USE_ENV_PROXY=1`.
- Tests use scripted providers. Real-provider behavior is covered by adapter tests against local mock servers (`tests/providers.spec.ts`).
- `llmchip mcp` serves MCP on stdio for external agents (Claude Code, Codex, ...). It uses the kernel's external token: read-only plus starting runs and tasks; NDA content is refused.

### Web UI

- harnessd serves `apps/web/dist` at `/` without authentication (it holds no data); the API needs the token, which `llmchip ui` passes in the URL fragment.
- Every visible text goes through `t('key')` with the key present in both catalogs of `apps/web/src/i18n.ts`; kernel messages are rendered from their codes. `tests/web-ui.spec.ts` enforces both, and renders every page in both languages in headless Chromium (`npx playwright-core install chromium` once; the browser test is skipped without it).
- Agent loops and other long work must yield to the event loop; `tests/ui-latency.spec.ts` checks UI queries stay fast under load.

### Containers

- `runtime: container` plugins run under Podman (`podman run -i`, IF-2 over stdio) with no network, read-only inputs and plugin directory, and only the run workspace writable. Pin `entry.image` by digest.
- The EDA image is large (about 13 GB). Pull it once with `podman pull <entry.image of plugins/eda-gf180>`; tests that need an image are skipped, visibly, when it is absent.
- The full S0–S8 flow test (`tests/m4-flow.spec.ts`) runs the physical tools for about 40 minutes (39 min measured on 2026-10-19) and is opt-in; it also needs the EDA image: `LLMCHIP_FULL_FLOW=1 yarn test tests/m4-flow.spec.ts`. Run it after any change to the RTL generator, `eda-gf180`, `packager` or the signoff checks.
- In WSL, Podman's default container networking does not work; that is irrelevant because plugin containers never get a network.

### Adding a plugin

The full recipe, with a worked example and the tests to copy, is in implementation guide §5.1. For checks and flow packs, see §5.2.

1. Create `plugins/<id>/llmchip-plugin.json` (schema `PluginManifest` in `packages/kernel-api/schemas/kernel.schema.json`). Declare every handler with its inputs, outputs and a JSON Schema for its params; the kernel validates them before a run is queued.
2. Python plugins are uv workspace members: add `plugins/<id>` to `[tool.uv.workspace].members` and to the root dependencies in `pyproject.toml`, then run `uv lock` and `yarn setup:python`.
3. Handlers return outputs, metrics and a parse status, never verdicts (AP-7). Judging is done by check definitions in a flow pack.
4. Test the plugin directly (pytest or Vitest) and end to end through the kernel (`tests/`).

### Generated hardware

- Never edit a generated `rtl.bundle`; change the generator. The weight mapping rule must stay reversible; the generator checks the round trip (FR-RTL-02).
- Every register of generated RTL has a reset value: synthesized logic is X-pessimistic in four-state gate-level simulation, and un-reset control registers turn the whole netlist X.
- Gate-level simulation of a routed netlist runs on an event-driven simulator (Icarus Verilog) with the PDK's cell models; Verilator races on the PDK's sequential UDPs behind clock-tree buffers.
- Do not call Verilog functions with local variables inside clocked blocks of generated RTL: synthesis turns the locals into registers.

### Vendored code

- Treat `vendor/` as upstream code. Change it only when necessary, and record every change in the "Local modifications" log of `vendor/README.md` in the same commit.
- Do not reformat vendored code, tighten its compiler options, or rename its packages.
- To update or add a vendored package, follow the sync procedure in `vendor/README.md` and update its manifest table; `yarn check:workspace` requires a manifest row and an upstream `LICENSE` for every vendored package.

## Prohibitions

- Do not delete or overwrite files you did not create. Do not run destructive git operations (force push, hard reset, history rewrite) without explicit owner approval.
- Do not commit secrets, API keys, PDK files, foundry library files, or any NDA-labeled material. Never send such material to a cloud LLM.
- Do not commit machine-specific settings such as proxy addresses; put them in your user-level configuration.
- Do not hand-edit generated outputs. Change the generator, template, or configuration instead (DP-3).
- Do not weaken a P0 requirement, an acceptance criterion, or a verification check in order to make something pass.

## Language conventions

- File names, code, identifiers, and commit messages are in English.
- Key design documents come in two versions: `<name>.md` (English) and `<name>.zh.md` (Chinese, authoritative).
- The UI supports Chinese and English, switchable by the user.
