# Vendored Packages

This directory contains source-vendored copies of the [Cordis](https://github.com/cordiverse/cordis) framework and its foundation libraries. They are copied into this monorepo instead of being depended on via npm, so that llmchip-harness fully owns its framework layer: auditable, patchable and pinned. See Microkernel Architecture §5 (ADR-001) for how Cordis is used: only to assemble `harnessd` internals; `@llmchip/kernel-api` must never depend on it.

All vendored packages keep their **upstream npm names** (resolved through Yarn workspaces) and are marked `private: true`; they are never published from this repository. Upstream MIT `LICENSE` files are preserved in each package directory.

## Source

The code is taken from the `vendor/` directory of [deepseek-harness](https://github.com/LaiQuan-conquer/deepseek-harness) at commit `86a402200d95fecef4529d969836f1fe13c6cb0d` (2026-10-15). That fork carries lifecycle and robustness fixes on top of upstream Cordis, and its sources are already Node-ESM friendly (explicit `.ts` relative specifiers, type-only imports marked). The table records the upstream version and commit that fork is based on.

## Manifest

| Directory | npm name | Upstream version | Upstream repo | Upstream commit |
|---|---|---|---|---|
| `cosmokit/` | `cosmokit` | 1.8.1 | https://github.com/deepseek-harness/cosmokit | `16f6fc058ade66e8ac5da0033d35a8d0f279f544` |
| `schemastery/` | `schemastery` | 3.18.0 | https://github.com/deepseek-harness/schemastery (`packages/core`) | `e67cee00ad725bd1534aee930a979ea3eec6f698` |
| `cordis/` | `cordis` | 4.0.0-rc.7 | https://github.com/cordiverse/cordis (`packages/core`) | `56b3d4f725681cf4556c1a8695a709cc3b6eed74` |
| `loader/` | `@cordisjs/plugin-loader` | 1.0.0-rc.5 | https://github.com/cordiverse/cordis (`packages/loader`) | `56b3d4f725681cf4556c1a8695a709cc3b6eed74` |
| `include/` | `@cordisjs/plugin-include` | 1.0.4 | https://github.com/deepseek-harness/cordis (`packages/include`) | `abb0a307cb1d3b0947f455d590cf5ba922d4caa4` |
| `group/` | `@cordisjs/plugin-group` | 1.0.0 | https://github.com/deepseek-harness/cordis (`packages/group`) | `abb0a307cb1d3b0947f455d590cf5ba922d4caa4` |
| `timer/` | `@cordisjs/plugin-timer` | 1.1.2 | https://github.com/deepseek-harness/cordis (`packages/timer`) | `abb0a307cb1d3b0947f455d590cf5ba922d4caa4` |
| `hmr/` | `@cordisjs/plugin-hmr` | 1.0.15 | https://github.com/deepseek-harness/cordis (`packages/hmr`) | `abb0a307cb1d3b0947f455d590cf5ba922d4caa4` |
| `logger-console/` | `@cordisjs/plugin-logger-console` | 1.0.0 | https://github.com/deepseek-harness/cordis (`packages/logger-console`) | `abb0a307cb1d3b0947f455d590cf5ba922d4caa4` |

Third-party dependencies of the vendored packages stay on npm: `@standard-schema/spec`, `js-yaml`, `chokidar`, `picomatch`, `@babel/code-frame`, `supports-color`, and the optional peer `node-addon-require-builtin`.

Intentionally **not** vendored: `reggol`, `@cordisjs/utils`, `@cordisjs/element`, `@cordisjs/unyaml`, `create-cordis`.

## How vendored packages are built and consumed

- Each package is a TypeScript project referenced from the root `tsconfig.build.json` and compiled to its own `lib/`.
- Vendored sources use syntax that Node cannot run directly (namespaces, parameter properties, enums), so their `exports` have **no `development` condition**: consumers, tests and type checking always use `lib/`. `yarn typecheck`, `yarn test` and `yarn build` build them first.
- Their `tsconfig.json` extends the root `tsconfig.base.json` but relaxes the options the upstream code was not written for (`verbatimModuleSyntax`, `erasableSyntaxOnly`, and per package the strictness flags listed there). Do not tighten them by editing vendored code; that would be an unlogged local modification.
- `tests/vendor.spec.ts` at the repository root smoke-tests the built packages.

## Local modifications

Keep this log exhaustive: every divergence from the source commit above must be listed.

**Inherited from deepseek-harness.** The source commit's own `vendor/README.md` lists 24 local modifications relative to upstream. The source changes among them are kept as is: modifications 1, 4, 6–9, 11–15 and 18–23 (for example the HMR i18n removal, `.ts` specifiers, `fiber.ts` lifecycle hardening, Include/Loader robustness fixes, lazy config resolution, volatile config), and the source part of modification 10 (explicitly marked type-only imports). The packaging changes are reverted or replaced as described below: modifications 2, 3, 5, 16, 17, 24, and the Schemastery conditional-exports part of 10.

**Made in this repository:**

1. **Upstream npm names restored** (reverts deepseek-harness modification 17): every `@deepseek-ai/cosmokit`, `@deepseek-ai/schemastery`, `@deepseek-ai/cordis` and `@deepseek-ai/cordis-plugin-<x>` specifier in `src/`, `bin.js` and `README.md` is renamed back to `cosmokit`, `schemastery`, `cordis` and `@cordisjs/plugin-<x>`.
2. **All `package.json` files regenerated** (replaces modification 2): upstream version numbers, `private: true`, ESM-only `exports` into `lib/` (Schemastery's dual CJS build and the `./src/*` export are dropped; Logger Console keeps its `node`/browser split), no publish metadata. Dependencies within the vendored set use `workspace:*`; other dependency and peer-dependency ranges are kept. HMR additionally declares its imports of `@cordisjs/plugin-loader` and `@cordisjs/plugin-include` as peer dependencies, which the source manifest omitted.
3. **All `tsconfig.json` files regenerated** (replaces modification 3): extend this repository's `tsconfig.base.json`, emit to `lib/`, keep the source commit's per-package strictness relaxations and project references, and additionally set `verbatimModuleSyntax` and `erasableSyntaxOnly` to `false`.
4. **Files not copied:** the `tsdown.config.ts` build overrides of `loader`, `logger-console` and `schemastery` (modification 5) and `timer/locale/*.json` (modification 24, deepseek-harness settings metadata), plus deepseek-harness's `vendor/AGENTS.md`.

## Sync procedure

To update a vendored package:

1. Note the source commit (deepseek-harness or upstream) you are syncing from.
2. Copy the package's `src/` (and `bin.js`, `README.md`, `LICENSE` if changed) over the vendored directory.
3. Re-apply the local modifications listed above, or drop them if no longer needed; update the log either way.
4. Update the source and manifest tables.
5. Run `yarn install && yarn verify` at the repository root.
