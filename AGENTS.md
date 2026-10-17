# AGENTS.md

This is the monorepo for **llmchip-harness**: a private-deployment workbench that takes the weights of an open-source LLM and produces a foundry-ready tapeout package for a chip with the model's weights hardwired into silicon. All research and development work in the workbench is done by AI agents; humans approve at gates.

This file is the **single entry point** for every agent working on this repository. Read it before doing anything else.

## Design Documents

- [Coding Harness MVP Requirement Analysis](<Coding Harness MVP Requirement Analysis.md>) ([中文](<Coding Harness MVP Requirement Analysis.zh.md>)) — what the product must do: requirements, priorities, acceptance criteria.
- [Microkernel Architecture](<Microkernel Architecture.md>) ([中文](<Microkernel Architecture.zh.md>)) — how the system is organized and extended: kernel components, plugins, protocols, ADRs.

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
- A lower-domain document must not redefine a higher-domain topic. For example, the architecture must not change acceptance criteria.
- **If you find a conflict, do not pick a side.** Record it as an open issue, report it to the owner, and pause work within the scope of the conflict.

Key documents exist in both English (`<name>.md`) and Chinese (`<name>.zh.md`). **The Chinese version is authoritative.** Every change must update both versions in the same commit, and both must carry the same version number. If you find a mismatch, follow the Chinese version and report the mismatch as a conflict.

## Iteration loop

Every change goes through all six steps. Skipping a step is not allowed.

1. **Define the goal.** Link the task to a requirement ID (for example `FR-EST-07`) or a charter goal. A task with no goal does not start.
2. **Bound the structure.** Declare which modules and files you will touch and which you will not. Stay inside that scope.
3. **Implement in small steps.** One verifiable change per change set, independently revertible.
4. **Review.** A different agent reviews the change against the requirements, the architecture, and this file. Never review your own change.
5. **Verify for real.** Run `yarn verify` and any other relevant tools, and check the results. Claiming completion is not verification. A zero exit code is not a pass (DP-8); "nothing was checked" is not "no violations" (DP-10).
6. **Update constraints.** Turn what you learned from failures and incidents into proposed constraints in this file or in the relevant playbook. The owner approves changes to hard constraints. Anything that affects an acceptance result is a hard constraint; when in doubt, treat a constraint as hard.

## Architecture

The kernel and UI are TypeScript. The desktop shell is Tauri. Compute-heavy plugins (quantization, bit-accurate models) are Python. EDA tools run in containers. Plugins in any language talk to the kernel through one unified protocol.

**Cordis** is the candidate substrate for the in-process TypeScript plugin layer (ADR-001). Whether to adopt it is still open, to be settled by a spike in milestone M1. Do not vendor Cordis or build on its APIs until that ADR is accepted.

## Engineering

### Layout

| Path | Contents |
|---|---|
| `packages/<name>/` | One TypeScript workspace package, named `@llmchip/<name>` |
| `packages/<name>/src/` | Source; `src/index.ts` is the package entry point |
| `packages/<name>/tests/` | Vitest tests, named `*.spec.ts` |
| `packages/<name>/lib/` | Build output (ignored by git) |
| `scripts/` | Repository tooling scripts |

Current packages:

- `kernel-api` — language-neutral contracts: kernel API, plugin protocol, artifact types. Must not depend on Cordis (AP-6).
- `kernel` — `harnessd`, the kernel daemon. Only `--version` and `--help` are implemented so far.

Planned directories (`apps/`, `python/`, `plugins/`, `images/`, `vendor/`) are listed in architecture §14. Create them only when the corresponding work starts.

### Toolchain

- Node.js 24 LTS (see `.nvmrc`) and Yarn 4 via corepack (`packageManager` in `package.json`).
- TypeScript with project references. Node runs `.ts` sources directly; there is no separate transpiler.
- Each package exports `"."` with the conditions `development` → `./src/index.ts`, `types` → `./lib/index.d.ts`, `default` → `./lib/index.js`. Type checking, tests and `yarn harnessd` resolve the `development` condition, so they work without a build.
- Relative imports inside a package use the `.ts` extension; the build rewrites them to `.js`.

### Commands

| Command | Purpose |
|---|---|
| `yarn install` | Install dependencies |
| `yarn check:workspace` | Check that tooling configuration matches the packages that exist |
| `yarn typecheck` | Type-check sources, tests and scripts |
| `yarn test` | Run all tests |
| `yarn build` | Compile every package to `lib/` |
| `yarn clean` | Remove build output |
| `yarn harnessd` | Run the kernel daemon from source |
| `yarn verify` | Run check:workspace, typecheck, test and build in order |

`yarn verify` must pass before every commit.

### Adding a package

1. Create `packages/<name>/` with `package.json` (name `@llmchip/<name>`, `"private": true`, `"type": "module"`, the `exports` shape above), `tsconfig.json` extending `../../tsconfig.base.json`, and `src/index.ts`.
2. Add `{ "path": "./packages/<name>" }` to `tsconfig.build.json`.
3. For every `@llmchip/*` package imported from `src/`, add it to `dependencies` as `"workspace:*"` and add a matching entry to `references` in the package's `tsconfig.json`.
4. Run `yarn install`, then `yarn verify`. `yarn check:workspace` reports anything missed in steps 1–3.

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
