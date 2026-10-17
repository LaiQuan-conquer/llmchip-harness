# llmchip-harness

A private-deployment workbench between open-source LLMs and the foundry: it takes a model's weights and produces a foundry-ready tapeout package for a chip with those weights hardwired into silicon. Research and development inside the workbench is done by AI agents, with humans approving at key gates.

The project is at an early stage: the kernel (event log, artifact store, scheduler, verdicts and approvals), the plugin protocol and the feasibility estimator work; the chip design flow itself is not implemented yet.

## Documents

- [Coding Harness MVP Requirement Analysis](<Coding Harness MVP Requirement Analysis.md>) ([中文](<Coding Harness MVP Requirement Analysis.zh.md>)) — what the product must do.
- [Microkernel Architecture](<Microkernel Architecture.md>) ([中文](<Microkernel Architecture.zh.md>)) — how the system is organized and extended.
- [AGENTS.md](AGENTS.md) — development process, codebase conventions and agent instructions.

## Development

Requires Node.js 24 (see `.nvmrc`) with corepack enabled, and [uv](https://docs.astral.sh/uv/) for the Python parts.

```sh
corepack enable
yarn install
yarn setup:python       # Python 3.12 environment for Python plugins
yarn verify             # workspace and generated-file checks, Python checks, typecheck, tests, build

yarn harnessd serve &   # start the kernel (home: $LLMCHIP_HOME or ~/.llmchip)
yarn llmchip estimate --model stories260K
```

See [AGENTS.md](AGENTS.md#engineering) for the repository layout and all commands.
