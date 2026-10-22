# llmchip-harness

A private-deployment workbench between open-source LLMs and the foundry: it takes a model's weights and produces a foundry-ready tapeout package for a chip with those weights hardwired into silicon. Research and development inside the workbench is done by AI agents, with humans approving at key gates.

Status: the kernel, the plugin protocol, managed agents, the Web UI and the flow from S0 to S8 on GF180MCU work, with an untrained proxy model as the design subject. Not built yet: the desktop shell, the installer and S9 (post-silicon). Details are in the [implementation guide](docs/implementation.md#1-current-implementation-scope).

## Documents

- [Coding Harness MVP Requirement Analysis](<Coding Harness MVP Requirement Analysis.md>) ([中文](<Coding Harness MVP Requirement Analysis.zh.md>)): what the product must do.
- [Microkernel Architecture](<Microkernel Architecture.md>) ([中文](<Microkernel Architecture.zh.md>)): how the system is organized and extended.
- [Current Implementation and Extension Guide](docs/implementation.md) ([中文](docs/implementation.zh.md)): what the code does today, and how to extend and verify it.
- [AGENTS.md](AGENTS.md): development process, codebase conventions and agent instructions.

## Setup

Requires Node.js 24 (see `.nvmrc`) with corepack enabled, and [uv](https://docs.astral.sh/uv/) on `PATH` for the Python parts. The container plugins (simulation, physical flow) also need Podman and the pinned EDA image, which is about 13 GB; without it, the tests that need it are skipped.

```sh
corepack enable
yarn install
yarn setup:python       # Python 3.12 environment for the Python plugins
yarn build              # vendored Cordis and the Web UI are used from their build output
```

## Run

```sh
yarn harnessd serve &                       # start the kernel (home: $LLMCHIP_HOME or ~/.llmchip)
yarn llmchip estimate --model stories260K   # feasibility, area and cost estimate
yarn llmchip run list                       # the run behind it, with its outputs
yarn demo                                   # run the demo project through the whole flow (S0-S8)
yarn demo --arch spatial                    # the same with the spatial engine (the model as circuits)
yarn demo --model /path/to/Qwen3-0.6B       # import a downloaded model: census, estimate, graph recovery (S0-S1), int8 analysis (S2)
yarn ui                                     # open the Web UI; the Flow page shows every stage
yarn llmchip kernel stop
```

`yarn llmchip --help` lists every command.

## Verify

```sh
yarn verify             # workspace and generated-file checks, Python checks, typecheck, build, tests
```

`yarn verify` must pass before every commit. The full S0–S8 flow test is opt-in: `LLMCHIP_FULL_FLOW=1 yarn test tests/m4-flow.spec.ts`. See [implementation guide §6](docs/implementation.md#6-verification) for what each test proves and when it runs, and [AGENTS.md](AGENTS.md#engineering) for the repository layout and all commands.
