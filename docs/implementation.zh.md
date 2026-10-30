# 当前实现与扩展指南

[English](implementation.md) | 中文

| 项目 | 内容 |
|---|---|
| 文档版本 | v0.5 |
| 日期 | 2026-10-30 |
| 对应代码 | 2026-10-30 的 B1/B2（提交 `bbb3874`）：RTL 的逐相位调度 `tile_layer`、定向测试与 `ver.directed`；此前为 2026-10-30 的流程包接线修复（提交 `7232a39`），以及决策记录的约定（提交 `f0a4945`；`docs/adr/`、ADR-012、AGENTS.md「决策记录」）：`spec.design` 由自己的 `verify_spec` 运行判定（tile 的设计节点不再挂检查），tile 的等价检查 id 为 `rtl.tile.equivalence`；加载器拒绝节点判定其他处理器检查的流程包，也不登记被拒流程包的任何检查。此前，R2b：Qwen2.5-0.5B-Instruct 的 SPEC 步骤：快速 L2（GPU，与参考 L2 逐位一致）与 KV 缓存、校准量化（GPTQ）、答错回溯诊断、开源基准评测（快速集与完整集）与六步流程包 `flow-six`；R1（提交 `11d0dcd`）：阶段流水线（每一步的“运行研发流程”与“运行全部测试”）、按 CPU/内存/GPU 调度、运行检查点与续跑；R0（提交 `d771bb9`，负责人决定，2026-10-24）：取消审批门、豁免、导出与 `packager`，流程为 S0–S7；在此之前的提交 `9434f86`：M4 的 `9cc76a0`，以及演示流程页面、`yarn demo`、模型导入与图还原、S2 的数值方案、S3 的精确速度与微架构选项，到 S5 为止的空间引擎 `taalas-spatial/1`，以及虚拟芯片测试环境 `llmchip-vchip`：独立运行，也作为内核插件 `vchip` 并有 Web UI 页面（设计中的 L4） |
| 上游文档 | [需求分析](<../Coding Harness MVP Requirement Analysis.zh.md>) v0.7、[微内核架构](<../Microkernel Architecture.zh.md>) v0.12 |

本文说明**代码现在实际做了什么、怎样扩展**。它是描述性文档，不具权威性：产品必须做什么由需求文档规定，系统应如何组织由架构文档规定。

- 本指南与代码不一致时，以代码为准，说明本指南过时，应修正本指南。
- 本指南的两个语言版本之间，与所有关键文档一样，以中文版为准。
- 代码与上游文档不一致时，属于冲突：记入 §7.3 并上报（见 AGENTS.md「Authoritative documents」），不得单方面修改任何一方来消解。

源码引用写文件和符号（如 [runs/scheduler.ts](../packages/kernel/src/runs/scheduler.ts) 中的 `Scheduler.execute`），不写行号，以免编辑后失效。

---

## 1. 当前实现范围

### 1.1 里程碑

| 里程碑（需求 §11.4） | 提交 | 代码中的状态 | 证明方式 |
|---|---|---|---|
| M1a 内核基础 | `826168b` | 完成 | `packages/kernel/tests/*`、`tests/kernel-api.spec.ts`、`tests/recovery.spec.ts` |
| M1b 执行与判定 | `62e7a88` | 完成 | `tests/runs.spec.ts`、`tests/workflow.spec.ts`、`tests/estimator.spec.ts` |
| M1c 智能体与安全 | `427d16e` | 完成；智能体只在内核内运行 | `tests/agents.spec.ts`、`tests/mcp.spec.ts`、`tests/providers.spec.ts` |
| M1d Web UI 部分 | `851590d` | 只有浏览器 UI；没有 Tauri 外壳、安装器和首次启动引导 | `tests/web-ui.spec.ts`、`tests/ui-latency.spec.ts` |
| M1.5 存储标定 | `3691b26` | 标准单元 ROM 与 SRAM 512×8 完成；OpenRAM 的结论为不可用 | `tests/calibration.spec.ts`（需 EDA 镜像） |
| M2 模型到规格（S1–S3） | `9284a99` | 在 stories260K 与占位模型上完成；没有训练好的参考模型 | `tests/m2-flow.spec.ts` |
| M3 RTL 与验证（S4–S5） | `3842361` | 在占位模型上完成 | `tests/m3-flow.spec.ts`（需 EDA 镜像） |
| M4 物理实现与签核（S6–S7） | `9cc76a0` | 在占位模型上完成，芯片 1.2×2 mm；当时的 S8 流片包已随 R0 删除 | `tests/m4-flow.spec.ts`（须显式开启，实测 39 分钟） |
| M5 迁移到流片工艺、M6 回片测试 | — | 已删除（负责人决定，2026-10-24：不流片）。迁移影响评估（FR-MIG）仍可用 | `tests/m4-kernel.spec.ts`、`plugins/estimator/tests/test_migration.py` |
| R0 取消审批门与豁免 | `d771bb9` | 完成：没有审批门、快照、审批、放行、豁免与导出；删除 `packager`；需求 v0.7、架构 v0.11 | `tests/workflow.spec.ts`、`tests/m4-kernel.spec.ts`、各流程测试 |
| R1 流水线与资源 | `11d0dcd` | 完成：阶段流水线（`pipelines.*`、CLI `pipeline`、演示流程页的两个入口与流水线面板），`flow-default` 为 S0–S7 声明研发流水线与必需测试；按 CPU、内存、GPU 调度；运行检查点与续跑（IF-2 `checkpoint`、输入 `resume`）；架构 v0.12 | `tests/pipelines.spec.ts`、`tests/pipelines-flow.spec.ts`、`tests/resources.spec.ts`、`tests/checkpoint.spec.ts` |
| R2a 快速 L2 与基准 | 本提交 | 部分完成（R2 的第一部分）：计算图 L2 的 KV 缓存；GPU 上的快速 L2（`TorchL2`），逐位等于参考 L2；七个开源基准在 L0 与 L2 上的快速集和完整集，代码题在无网络容器中判分；配对 bootstrap 比较；六步流程包 `flow-six` 的 SPEC 步骤（研发流水线与必需测试）。数值规格暂用逐元素舍入（RTN）的 W4，校准量化、KV int8、回溯诊断、`taalas-tile/1` 与 sky130/IHP 档案在 R2 的后续部分 | `plugins/numeric/tests/test_l2_torch.py`、`test_bench.py`、`test_benchcode.py`、`tests/qwen-spec.spec.ts`（须显式开启） |

因此 M1 整体**未**通过验收：缺少 AC-M1-01（安装器）和桌面外壳，见 §7。

### 1.2 已实现

| 领域 | 能做什么 | 入口 |
|---|---|---|
| 内核守护进程 | 启动、单实例锁、恢复、有序关闭 | `harnessd serve`、[kernel.ts](../packages/kernel/src/kernel.ts) `Kernel.start` |
| K1 事件库 | 追加式 SQLite 日志、哈希链、投影、重放 | [events/store.ts](../packages/kernel/src/events/store.ts) |
| K2 产物库 | 内容寻址、只读对象、血缘、标签传播 | [artifacts/store.ts](../packages/kernel/src/artifacts/store.ts) |
| K3/K6 工作流 | 项目、槽位、分支、阶段（只有准入检查）、判定、迁移记录 | [workflow/engine.ts](../packages/kernel/src/workflow/engine.ts) |
| K3 流水线 | 每一步的研发流水线与必需测试：依赖、失败与跳过、复用、取消、改正后继续、内核重启后继续 | [workflow/pipelines.ts](../packages/kernel/src/workflow/pipelines.ts) |
| K4 调度器 | 持久队列、按 CPU/内存/GPU 调度、进程与容器执行、超时、取消、重试（从检查点续跑）、暂停、运行检查点 | [runs/scheduler.ts](../packages/kernel/src/runs/scheduler.ts) |
| K5 API 策略 | 区分所有者令牌与外部令牌；方法白名单 | [security/policy.ts](../packages/kernel/src/security/policy.ts) |
| K8 出口网关 | 标签、目的地上限、NDA 指纹、去密规则、审计 | [security/egress.ts](../packages/kernel/src/security/egress.ts) |
| K9 插件注册表 | 清单扫描、校验、数据导入 | [plugins/registry.ts](../packages/kernel/src/plugins/registry.ts) |
| K10 配置与密钥 | 分层配置、AES-256-GCM 密钥文件、脱敏 | [config/](../packages/kernel/src/config/) |
| 智能体运行时 | 托管智能体：工具检查、变更集、接手包、死循环与预算停止 | [agents/runtime.ts](../packages/kernel/src/agents/runtime.ts)、[agents/providers.ts](../packages/kernel/src/agents/providers.ts) |
| IF-1 内核 API | WebSocket `/v1/rpc` 上的 JSON-RPC 2.0，以及 HTTP `/v1/artifacts` | [server.ts](../packages/kernel/src/server.ts)、[client](../packages/client/src/index.ts) |
| IF-2 插件协议 | stdio 上的 JSON-RPC 2.0，每行一条消息；通知 `progress`、`log`、`checkpoint` | [plugins/process.ts](../packages/kernel/src/plugins/process.ts)、两个 SDK |
| IF-3 工具容器 | Podman，无网络，输入与插件目录只读 | `Scheduler.containerCommand` |
| CLI 与 MCP | `llmchip` 命令（含 `pipeline plan/start/list/show/cancel/resume`）、通用 `call`、stdio 上的 MCP | [cli/src/main.ts](../packages/cli/src/main.ts)、[cli/src/mcp.ts](../packages/cli/src/mcp.ts) |
| Web UI | 九个页面，中英文。默认页面“演示流程”用通俗语言展示项目的 S0–S7 路径、结果和检查；全部阶段与每个阶段各有“运行研发流程”和“运行全部测试”两个入口，流水线面板列出每次运行的节点、运行、判定与原因 | [apps/web/src](../apps/web/src) |
| 领域流程 | 内置插件跑通 S0–S7，每个阶段由其检查判定（见 §2.4 的表） | [plugins/](../plugins) |
| 模型质量（开源基准） | 七个开源基准（MMLU、CMMLU、C-Eval、GSM8K、IFEval、HumanEval+、MBPP+）的快速集（900 题，固定种子 20261024）与完整集，在 L0（transformers，bf16，GPU）与 L2（快速 L2）上作答，按各基准的规则判对错；代码题只在无网络、只读的容器中运行；L2 对 L0 每个基准的下降与配对 bootstrap 95% 区间，由每个基准一项检查判定 | [bench.py](../plugins/numeric/src/llmchip_numeric/bench.py)、[l2_torch.py](../plugins/numeric/src/llmchip_numeric/l2_torch.py)、[flow-six](../plugins/flow-six/flow.json) |
| 真实模型导入与图还原 | 按本机路径导入已下载的模型目录（safetensors + config.json + tokenizer.json，或 llama2.c），从文件头做张量普查并估算，还原计算图（Llama 家族：Llama、Mistral、SmolLM、Qwen2、Qwen3、llama2.c），执行为 L0，并与 transformers / run.c 移植逐项测试：权重逐位、结构参数、参数总量、每层中间结果、每个提示的 logits、top-1 与贪心生成、单 token 与长序列边界用例。项目自带的 llama2.c 权重（如占位模型）直接从产物导入。S1 页面可选择任一已导入模型或新路径，填写自定义提示，并展示每个测试项 | [model-llama/src/llmchip_model/pipeline.py](../plugins/model-llama/src/llmchip_model/pipeline.py) |

### 1.3 只有接口（已声明，无行为）

- `PluginKind` 中的 `ui-panel`、`llm-provider` 和 `parser`（[kernel.schema.json](../packages/kernel-api/schemas/kernel.schema.json)）。没有插件声明它们。LLM 提供方是内核代码，不是插件。
- 默认流程中的检查 `sdf.timing`。它已定义，但没有流程步骤使用它。它要求能力 `sdf-timing-sim`，而没有插件声明该能力，所以它只会判为 `indeterminate`。
- 验证报告（sim-verilator `verify`）中的 `formal` 字段，始终为空（FR-VER-07）。
- 内核内 Cordis 插件的宿主 `PluginHost`（[plugin-host.ts](../packages/kernel/src/plugin-host.ts)）。它能工作，也有测试，但只有 `KernelOptions.plugins` 会向它加载插件，而只有测试会传入这个选项。没有产品插件在内核内运行（见 §7.3 的 C5）。

### 1.4 未实现

Tauri 桌面外壳、安装器、专用 WSL 发行版、首次启动引导、自动拉取镜像。此外还有：CHARTER.md 生成、示例项目、项目导出与导入、项目 git 工作目录（架构 §6.3）、模型训练（FR-QNT-05，P2）、Llama 家族以外和 GGUF 的图还原、`images/`。§7.1 按需求编号列出。负责人的决策记录放在 `docs/adr/`（最新为 ADR-012），它是本地材料、不提交 git（AGENTS.md「决策记录」）。

---

## 2. 模块职责与依赖

### 2.1 包与入口

| 模块 | 入口 | 职责 | 允许依赖 |
|---|---|---|---|
| `@llmchip/kernel-api` | [src/index.ts](../packages/kernel-api/src/index.ts)、[schemas/kernel.schema.json](../packages/kernel-api/schemas/kernel.schema.json) | 契约：方法表、事件种类、插件协议、记录。schema 是唯一来源，`src/generated/` 由它生成 | 运行时不依赖任何包（`ajv` 只用于测试）。**绝不依赖 Cordis**（AP-6） |
| `@llmchip/kernel` | `harnessd` → [src/cli.ts](../packages/kernel/src/cli.ts) → [src/main.ts](../packages/kernel/src/main.ts) → `Kernel.start` | 守护进程：K1–K10、智能体运行时、API 服务 | `kernel-api`、`cordis`（仅 `plugin-host.ts`）、`ws`、`ajv` |
| `@llmchip/client` | [src/index.ts](../packages/client/src/index.ts) `KernelClient` | Node.js 与浏览器通用的 IF-1 客户端 | `kernel-api` |
| `@llmchip/cli` | `llmchip` → [src/bin.ts](../packages/cli/src/bin.ts) → [src/main.ts](../packages/cli/src/main.ts) | 命令行、`llmchip ui`、`llmchip mcp` | `client`、`kernel-api` |
| `@llmchip/plugin-sdk` | [src/index.ts](../packages/plugin-sdk/src/index.ts) `runPlugin` | TypeScript 插件的 IF-2 SDK | `kernel-api` |
| `llmchip-sdk`（Python） | [llmchip_sdk/\_\_init\_\_.py](../python/llmchip-sdk/src/llmchip_sdk/__init__.py) `run_plugin` | Python 插件的 IF-2 SDK | 只用标准库 |
| `llmchip-vchip`（Python） | `vchip` → [llmchip_vchip/cli.py](../python/llmchip-vchip/src/llmchip_vchip/cli.py) | 虚拟芯片测试环境：按 `chip.json` 加载芯片，经芯片的主机协议从提示词驱动贪心生成，对照参考芯片与黄金芯片衡量质量、按周期衡量速度，按芯片自己的策略判定，写出运行目录（[README](../python/llmchip-vchip/README.md)）。不在内核内 | `llmchip-model`、`llmchip-numeric`、`llmchip-rtl`（参考、数值与 RTL 芯片）。绝不依赖内核；硬件在独立进程中运行（RTL 桥，在 EDA 容器内） |
| `@llmchip/web` | [apps/web/src/main.tsx](../apps/web/src/main.tsx) | Web UI，由 harnessd 从 `apps/web/dist` 提供 | `client`、`kernel-api`、React |
| `plugins/<id>` | `llmchip-plugin.json` | 领域工作：每次运行一个进程 | 只依赖 SDK，绝不依赖内核 |
| `vendor/*` | [vendor/README.md](../vendor/README.md) | Cordis 及其配套库，保持上游源码 | 彼此之间 |

下面是允许的依赖方向。`yarn check:workspace` 只检查两件事：`packages/` 中的 workspace 导入都已声明为 `workspace:*` 并在 `tsconfig.json` 中引用；`kernel-api` 没有 workspace 依赖。它不检查方向本身，也不检查 `apps/web`。

```
导入（构建期）：
  kernel-api        ◀── kernel, client, plugin-sdk, cli, web
  client            ◀── cli, web
  vendor/cordis     ◀── kernel（生产代码中仅 plugin-host.ts）
  plugin-sdk        ◀── TypeScript 插件        llmchip_sdk ◀── Python 插件

协议（运行期）：
  cli / web / MCP 桥 ──IF-1（WebSocket + HTTP）──▶ harnessd
  harnessd ──IF-2（stdio）──▶ 插件进程   [runtime: container 时在 podman 内运行（IF-3）]
  插件绝不导入内核，也只通过 IF-2 与内核交互
```

以下规则没有工具检查，需要开发者自己遵守：

- 前端（CLI、Web UI、MCP、以客户端身份工作的测试）只通过 `KernelClient` 访问内核，不导入 `@llmchip/kernel`。`tests/` 可以在进程内启动内核（`tests/helpers.ts` 的 `startKernel`），但应通过客户端驱动它。
- 插件返回输出、指标和解析状态。判定只由判定引擎做（AP-7、ADR-009）。
- 内核状态只通过 `EventStore.append` 改变。除 `events` 与 `projections` 外，每张 SQL 表都属于某个 `Projection`，只在其 `apply` 中写入。组件可以直接读取投影表。

### 2.2 内核组件

`Kernel` 构造函数按依赖顺序创建 1–14 号组件，每个组件只拿到排在它前面的组件。随后 `Kernel.start` 加载内核内插件，并创建服务（15 号）：

| # | 组件 | 类、文件 | 拥有 | 使用 |
|---|---|---|---|---|
| 1 | 主目录与锁 | `homeLayout`、`acquireLock`（[config/home.ts](../packages/kernel/src/config/home.ts)、kernel.ts） | `<home>/` 各目录、`run/harnessd.lock` | — |
| 2 | K10 配置 | `loadConfig`（[config/config.ts](../packages/kernel/src/config/config.ts)） | 合并后的 `WorkspaceConfig`（schema 默认值 < `config/config.json` < 命令行参数） | — |
| 3 | K10 密钥 | `SecretStore`、`Redactor`（[config/secrets.ts](../packages/kernel/src/config/secrets.ts)） | `secrets/secrets.json.enc`、`secrets/master.key`（或 `LLMCHIP_MASTER_KEY`） | 事件（在 5 号之后通过 `bindEvents` 绑定） |
| 4 | 日志 | `createLogger`（[util/logger.ts](../packages/kernel/src/util/logger.ts)） | `logs/harnessd.log`（已脱敏） | Redactor |
| 5 | K1 事件 | `EventStore` | `db/harness.sqlite` | Redactor |
| 6 | K2 产物 | `ArtifactStore` | `store/objects/`、`store/tmp/`、`artifacts` 投影 | 事件 |
| 7 | K9 注册表 | `PluginRegistry` | 内存中的清单；把插件数据文件导入为产物 | 产物 |
| 8 | K4 调度器 | `Scheduler` | `runs` 投影（即队列）、`runs/<id>/`、子进程、容器 | 事件、产物、注册表 |
| 9 | 事故 | `Incidents`（[security/incidents.ts](../packages/kernel/src/security/incidents.ts)） | `incidents` 投影 | 事件 |
| 10 | K8 网关 | `EgressGateway` | 指纹与去密规则投影 | 事件、产物、事故 |
| 11 | K5 策略 | `Policy` | 外部方法白名单 | 事故、网关 |
| 12 | K3/K6 | `Workflow` | 流程包、检查定义、`workflow` 投影 | 事件、产物、注册表、调度器、事故 |
| 12a | K3 流水线 | `Pipelines`（[workflow/pipelines.ts](../packages/kernel/src/workflow/pipelines.ts)） | `pipelines` 投影、正在等待的运行 | 事件、注册表、调度器、工作流、日志 |
| 13 | 智能体运行时 | `AgentRuntime` | `agents` 投影、活动会话 | 事件、产物、调度器、工作流、网关、事故、日志、密钥（不经过 API 策略） |
| 14 | K9 内核内宿主 | `PluginHost` | Cordis 根上下文 | 事件、产物、日志 |
| 15 | IF-1 服务，在 `Kernel.start` 中创建 | `KernelServer`（[server.ts](../packages/kernel/src/server.ts)） | 连接；`run/harnessd.json` 由 `Kernel.start` 写入 | 策略、处理函数、事件（订阅）、产物（HTTP） |

所有 API 方法都集中在 `Kernel.handlers()` 中注册，每个方法名对应一次组件调用。

### 2.3 主目录（`$LLMCHIP_HOME`，默认 `~/.llmchip`）

```
config/config.json         可选；schema WorkspaceConfig
secrets/                   master.key、secrets.json.enc         (0700)
db/harness.sqlite          事件 + 投影（WAL，synchronous=FULL）
store/objects/ab/cdef…     产物内容，权限 0444
store/tmp/                 写入中的临时文件；启动时清空
run/harnessd.json          url、所有者令牌、外部令牌             (0600，运行期间存在)
run/harnessd.lock          持有该目录的内核的 pid
logs/harnessd.log          JSON 行，密钥已脱敏
runs/<run-id>/             in/ out/ work/ log.txt；运行结束即删除
plugins/                   额外的插件搜索路径
```

### 2.4 内置插件

在全新主目录上，`plugins.list` 列出以下插件。阶段与检查来自 `flow-default`（流程版本 0.12.0：0.11.0 删除审批门、阶段 S8 与 S9、检查 `package.integrity` 与 `external.signoff`，以及检查定义中的 `waiver` 字段；`accuracy.budget` 与 `phy.drc` 原先允许豁免，因此升到版本 2。0.10.0 新增虚拟芯片检查 `vchip.l3` 与 `vchip.conformance`；行覆盖率开始计入全部覆盖的文件，`rtl.equivalence` 因此升到版本 2；`arch.spec` 在设了周期目标时判定它，因此升到版本 3；0.12.0 新增每个阶段的研发流水线与必需测试）。

| 插件 | 运行方式 | 处理器 | 用于 | 单元测试 |
|---|---|---|---|---|
| `flow-default` | data | 流程包：S0–S7、20 项检查、阶段准入检查、硬约束槽位、每个阶段的研发流水线与必需测试；数据 `estimate-request-proxy`、`timing-exceptions-none` | 全部 | 经由 `tests/*-flow.spec.ts` |
| `flow-six` | data | 六步流程包（Qwen2.5-0.5B-Instruct，负责人决定 2026-10-24）：目前有 SPEC、RTL、VER、SYN 四步，49 项检查；PD 与 SGN 随 R6、R7 加入 | SPEC、RTL、VER、SYN | `tests/qwen-spec.spec.ts`、`tests/flow-packs.spec.ts`、`tests/tile-verify.spec.ts`、`tests/tile-syn.spec.ts` |
| `process-library` | data | 工艺档案 `gf180mcu`、`sephonchitech-180bcd`、`sky130`、`ihp-sg13g2`；实现库 `library-180nm`、`library-130nm`；`signoff-gf180mcu` | S0、S3、S6–S7 | 经由估算器与流程测试 |
| `estimator` | process（Python） | `estimate`、`check_views`、`migrate` | S0、S4/S6/S7 阶段准入、FR-MIG | `test_estimate.py`、`test_migration.py` |
| `model-llama` | process（Python） | `synthesize`、`intake`、`reference`、`import`（可另存为一个模型归档）、`recover`、`run_graph`、`compare` | S0（导入）、S1、SPEC | `test_model.py`、`test_graph.py` |
| `numeric` | process（Python；`benchmark`、`l2_fast_check`、`calibrate_graph`、`diagnose` 用 GPU） | `quantize`、`infer`、`evaluate`、`quantize_graph`、`calibrate_graph`、`calibration_data`、`evaluate_graph`、`benchmark_data`、`benchmark`、`benchmark_compare`、`diagnose`、`l2_fast_check` | S2、SPEC | `test_numeric.py`、`test_graph_l2.py`、`test_l2_torch.py`、`test_bench.py`、`test_benchcode.py`、`test_gptq.py`、`test_diagnose.py` |
| `arch-taalas` | process（Python） | `design`（计算图数值规格用 `taalas-tile/1`，向量引擎或 `taalas-spatial/1`）、`verify_spec`、`explore`、`slice`、`tlm` | S3、SPEC | `test_arch.py`、`test_spatial.py`、`test_slice.py`、`test_tile.py`、`test_tlm.py` |
| `rtl-taalas` | process（Python） | `generate`、`check_isa`、`tile_generate`（切片的 `taalas-tile/1` RTL bundle）、`tile_check`（ROM 往返、规则、三种 lint、Yosys、ROM 三个视图） | S4、S5、RTL | `test_rtl.py`、`test_spatial.py`、`test_tile.py` |
| `sim-verilator` | container | `lint`、`qualify`、`verify`、`gatesim`、`sdfsim`、`generate` | S5、S7、Playground | `test_simlib.py` |
| `eda-gf180` | container | `calibrate`、`constraints`、`implement`、`drc`、`lvs`、`sta` | M1.5、S6、S7 | `test_calibrate.py`、`test_physical.py` |
| `syn-yosys` | container | `synth`、`dft`、`timing`、`atpg` | SYN | `test_syn.py` |
| `vchip` | process（Python；仿真器在 Podman 中启动） | `bench`、`conform` | S2 之后（L2）、S4 之后（RTL）；Web UI 页面“虚拟芯片” | `test_plugin.py` |

仅测试用的插件在 [tests/fixtures/plugins](../tests/fixtures/plugins)：

- `stub-tool`（TypeScript）的处理器有 `report`（参数 `violations`、`truncate`、`coverage`、`crash`）、`qualify`、`sleep`、`hang`、`crash`、`fail` 和 `escape`。
- `container-stub`（在容器中运行的 Python）的处理器有 `probe`（探测沙箱）和 `sleep`。
- `test-flow` 是一个三阶段流程包。

各领域插件产出什么：

- **estimator**：可行性、面积、成本与性能估算（FR-EST），给出下限/典型/上限值、每项的来源与置信度，以及缺失的参数。`check_views` 按阶段报告已标定存储实现具备哪些集成视图。`migrate` 依据设计对工艺能力的依赖评估迁移影响（FR-MIG）。
- **process-library**：工艺档案（含迁移所需的工艺数据）、实现库与 GF180 签核规则。每个数值都记录来源与置信度，经验值在标定前始终是经验值。
- **flow-default**（流程版本 0.12.0）：阶段 S0–S7 及其检查，阶段准入检查，以及硬约束槽位 `constraints`、`timing-exceptions`、`signoff-rules`。没有审批门（负责人决定，2026-10-24）。每个阶段声明研发流水线 `develop` 与必需测试 `tests`，驱动 5K 占位模型走与 `yarn demo` 相同的正向路径：参数固定在流程包中（占位模型结构、三值数值方案、1.2×2 mm 晶粒上的向量引擎）；默认产物（占位模型的估算请求 `estimate-request-proxy`、空的时序例外登记表 `timing-exceptions-none`、工艺档案、评估集、验证计划、签核规则）只在槽位为空时绑定。S1 没有必需测试（占位模型的导入没有检查）。
- **flow-six**（流程版本 0.2.0）：负责人 2026-10-24 决定的六步（SPEC、RTL、VER、SYN、PD、SGN），设计对象 Qwen2.5-0.5B-Instruct；没有审批门。目前有 SPEC、RTL、VER、SYN 四步，物理设计与签核随 R6、R7 加入。SPEC 的输入有三项由负责人提供：槽位 `model`（`model-llama import` 带 `archive: true` 产出的模型归档）、`benchmark-data`（`numeric benchmark_data` 打包的本机基准数据）与 `calibration-data`（`numeric calibration_data` 打包的本机标定文本：WikiText-2 或一个文本文件）。它们是普通槽位，不是硬约束，流水线或智能体都可以绑定；只有模型与本机基准数据是负责人独有的。`model.census`、`model.graph`、`model.upstream` 三项检查在 `flow-default` 中也有定义：加载器要求跨流程包的定义完全一致（§5.2），所以改一处必须两处都改，否则该流程包会被丢弃。研发流水线依次是：导入（判定 `model.census`）、图还原（`model.graph`）、与 transformers 对拍（`model.upstream`）、校准量化（在标定文本上做 GPTQ：W4、分组 128、上下文 2048、逐层小数位、残差流 24 位）、快速 L2 对参考 L2（`spec.l2.fast`）、L0 与 L2 的快速集、二者比较（7 项 `spec.accuracy.quick.<基准>`），对“L0 答对、L2 答错”的题的回溯诊断（`spec.diagnosis`）、第 0 层的切片（`spec.slice`）、`taalas-tile/1` 设计（节点 `design`：产出芯片规格与 PPA，自身不挂检查）、对该芯片规格的一致性检查（节点 `verify`：运行 `verify_spec`，判定 `spec.design`；节点只能判定定义在自己处理器上的检查，否则加载器直接拒绝该流程包）以及它在同一批 token 上的事务级模型（`spec.tlm`）。必需测试对绑定的槽位判定 `model.upstream`、`spec.l2.fast`、7 项快速集检查、`spec.diagnosis`、`spec.slice`、`spec.design` 与 `spec.tlm`；完整集的 7 项 `spec.accuracy.<基准>` 判定 `set: full` 的比较，目前只能用 `runs.start` 启动（§7.2）。RTL 步（流程版本 0.2.0）为绑定的切片生成 `taalas-tile/1` 的 bundle（`rtl-taalas tile_generate`：模板、每个 bank 的视图① ROM 内容、清单），并检查它（`tile_check`，5 项检查：`rtl.tile.lint`、`rtl.tile.synthesizable`、`rtl.tile.roundtrip`、`rtl.tile.rules`、`rtl.tile.rom.views`——最后一项用 Yosys 的等价引擎证明行为 ROM 与综合出的标准单元网表相等）。因为 `flow-default` 已经定义了 `rtl.lint`，而加载器要求跨流程包的定义完全一致，这些检查命名为 `rtl.tile.*`。VER 阶段（R4）绑定同一个 `rtl-bundle` 并运行 `rtl-taalas tile_verify`：`ver.unit`（模板套件）、`ver.slice`（功能化 S-1 q_proj 切片对 `GraphL2`）、`ver.cdc`（clk/host_sclk 两个域）与 `ver.layer`（两个 Q-full 层，各 17 个观测点，串联对 `GraphL2`）。覆盖、变异、形式、DFT 与 `rtl.tile.equivalence` 也都可以运行（R4）。
  - `spec.accuracy.quick.<基准>` / `spec.accuracy.<基准>`：每个基准一项，按需求 FR-QNT-04：下降（L0 − L2，百分点）大于允许值（MMLU、CMMLU、C-Eval 2，GSM8K、IFEval 3，HumanEval+、MBPP+ 5）为 fail；不大于允许值但 95% 区间的上界大于它为 indeterminate（用带 `when` 的完整性条件表达）；否则 pass。每个基准各占一项检查，是因为判定引擎先判完整性：若写在一项检查里，一个基准的区间太宽会把另一个基准明确的 fail 盖成 indeterminate（评审发现，2026-10-24）。
- **model-llama**：
  - 模型导入与图还原（见下文）。`import` 带 `archive: true` 时另存整个模型为一个产物 `model.archive`（模型文件与 `bundle.json` 的 tar），流水线经槽位把它交给每个读模型的处理器（`open_model`；它们也仍接受 `bundle` 与 `files`）。
  - 把 llama2.c 检查点与分词器导入为 S2–S7 使用的 llama2.c 模型 IR。
  - 生成占位模型：采用某个规划模型的结构，固定种子，未训练，附带 `evalset-proxy`。
  - 提供 L0 FP32 参考，即 `run.c` 的 NumPy 移植。它与 `plugins/model-llama/upstream/` 中随仓库提供并编译的 `run.c` 对照检查。
- **numeric**：量化（int8/int4/int3/三值，分组缩放）、数值规格（需求 §3.4.2），以及带观测点轨迹的 L2 逐位精确整数模型。它还做 L2 对 L0 的精度评估（S2 的证据），并附带评估集。舍入（`half-up`、`half-even`、`truncate`）、溢出处理（`saturate`、`wrap`）和累加器位宽（已证明不溢出的位宽，或更窄的位宽）是规格中的结构化字段，统一定义在 [arith.py](../plugins/numeric/src/llmchip_numeric/arith.py)；默认值得到的 L2 与引入这些选项之前逐位相同。`quantize_graph` / `evaluate_graph` 对任何已还原的计算图做同样的事（[graph_l2.py](../plugins/numeric/src/llmchip_numeric/graph_l2.py)，规格格式 `llmchip-numeric-graph-spec/1`）：这是对向量引擎无法实现的模型的分析，默认逐层确定小数位、残差流 24 位，由 `numeric.accuracy` 判定。在 llama2.c 计算图上，它与芯片 L2 逐位相同。
  - **KV 缓存**：`GraphL2.forward` 可带 `KVCache`，先送入提示，再逐 token 继续，结果与整段一次计算逐位相同；`rows` 只计算指定位置的 logits（15 万词表时整段 logits 太大）。
  - **快速 L2**（[l2_torch.py](../plugins/numeric/src/llmchip_numeric/l2_torch.py)，`TorchL2`）：同样的整数运算，在 PyTorch 上（有 GPU 时用 GPU）。矩阵乘是唯一的浮点运算，并且精确：操作数拆成 8 位分段，每 256 项一组求和，部分和都小于 2^24，float32 能精确表示；关闭 TF32。多个序列右补齐后成批计算，补齐位置不影响结果，也不计入溢出次数；KV 缓存按槽位存放。Qwen2.5-0.5B（W4，分组 128）：单序列每 token 约 150 ms（瓶颈在启动内核），32 个序列一批时折合每 token 6.8 ms；300 token 的提示一次约 0.35 秒（2026-10-24）。`l2_fast_check`（检查 `spec.l2.fast`）比较三件事：经 KV 缓存成批生成的每一步 logits、整段计算的全部观测点与溢出次数、给答案打分用的成批补齐路径（logits 与溢出次数）。
  - **基准评测**：`benchmark_data` 把本机已下载的基准（工具目录的布局：`hf/`、`datasets/cmmlu`、`datasets/evalplus`、`nltk_data`）打包为一个产物 `benchmark.data`（按 lm-eval 任务与切分存的 parquet、EvalPlus 题目文件、NLTK 数据；标签 `internal`，CMMLU 与 C-Eval 是非商业许可），并按固定种子 20261024 选出快速集：选择题每科 5 题、生成类每个基准 5 题，共 900 题（`benchmark.sets`）。评测只读这个产物：把 lm-eval 的数据加载转到打包的文件（[benchdata.py](../plugins/numeric/src/llmchip_numeric/benchdata.py)），所以题面、少样本示例、过滤与指标都是 lm-eval 自己的。`benchmark`（参数 `layer: L0|L2`、`set: quick|full`）：L0 是 lm-eval 的 Hugging Face 模型（bf16，上游原生精度）；L2 是同一个模型类，只把两处模型调用换成快速 L2（[benchlm.py](../plugins/numeric/src/llmchip_numeric/benchlm.py)）：对数似然取芯片整数 logits 乘 2^-frac 后在主机上 float64 的 log-softmax，生成用 KV 缓存贪心解码。两者都只用贪心解码（模型自带的重复惩罚 1.1 与采样设置被替换，评审发现）和同样的 `max_length`（数值规格的上下文）。判对错：选择题取对数似然最大的选项；GSM8K 取最后一个 `\boxed{}` 中的数（零样本，Qwen 自己评测用的指令；lm-eval 默认的 5-shot 在 256 token 处截断指令模型的回答）；IFEval 用严格的提示级准确率；HumanEval+ / MBPP+ 按 EvalPlus 的提示、停止串与清理生成代码，在 `podman run --network=none --read-only` 中由 `evalplus.evaluate` 运行基础测试与扩充测试，两者都过才算对（[benchcode.py](../plugins/numeric/src/llmchip_numeric/benchcode.py)，镜像按摘要固定）。每完成一个基准存一次检查点，中断后从下一个基准继续。`benchmark_compare` 逐题配对两份报告（同一批题、同一模型、同样设置，否则拒绝），给出每个基准的下降与配对 bootstrap（10,000 次，固定种子）95% 区间，以及只有一方答对的题。L0 的结果按（模型、题目、评测设置）缓存的方式是运行复用：输入与参数相同的 L0 运行不重算（需求 FR-QNT-08）。
- **arch-taalas**：Taalas 风格的硬连线模板。由冻结的数值规格推出芯片规格与 PPA，并检查二者一致（S3 的检查 `arch.spec`）。它的速度不是公式：它编译出引擎的微码（`Compiled`），再用 [perf.py](../plugins/rtl-taalas/src/llmchip_rtl/perf.py) 数周期。这是核心状态机和存储端口的逐状态模型，其忙碌周期与 RTL 仿真逐步相等（[performance.py](../plugins/arch-taalas/src/llmchip_arch/performance.py)；`verify_spec` 会重新计算，所以芯片规格不能声称另一个速度）。芯片规格的微架构选项有：通道数、`memoryInterface`（`spi`，或 `sqi`：四根双向线的四线 SPI）、`operandBuffers`（`none`；`attention`：注意力头的 q 和输出累加器，用循环移位寄存器实现；`all`：再加一个供矩阵乘和 RMSNorm 用的向量缓冲）。`explore` 计算所有组合（精确周期、估算面积、能否放下、帕累托前沿），也能按形状分析导入模型的计算图数值规格。
- **rtl-taalas**：把数值规格编译成微码化向量引擎，产出一个确定性的 `rtl.bundle`。其中包含：
  - Verilog 核心（数据放在片外 SPI 或四线 SPI SRAM，可选片上操作数缓冲），以及 SPI 主机接口；
  - 生成为 case 表的 ROM，以及微码；
  - 带 SRAM 模型的 Verilator 测试平台，以及用于门级仿真的 `gl_tb.v`；
  - 指令级模拟器 `isa.py`。

  权重映射规则可逆，并做往返检查。`check_isa` 对照 L2 验证微码。
- **空间引擎 `taalas-spatial/1`**（负责人决定，2026-10-20：演示芯片复刻 Taalas，把模型做成电路）。`arch-taalas design` 带参数 `architecture: taalas-spatial/1`（[spatial.py](../plugins/arch-taalas/src/llmchip_arch/spatial.py)），`rtl-taalas generate` 读到这样的芯片规格时生成空间引擎（[spatial/](../plugins/rtl-taalas/src/llmchip_rtl/spatial/)）：
  - 每个矩阵的每一行是一棵按整数权重连线的位串行加法树（+1 加上输入，−1 减去输入，0 不连线，更大的值加上移位副本）；组缩放尾数是移位副本的固定和（CSD 数字，每组一条带抽头的延迟线）；行输出按数值规格舍入和收窄（[sp_lib.v](../plugins/rtl-taalas/src/llmchip_rtl/spatial/rtl/sp_lib.v)）。每层都有自己的电路；
  - 注意力是共享单元（16 个乘法器，两个头对同一个 KV 头；16 个精确的恢复除法器），KV 缓存在片上，是每拍读一行的寄存器堆（最初用的是循环移位环：32,768 个触发器到触发器的连接在 CTS 后每个都要一个保持时间缓冲器，这些缓冲器放不下）；没有程序，也没有片外存储器；
  - 状态机为 `Spatial.schedule()` 的每个阶段设一个状态，所以每一步在任何位置都是固定周期数（占位模型 1,327 周期：8 MHz 下 6,029 tok/s）；
  - 往返检查（FR-RTL-02）从生成的网表读回每个电路，把它的线性映射与 q × m 比较（`check_round_trip`）；bundle（格式 `llmchip-rtl-bundle/3`）附带电路模型 `sp_model.py`，在 L2 跟踪没有覆盖的地方作参考；
  - S3 页面用专门的视图展示空间设计（`S3Spatial.tsx`）：每个矩阵的电路（行数、串行加法器、延迟位、数据流位数），注意力单元与 KV 缓存，各模块面积与晶粒的对比，时序表按阶段组和逐阶段的周期，以及除法器数 × 时钟的设计空间；试验台可选择模板、除法器数、时钟和晶粒；
  - 主机接口增加 `0x30 READ`（读观测字）；验证通过它读取所有观测点，不走后门；
  - 面积等于生成的结构数乘以综合实测的单元面积（`gf180mcu` 工艺档案中的 `spatialCells`），按档案的利用率摆放；晶粒取尺寸规则内最小的一个（占位模型为 5.0 × 3 mm）；
  - 选项 `dividers`（4 到 64 个精确的 softmax 除法器）用面积换周期；`explore` 带 `architecture: taalas-spatial/1` 时计算每种除法器数和时钟（生成时序表的精确周期、面积、晶粒、能否放下、帕累托前沿：占位模型 8 MHz 下，4 个除法器 3,733 tok/s，64 个 7,124 tok/s，单元面积 5.89 到 6.25 mm²）。对导入模型的计算图数值规格，它按形状做分析，用占位模型各权重格式的电路比例，并注明是外推。
- **sim-verilator**（在固定版本的 `iic-osic-tools` 镜像中运行 Verilator、Icarus Verilog 和 cocotb）：
  - lint 与仿真器资格认证；
  - 验证计划（回归、定向、边界、穷举、随机、覆盖率；内置默认计划），带首个分歧定位（算子、元素、周期、VCD 窗口）；空间 bundle 使用 `verification-plan-spatial`（位串行单元由 cocotb 在设计的数据流长度下做单元测试，`cocotb_spatial.py`；核心的查表索引单元做穷举测试，`cocotb_spatial_index.py`）。行覆盖率统计每个带注释的源文件，包括全部覆盖的文件（`--annotate-all`；2026-10-21 之前这些文件没有计入）；常量表是数据，不计入。生成的 C++ 被拆分（`--output-split`），以便并行编译；
  - 布线后网表的门级仿真（L4，使用 PDK 单元模型的 Icarus Verilog）；
  - 带反标统计的 SDF 时序仿真（补充能力，尚未认证）；
  - 为芯片 Playground 生成 L3 结果。
- **eda-gf180**（同一镜像，含 gf180mcuD PDK、Yosys、OpenROAD、Magic、KLayout、Netgen、OpenSTA）：
  - 存储实现标定（FR-EST-13）；
  - 由芯片规格生成约束；
  - 带焊盘环的物理实现（LibreLane Chip 流程）；
  - DRC、LVS，以及含约束完整性检查的 9 角 STA，每项检查只在它所判定的产物上运行。
- **vchip**（负责人草案设计的 L4；[入口](../plugins/vchip/src/llmchip_vchip_plugin/__main__.py)）：在项目产物上运行虚拟芯片测试环境。每次运行在运行工作区里构建芯片（[build.py](../python/llmchip-vchip/src/llmchip_vchip/build.py) 的 `chips_from_artifacts`）：L2 黄金芯片来自 `numeric.spec` 与 `numeric.weights`；给了 `model.ir` 时构建浮点参照芯片（模型 IR 写回 llama2.c 检查点，`save_llama2c`）；给了 `rtl.bundle` 时构建 RTL 芯片，它就是被测芯片。bundle 必须正是由这份规格和这些权重生成的（看它的 `source` 哈希），且只有 `taalas-spatial/1` 能在引脚级 testbench 下运行。`chip.spec`（S3）必须由同一份规格和权重设计而来，有 RTL 时还必须描述同一个设计（架构、除法器数）；这时它的周期与时钟就是设计值。没有芯片规格时，周期取自 bundle，时钟取参数 `clockMHz` 或 8 MHz；指标写明来源（`design_cycles_source`、`clock_source`）。`bench` 使用 `eval.set` 或手输的 `prompts`；`conform` 运行 T-01–T-19（`quick` 使 T-18/T-19 为 `not_run`），并报告其验收部分所用的标准（`acceptance_tok_s_min`、`acceptance_bit_exact`、`acceptance_max_tokens`）。报告不含判定，由检查判定指标。`vchip.l3`：完整性为 RTL、≥ 20 个提示词、≥ 64 个 token、已与黄金芯片对比、设计周期与时钟取自芯片规格；判据为无错误、逐位一致、测量可信、周期等于设计、≥ 1000 tok/s。`vchip.conformance`：完整性为没有未运行的检查项、≥ 20 个提示词，RTL 芯片还要求验收部分按 L3 标准、以芯片规格的时钟进行；判据为没有失败的检查项。因此，运行不能靠自选时钟或删减提示词通过。运行测不到的指标不出现，需要它的检查因此判为 indeterminate。这两项检查不是任何阶段的准入检查（流程不变）；它们的判定只对该次运行的输入构成证据。被取消的运行（SIGTERM）在退出前结束芯片的仿真进程并移除其容器。

---

## 3. 一条完整运行路径

下面的流程于 2026-10-19 在全新主目录上实际运行过。事件序号是当时观察到的序号。

### 3.1 路径 A：`llmchip estimate --model stories260K`

```sh
yarn harnessd serve &                        # 用 LLMCHIP_HOME=/tmp/h 可得到一次性主目录
yarn llmchip estimate --model stories260K    # 打印 Markdown 摘要
yarn llmchip run list                        # 这次运行、执行状态与输出
yarn llmchip events list                     # 记录了什么
```

**内核启动**（`Kernel.start`）：

1. 构造函数获取 `run/harnessd.lock`，加载配置与密钥，打开事件库。它校验最后 `events.verifyTailOnStart`（1000）条哈希；哈希链断裂则拒绝启动（`EVENT_LOG_CORRUPT`）。然后清空 `store/tmp/`。
2. 事件 1 为 `kernel.started`。
3. `PluginRegistry.scan` 依次搜索 `plugins/`（`plugins.bundled` 为真时搜索内置插件）、`<home>/plugins`、`plugins.searchPaths`。配置分层只做浅合并，所以 `--plugins` 会替换 `config.json` 中的 `searchPaths`。它按 `PluginManifest` 和 `kernelApi` 范围校验每个 `llmchip-plugin.json`，并把每个 `data` 文件导入为产物：事件 2–10 都是 `artifact.created`，执行者都是 `plugin:*`。有问题的插件被拒绝并记录，但不中断启动。
4. `Workflow.loadFlows` 读取每个 `flow.pack` 产物，汇总检查定义。对同一检查 id 有冲突定义的流程包、不符合 schema 的流程包（例如仍声明审批门），以及流水线里节点判定其他处理器检查（或阶段入口检查、必需测试引用了无人定义的检查）的流程包，都会被整体拒绝，记为 `FLOW_INVALID` 问题并写入内核日志：它的阶段与检查都不登记。项目创建时所用的流程包按原样保存在产物库中：读取 R0 之前的项目时，其中只属于历史的字段（`gates`、阶段入口的 `gates`、检查的 `waiver`）被忽略。
5. `Scheduler.recover` 把仍记为 `running` 的运行结束为 `interrupted`。它杀掉该运行遗留的进程组（仅当 `/proc/<pid>/environ` 含 `LLMCHIP_RUN=<id>` 时）及其容器，还有剩余次数时就重试（处理器声明了 `resume` 时从最近的检查点续跑），然后清空 `runs/`。随后 `Pipelines.recover` 让仍在运行的流水线继续：运行被中断的节点重新运行（同样从检查点续跑），期间已结束的运行记入节点。接着加载 `KernelOptions.plugins` 中的内核内插件（只有测试会传入）。
6. `KernelServer.listen` 监听 `127.0.0.1:<随机端口>`，然后写入 `run/harnessd.json`，内含 URL 与两个新令牌。最后调用 `Scheduler.pump`。

**命令**（[cli/src/main.ts](../packages/cli/src/main.ts) 中的 `case 'estimate'`）：

7. CLI 读取 `run/harnessd.json`。文件不存在时，提示如何启动内核并以 1 退出。`KernelClient.connect` 打开 `ws://…/v1/rpc?token=…`。
8. CLI 通过 `plugins.list` 在数据产物中找到工艺档案（默认 `sephonchitech-180bcd`）与实现库，然后调用 `runs.start {plugin: estimator, handler: estimate, params, inputs: [profile, library]}`。

**API**（`KernelServer.handleMessage`）：

9. 服务端按方法的 schema 定义校验请求参数（`INVALID_PARAMS`，JSON-RPC -32602）。`Policy.authorize` 放行所有者令牌。使用外部令牌时，白名单之外的方法会打开 `permission_denied` 事故，并以 `PERMISSION_DENIED` 失败。服务端调用处理函数，再校验结果；`RESULT_INVALID` 会转为内部错误。

**入队**（`Scheduler.start`，同步执行，此时还没有运行任何东西）：

10. 解析处理器（`PLUGIN_NOT_FOUND`、`HANDLER_NOT_FOUND`）。`data` 插件不能运行（`PLUGIN_NOT_EXECUTABLE`）。按处理器的 JSON Schema 校验参数并填入默认值（`RUN_PARAMS_INVALID`）。每个输入产物都必须存在（`ARTIFACT_NOT_FOUND`），并且要符合处理器声明的输入名称、类型、`required` 与 `multiple`（`RUN_INPUT_UNKNOWN`、`_TYPE`、`_MISSING`、`_MULTIPLE`）。
11. 运行标签取输入中最敏感的标签。事件 11 为带完整 `RunRecord` 的 `run.queued`，`runs` 投影随之插入一行，这一行*就是*队列。方法返回排队状态的记录。

**执行**（`Scheduler.pump` → `Scheduler.execute`）：

12. 并发上限为 `scheduler.maxParallel`（默认 2）。内核暂停时不启动新运行。
13. 创建 `runs/<id>/{in,out,work}`。每个输入复制到 `in/`（文件系统支持时用 reflink）并设为 0444，插件因此无法改动已存储的内容。
14. 由 `entry.command` 构造命令，替换其中的 `${python}`（存在 `.venv/bin/python` 时用它）、`${node}` 和 `${pluginDir}`。`runtime: container` 时用 `podman run` 包装（`Scheduler.containerCommand`）。`PluginProcess.spawn` 在独立进程组中启动命令。环境变量只有 `PATH`、`HOME` 之类，加上 `LLMCHIP_RUN`，插件看不到密钥。
15. 事件 12 为 `run.started`（含 pid）。随后设置超时，时长依次取：`runs.start` 传入的 `timeoutMs`；否则处理器的 `timeoutMs`（估算器为 60 秒）；再否则 `scheduler.defaultTimeoutMs`（1 小时）。
16. 通过 stdin 和 stdout 进行 IF-2 交换：
    - `initialize` 检查插件报告的 `id` 与清单一致。
    - 然后发送 `invoke {run, handler, params, inputs[{name, hash, type, label, path}], outDir, workDir}`。Python SDK 执行 `handle_estimate`（[estimator/\_\_main\_\_.py](../plugins/estimator/src/llmchip_estimator/__main__.py)），它向 `outDir` 写入 `estimate.json` 与 `estimate.md`，并返回 `Result(outputs, metrics, parse={status: complete})`。
    - 内核按 `ProtocolInvokeResult` 校验结果，再发送 `shutdown`。
    - 插件的 stderr 和 `log` 通知写入 `log.txt`（已脱敏）；`progress` 通知更新内存中的进度。
17. 计算执行状态：`exited_zero` / `exited_nonzero` / `crashed` / `timed_out` / `cancelled` / `interrupted`。**只有** `exited_zero` 才保留指标与解析状态。
18. `registerOutputs` 只在 `exited_zero` 之后登记输出：
    - 每个输出必须是 `out/` 内的文件，名称与类型都要在清单中声明。否则报 `RUN_OUTPUT_INVALID`，运行变为 `exited_nonzero`。
    - 每个输出入库时，`inputs` 设为该运行的输入（血缘），同时记录 `run`、`creator: plugin:estimator`，标签不低于运行标签。事件 13–14 为 `artifact.created`。
    - 输出逐个入库，不在同一个事务中。后面某个输出无效时，前面已入库的输出仍留在库中并带有 `run: <id>`，而运行记录不列出任何输出（§7.3 的 C6）。
    - 无论执行状态如何，运行日志都会成为 `run.log` 产物（事件 15）。
19. `finish` 追加事件 16 `run.finished`，含执行状态、退出码、输出、指标、解析状态和日志。在同一事务中，若执行状态为 `crashed` 或 `interrupted` 且还有剩余次数，就排入重试。随后唤醒 `runs.wait` 的等待者，并删除 `runs/<id>/`。

**结果**：

20. CLI 的 `runs.wait` 返回已完成的记录。CLI 通过 `GET /v1/artifacts/<hash>` 下载 `summary` 输出并打印（使用 bearer 令牌；外部客户端须先经出口网关检查）。此后 `artifact lineage <summary>` 会显示：它经由这次运行，以工艺档案和实现库为输入。

**失败处理（实测）：**

| 情形 | 在哪里停下 | 调用方看到 | 记录了什么 |
|---|---|---|---|
| 内核未运行 | CLI | `harnessd is not running for <home>`，退出码 1 | 无 |
| 参数不符合处理器 schema | `Scheduler.start` | `RUN_PARAMS_INVALID`，退出码 1 | 无（未入队） |
| 输入产物不存在，或输入未知、类型不符、缺失、重复 | `Scheduler.start` | `ARTIFACT_NOT_FOUND`、`RUN_INPUT_UNKNOWN` / `_TYPE` / `_MISSING` / `_MULTIPLE` | 无 |
| 处理器抛异常 | 插件 → JSON-RPC 错误 -32001 | 运行 `exited_nonzero`，`error` = `ValueError: …` | `run.finished`，以及含回溯的日志产物。不会发送 `shutdown`；内核最多等待 `cancelGraceMs`（2 秒）让插件退出，超时则杀掉进程组 |
| 插件在应答前退出、返回无效结果（`PLUGIN_RESULT_INVALID`），或自报为其他插件 | `Scheduler.execute` | `crashed` | `run.finished`；仅当 `maxAttempts` > 1 时重试 |
| 插件对 `initialize` 返回错误 | `Scheduler.execute` | `exited_nonzero` | `run.finished` |
| 插件向 stdout 写入非 JSON 行 | `PluginProcess` | 运行继续 | 运行日志中的 `protocol violation` 警告 |
| 输出在 `out/` 之外或未声明 | `registerOutputs` | `exited_nonzero`，`RUN_OUTPUT_INVALID …` | 运行不列出输出；坏输出之前已入库的输出仍保留（C6） |
| 超时或 `runs.cancel` | `Scheduler.abort` | `timed_out` / `cancelled` | 发送 `cancel` 通知和 SIGTERM，`cancelGraceMs` 后 SIGKILL，并执行 `podman kill` |
| 运行中内核被杀 | 下一次 `Kernel.start` | `interrupted`；仅当 `maxAttempts` > 1（默认为 1）时重试 | `Scheduler.recover`（AC-M1-04） |

### 3.2 路径 B：从运行到判定与阶段（S0 → S1）

没有审批门（负责人决定，2026-10-24）。判定绑定其运行的输入哈希：它只是这些产物的证据。阶段只有准入检查（`entry.checks`）会阻止进入；检查失败不锁住流程，但失败一直记录在案，直到原因改正、重新运行并通过。以下命令于 2026-10-24 在全新主目录上按顺序实际执行过：

```sh
yarn llmchip project create chip --title demo                    # created project chip（流程 "default"）
yarn llmchip artifact put req.json --type estimate.request --media-type application/json
yarn llmchip artifact put charter.md --type project.charter
yarn llmchip project bind chip charter <hash>                    # 另有 estimate-request、process-profile、impl-library
yarn llmchip run start estimator estimate --input request=<req> --input profile=<p> --input library=<l> --wait
yarn llmchip verdict evaluate estimate.feasibility <run> --project chip   # estimate.feasibility: pass
yarn llmchip stage enter chip S1                                 # entered stage S1
yarn llmchip stage enter chip S2                                 # entered stage S2
yarn llmchip call gates.release '{"project":"chip","gate":"G0"}'  # unknown method gates.release
```

[workflow/engine.ts](../packages/kernel/src/workflow/engine.ts) 内部发生了什么：

- **`Workflow.evaluate`**（K6）先确认运行属于检查所指定的插件和处理器，再由 `judge` 按顺序应用检查。
  - 判 `indeterminate` 的情形：执行状态不被接受；解析不是 `complete`；`requiredCapability` 未认证；`completeness` 条件不成立；缺少指标。
  - `criteria` 不满足判 `fail`。没有豁免：失败就是失败。
  - 判定的 `subjects` 是运行的输入哈希。引擎追加 `verdict.recorded`。
  - `fail` 或 `indeterminate` 会打开 `check_failed` 事故；运行报告了 `divergence_*` 指标时，事故会带上它们。
  - 若旧版本检查未通过、新版本检查却通过，会打开 `standard_lowered` 事故（AC-M1-13）。
- **`Workflow.bind`** 追加 `project.slot_bound`。`hardConstraintSlots` 中的槽位只能由人类绑定；其他人绑定会打开 `hard_constraint_change` 事故。
- **`Workflow.enterStage`** 要求每项入口检查（`{check, slot}`）在已绑定产物上的最新判定为通过。否则以 `STAGE_ENTRY_BLOCKED` 失败并给出原因，包括 `views.<stage>.missing` 指标（FR-PHY-08）。
- **`Workflow.applyMigration`** 记录已确认的迁移评估与重新开始的阶段（`migration.applied`）；从 S3 或更早重新开始须由人类确认（FR-MIG-04）。

旧事件日志中的 `approval.*`、`waiver.recorded` 与 `package.exported` 仍可读取并校验哈希链：这些事件种类只作为历史保留在 schema 中，没有写入者；`workflow` 投影（版本 3）忽略它们。判定结果 `waived` 与 `VerdictRecord.waiver` 同样只出现在历史记录中。

`tests/flow-helpers.ts` 的 `throughS3` 和 `throughS5Proxy` 用真实插件把同一路径走到 S3 和 S5。`tests/m4-flow.spec.ts` 再继续到 S7，并从 GDS 追溯到模型权重。


### 3.3 路径 C：一个阶段的两个入口（流水线）

每一步的“运行研发流程”和“运行全部测试”都是流水线（需求 FR-PRJ-07/08）。以下命令于 2026-10-24 在全新主目录上用内置插件实际执行过：

```sh
yarn llmchip project create demo --title demo
yarn llmchip pipeline plan demo S2 --kind develop      # S2: 5 nodes, not ready；逐项列出缺少的槽位（S2/quantize needs slot:model-ir ...），退出码 1
yarn llmchip pipeline plan demo all --kind develop     # S0, S1, S2, S3, S4, S5, S6, S7: 40 nodes, ready
yarn llmchip pipeline start demo S0 --kind develop --wait   # develop S0  passed  (3/3 done)；S0/estimate 判定 estimate.feasibility: pass
yarn llmchip pipeline start demo S1 --kind develop --wait   # develop S1  passed  (5/5 done)
yarn llmchip pipeline start demo S0 --kind test --wait      # test S0  passed；S0/estimate.feasibility 复用了研发流程的运行
yarn llmchip pipeline list --project demo
```

[workflow/pipelines.ts](../packages/kernel/src/workflow/pipelines.ts) 内部发生了什么：

- **`Pipelines.plan`** 由项目创建时的流程包展开节点（`build`）：研发流水线的每一步依次是 `<阶段>/defaults`、`beforeEntry` 节点、`<阶段>/enter`、其余节点，`all` 依次串起所有声明了研发流水线的阶段（后一步的 `defaults` 依赖前一步的全部节点）；必需测试每项一个节点，互不依赖。输入引用有三种：`slot:<槽位>`、`data:<数据 id>`、`node:<节点>.<输出>`。现在没有绑定、也不会由本次流水线前面的节点绑定的槽位，以及不存在的数据，列为缺少。
- **`Pipelines.start`** 在缺少输入时以 `PIPELINE_INPUTS_MISSING` 拒绝，什么都不声明时以 `PIPELINE_EMPTY` 拒绝，同一项目分支上已有流水线在其中某个阶段运行时以 `PIPELINE_RUNNING` 拒绝；否则追加 `pipeline.started`（含全部节点），然后 `advance`。
- **`advance`** 反复执行：依赖全部完成的等待节点开始执行；有依赖失败、跳过或取消的节点记为 `skipped`（`PIPELINE_DEPENDENCY_FAILED`）；没有等待和运行中的节点时追加 `pipeline.finished`（全部完成为 `passed`，否则 `failed`）。
- 节点的执行：`defaults` 把空槽位绑定到默认产物；`enter` 调用 `Workflow.enterStage`（准入检查判定）；`run` 以启动者的身份启动运行，`test` 先找同一处理器、同样参数（含默认值）和输入的成功运行，找到就复用（`reused`），否则启动一个。运行结束后（`Scheduler.wait`），`run` 节点把输出绑定到槽位并判定所列检查，`test` 节点判定它的检查；任何判定不是 `pass`、运行没有成功结束、或绑定被拒绝，节点为 `failed` 并带原因。硬约束槽位只在为空时由流水线绑定；已有人绑定的产物不被替换（`PIPELINE_HARD_CONSTRAINT_BOUND`），新的须由人手动绑定。只有节点当前的运行（或它复用的运行）能结束节点：取消后立即继续时，被取消的旧运行不影响新的一次。每次状态变化都是一条 `pipeline.node` 事件。
- **`Pipelines.cancel`** 把未结束的节点记为 `cancelled` 并取消它们的运行；**`Pipelines.resume`**（失败或取消的流水线）追加 `pipeline.resumed`，把失败、跳过、取消的节点放回等待，已完成的节点保留。重新运行同样输入的节点时，若上一次运行留有检查点且处理器声明了 `resume`，就从检查点续跑。

`tests/pipelines.spec.ts` 用 `test-flow` 证明这些语义（含杀掉内核后从中断的节点续跑）；`tests/pipelines-flow.spec.ts` 用内置插件在全新项目上跑 S0–S3 的研发与测试，在 `LLMCHIP_FULL_FLOW=1` 时用 `all` 跑完 S0–S7。

---

## 4. 数据与资源归属

### 4.1 谁写入什么

| 资源 | 唯一写入者 | 读取者 | 生命周期与释放 |
|---|---|---|---|
| 事件日志（`events`） | `EventStore.append` / `transaction` | 内核各组件；客户端通过 `events.list` / `events.subscribe` | 只追加、从不改写；`events verify` 校验哈希链 |
| 投影表 | 各 `Projection.apply`，在追加事务内 | 所属组件；UI 经 API | 可丢弃；`projections rebuild` 从日志重放 |
| 产物内容与目录 | `ArtifactStore.put`（`artifact.created`） | 调度器（复制）、HTTP 下载、智能体（`read_artifact`） | 不可变；`markDeleted` 只做标记，内容保留（尚无清除或 GC） |
| 插件数据文件对应的产物 | `PluginRegistry.register`，每次扫描时 | 流程、CLI `estimate`、测试 | 按哈希幂等；标签默认 `public` |
| 运行输出 | `Scheduler.registerOutputs` | 任何客户端 | 仅在 `exited_zero` 之后；标签 ≥ 运行标签 |
| 运行检查点（`run.checkpoint`） | `Scheduler.storeCheckpoint`，在 IF-2 `checkpoint` 通知到达时 | 重试与流水线续跑（输入 `resume`）、任何客户端 | 运行进行中立即入库，按报告顺序逐个入库，运行失败也保留；路径必须是运行 `work/` 中的普通文件（不是符号链接）；运行在全部检查点入库后才结束 |
| 流水线 | `Pipelines`（`pipeline.*` 事件） | API、UI、CLI | 都是事件；重启后由 `Pipelines.recover` 继续 |
| 运行日志（`run.log`） | `Scheduler.execute` | 任何客户端 | 每次运行都保存，与执行状态无关 |
| 上传（`PUT /v1/artifacts`） | `KernelServer`，仅限所有者令牌 | — | 外部令牌被拒绝并记事故 |
| 运行工作区 `runs/<id>/` | `Scheduler.execute` 创建并删除 | 插件（`in/` 只读） | 每次运行后删除；启动时清空整个 `runs/` |
| 插件进程与容器 | `PluginProcess`（独立进程组）、`podman run --rm --name=llmchip-run-<id>` | — | 结束、取消、超时或内核停止时杀掉；遗留进程在下次启动时杀掉 |
| 判定、约束、项目、迁移记录 | `Workflow` | API | 都是事件；判定从不删除，失败的判定一直保留 |
| 事故 | `Incidents.open` / `close`，由 `Policy`、`Workflow`、`EgressGateway`、`AgentRuntime` 调用 | UI、API | 关闭时必须给出原因和措施（FR-AGT-27） |
| 出口决定 | `EgressGateway.check`，在每次 LLM 请求和每次 MCP 下载之前 | 审计 | 每个请求一条 `egress.decided` |
| 切换到其他提供方时的接手包 | `AgentRuntime.switchProvider` 自行比较接手包的标签与提供方上限 | API | 接手包存为 `agent.handoff`；切换被拒时打开 `handoff_blocked` 事故，不产生 `egress.decided` |
| 任务、智能体步骤、变更集 | `AgentRuntime` | API、UI | 状态都在事件中，新会话可从日志重建（接手） |
| 密钥 | `SecretStore.set` / `remove`（`secret.changed` 只含名称） | 提供方通过 `secret(name)` 读取 | 值从不进入事件或日志（`Redactor`） |
| `run/harnessd.json`、锁 | `Kernel.start` / `shutdown` | CLI、MCP 桥、测试 | 关闭时删除；已退出进程留下的锁会被接管 |

按写入者划分的事件种类：

- Kernel：`kernel.started/stopped`、`projection.rebuilt`
- Scheduler：`kernel.paused`、`run.*`（含 `run.checkpointed`）
- Pipelines：`pipeline.started`、`pipeline.node`、`pipeline.resumed`、`pipeline.finished`
- ArtifactStore：`artifact.*`
- SecretStore：`secret.changed`
- PluginHost：`plugin.*`
- Workflow：`verdict.*`、`project.*`、`stage.*`、`constraint.*`、`migration.*`（`approval.*`、`waiver.recorded`、`package.exported` 只是历史，没有写入者）
- Incidents：`incident.*`
- EgressGateway：`egress.*`、`declass.*`
- AgentRuntime：`task.*`、`agent.step`、`changeset.*`

每个负载在计算哈希前都按其 schema 定义（`KERNEL_EVENT_PAYLOAD_DEFS`）校验，未知种类会被拒绝。

### 4.2 取消与关闭

- **单次运行**：`runs.cancel` → `Scheduler.abort`。调度器发送 IF-2 `cancel` 通知（SDK 会置位 `ctx.signal` / `ctx.cancelled`），对容器执行 `podman kill`，向进程组发 SIGTERM，`cancelGraceMs` 后发 SIGKILL。尚未开始的排队运行直接结束为 `cancelled`。
- **紧急停止**（FR-AGT-23）：`kernel.pause` 追加 `kernel.paused`。之后不再启动新运行，智能体循环也会暂停；正在进行的运行继续。`kernel.resume` 撤销暂停。
- **内核停止**（`Kernel.shutdown`）按固定顺序执行：
  1. 删除运行时文件；
  2. 关闭服务；
  3. 停止流水线的推进（`Pipelines.stop`：此时被中断的运行不判为失败，由下次启动的 `Pipelines.recover` 重新运行），再停止调度器（把所有运行中止为 `interrupted`，并等待各自记录结果）；
  4. 按加载的逆序、带超时地卸载内核内插件；
  5. 追加 `kernel.stopped`；
  6. 关闭数据库并释放锁。

  `SIGINT`/`SIGTERM` 和 `kernel.shutdown` 都走这条路径。智能体运行时没有被显式停止（§7.3 的 C7）。
- **智能体任务**在以下情况停下：紧急停止；达到 `maxSteps`/`maxTokens`（打开 `budget_exceeded` 事故）；同一失败调用重复 `loopThreshold` 次；出口被拦截；LLM 出错。停下的任务处于 `paused`，可用 `tasks.run` 继续。

---

## 5. 扩展方式

每个做法都给出代码中已经这样做的真实示例，以及证明它的测试。扩展前，先确认能否用清单、流程包或数据文件表达这次变更。修改内核代码应是最后手段（AP-2）。

### 5.1 进程外插件（新工具或新领域步骤）

示例：`estimator`（[清单](../plugins/estimator/llmchip-plugin.json)、[入口](../plugins/estimator/src/llmchip_estimator/__main__.py)）。最小示例是 [stub-tool](../tests/fixtures/plugins/stub-tool)（TypeScript）和 [container-stub](../tests/fixtures/plugins/container-stub)（在 Podman 中运行的 Python）。

1. 编写 `plugins/<id>/llmchip-plugin.json`（schema `PluginManifest`）：
   - `id`、`version`、`kinds`、`kernelApi: "^0.3"`。
   - `runtime`：`process`、`container` 或 `data`。
   - `entry.command`，其中可以使用 `${python}`、`${node}`、`${pluginDir}`。容器插件还需要 `entry.image`，按摘要固定版本。
   - `handlers[]`。每个处理器包含：`inputs`（名称、产物 `types`、`required`、`multiple`）；`outputs`（名称、`type`、`mediaType`）；带默认值的 JSON Schema `params`；可选的 `timeoutMs`。
   - 可选的 `capabilities`，并为其指定资格认证处理器。
2. 实现处理器。
   - Python：调用 `run_plugin("<id>", "<version>", {"<handler>": fn})`。`fn(ctx)` 读取 `ctx.input(name).path` 和 `ctx.params`，把文件写到 `ctx.out_dir` 下，返回 `Result(outputs=[Output(name, path, type)], metrics={…}, parse={"status": "complete", "parser": "<id>/<v>"})`。长循环中要调用 `ctx.check_cancelled()`。
   - TypeScript：`runPlugin({id, version, handlers})` 返回同样的结构，处理器要关注 `ctx.signal`。
   - 两者共同的要求：
     - stdout 属于协议。SDK 会把 `print` 与 `console.*` 重定向到 stderr，stderr 进入运行日志。
     - 如实报告 `parse.status`：`complete`；报告没有读完整时用 `incomplete`；或者 `failed`。schema `ParseStatus` 不允许其他值。
     - 绝不返回判定。
3. 只针对 Python `process` 插件：把插件加入 `pyproject.toml` 的 `[tool.uv.workspace].members` 和根 `dependencies`，再执行 `uv lock && yarn setup:python`。容器插件不是 workspace 成员，内核会把 Python SDK 挂载到容器内的 `/llmchip/sdk`。
4. 测试：
   - 插件旁的单元测试 `plugins/<id>/tests/test_*.py`，由 `yarn check:python` 收集。
   - 在 `tests/` 中经内核的端到端测试，用 `startKernel(undefined, {config: {plugins: {searchPaths: [...]}}})` 启动内核。可参照 [tests/runs.spec.ts](../tests/runs.spec.ts) 对 `stub-tool` 的做法，以及 [tests/containers.spec.ts](../tests/containers.spec.ts) 对 `container-stub` 的做法。
   - 需要镜像的测试在镜像缺失时会可见地跳过（`podman image exists`）。
5. 运行 `yarn llmchip plugins list`，注册问题会以 `problem:` 行显示。再运行 `yarn llmchip run start <id> <handler> --input …`。

已手工验证：先运行 `yarn harnessd serve --plugins tests/fixtures/plugins`，再依次用 `violations` 为 0、`violations` 为 3、`truncate` 执行 `run start stub-tool report --input design=<hash>`。`verdict evaluate stub.drc <run>` 分别得到 `pass`、`fail`（`CRITERION_FAILED`）和 `indeterminate`（`PARSE_INCOMPLETE`）。

### 5.2 检查或阶段准入检查

示例：阶段准入检查 `impl.views.S4`。它在 [flow-default/flow.json](../plugins/flow-default/flow.json) 中定义，判定 `estimator/check_views` 的运行，并以 `entry.checks: [{check: "impl.views.S4", slot: "storage-impl"}]` 挂在阶段 S4 上。另一个示例是 S0 的检查 `estimate.feasibility`。

1. 让处理器报告检查需要的指标，不做任何判定。
2. 在流程包的 `checks` 中加入检查：
   - `id`、`version`、`plugin`、`handler`。
   - `completeness`：结果有意义的前提，不满足即判 `indeterminate`。
   - `criteria`：决定通过或失败。带 `when`（对另一个指标的条件）的判据只在条件成立时适用；条件不成立时，判定记录 `CRITERION_NOT_APPLIED`；条件指标缺失时，判定为 `indeterminate`。处理器要显式报告条件（示例：`arch.spec` 在 `cycles_target_set` 为真时判定 `cycles_within_target`；`test-flow` 中的 `stub.limit`）。
   - 可选的 `requiredCapability`、`requireParse` 和 `acceptExitCodes`。

   标准一旦改变就修改 `version`，这是检测降低标准的依据（AC-M1-13）。
3. 若它要阻止进入某个阶段，就在该阶段的 `entry.checks` 中引用它，并指定判定所针对的槽位；运行的输入必须就是绑定在该槽位的产物，否则判定不针对它。没有审批门，也没有豁免：检查失败就一直是失败，直到改正后重新运行。
4. 多个流程包可以定义同一检查 id，但所有定义必须完全相同。否则冲突的流程包不会被加载，内核日志会记录一个指明该检查的 `FLOW_INVALID` 问题（`plugins list` 不显示流程包的问题）。流水线节点只能判定定义在自己插件与处理器上的检查（判定记录在该节点的运行上），否则流程包在加载时被拒绝；阶段入口检查或必需测试引用了无人定义的检查，同样被拒绝（测试：[tests/flow-packs.spec.ts](../tests/flow-packs.spec.ts) 覆盖内置流程包上的这条不变式，[tests/workflow.spec.ts](../tests/workflow.spec.ts) 覆盖无法被判定的流程包被整体拒绝）。
5. 测试：
   - [tests/workflow.spec.ts](../tests/workflow.spec.ts) 用 `test-flow` 验证判定语义，以及失败不会被豁免。
   - 内置流程包整体加载，每个节点只判定自己处理器上的检查，且它们用到的插件都已注册：[tests/flow-packs.spec.ts](../tests/flow-packs.spec.ts)；无法被判定的流程包会被拒绝：[tests/workflow.spec.ts](../tests/workflow.spec.ts)。
   - [tests/estimator.spec.ts](../tests/estimator.spec.ts) 覆盖缺数据时的 S0 检查。
   - [tests/calibration.spec.ts](../tests/calibration.spec.ts) 覆盖按视图的阶段准入（需 EDA 镜像）。

新的流程包是带 `flow.pack` 数据文件的 `data` 插件，参见 [test-flow](../tests/fixtures/plugins/test-flow)。项目用 `projects.create {flow}` 选择流程包。

### 5.2a 阶段的研发流水线与必需测试

示例：[flow-default/flow.json](../plugins/flow-default/flow.json) 的阶段 S2：`develop` 中 `quantize` 把 `spec`、`weights` 绑定到 `numeric-spec`、`l2-weights`，`evaluate` 用 `node:quantize.spec` 等输入并判定 `accuracy.budget`，`infer` 把 `trace` 绑定到 `l2-trace`；`tests` 对绑定的槽位判定 `accuracy.budget`。最小示例是 [test-flow](../tests/fixtures/plugins/test-flow/flow.json) 的阶段 A 与 B。

1. 在阶段中写 `develop`：`defaults`（槽位 → `data:` 或 `slot:` 引用，只在槽位为空时绑定）与 `nodes`。每个节点有 `id`、`plugin`、`handler`、可选的 `params`、`inputs`（输入名 → 引用或引用数组）、`after`、`bind`（输出名 → 槽位）、`judge`（检查 id）、`beforeEntry`（在进入阶段之前运行，供准入检查判定）和 `timeoutMs`。
2. 在阶段中写 `tests`：每项是 `check` 与其运行的 `inputs`、`params`。处理器取自检查定义；已有同样参数与输入的成功运行时复用它，所以研发流水线之后运行测试不会重复计算。
3. 引用只能指向本步骤前面的节点（`node:`）、槽位（`slot:`）或插件数据（`data:`）；跨步骤的数据经槽位传递。引用了未声明节点的流程包在展开时以 `PIPELINE_INVALID` 失败。
4. 参数改变了检查所判定的标准时，按 §5.2 修改检查的 `version`；流水线本身没有版本。
5. 测试：照 [tests/pipelines.spec.ts](../tests/pipelines.spec.ts)（语义）与 [tests/pipelines-flow.spec.ts](../tests/pipelines-flow.spec.ts)（真实插件，在全新项目上每一步都要 `ready`、通过，且测试复用研发运行）。

### 5.2b 处理器的资源需求与检查点

示例：[stub-tool](../tests/fixtures/plugins/stub-tool/llmchip-plugin.json) 的 `heavy`（`resources: {memGb: 3}`）、`gpu`（`gpu: 1`）与 `steps`（检查点与续跑，[plugin.ts](../tests/fixtures/plugins/stub-tool/plugin.ts)）。

1. 资源：在处理器清单中写 `resources: {cpu, memGb, gpu}`。不写时，进程插件按 1 核、1 GiB，容器插件按调度器的 `containerCpus`/`containerMemory`，都没有 GPU。容器运行的 `--cpus`、`--memory` 取这个需求。同一时刻只有一个运行使用 GPU（需求 NFR-15），只有它看得到 GPU（`CUDA_VISIBLE_DEVICES`）；容器插件不分配 GPU。主机容量取配置 `scheduler.resources`，未写的项自动探测（CPU 核数、内存总量、`nvidia-smi -L`）。
2. 检查点：处理器把检查点完整写入工作目录（先写临时文件再改名），再调用 `ctx.checkpoint(path, name)`（Python）或 `ctx.checkpoint(path, name)`（TypeScript），`path` 相对于 `work/`。
3. 续跑：在清单中声明可选输入 `resume`（类型 `run.checkpoint`），并在处理器开头读 `ctx.resume`。重试（`maxAttempts` > 1）、流水线在内核重启后重新运行被中断的节点、以及 `pipelines.resume` 重新运行同样输入的节点时，内核传入最近的检查点。
4. 测试：照 [tests/resources.spec.ts](../tests/resources.spec.ts) 与 [tests/checkpoint.spec.ts](../tests/checkpoint.spec.ts)；Python SDK 的通知格式由 `python/llmchip-sdk/tests/test_protocol.py` 覆盖。

### 5.3 工艺、实现库或模板数据

示例：[process-library](../plugins/process-library/llmchip-plugin.json)。在 `data/` 下加文件，并在 `data[]` 中加一条（`id`、`type`、`path`、`label`）。每个数值都要带来源与置信度（见 AGENTS.md）。使用方通过 `plugins.list` 按类型和 id 找到数据，CLI 的 `estimate` 命令就是这样做的。测试：[tests/estimator.spec.ts](../tests/estimator.spec.ts)；数值本身由 `plugins/estimator/tests/test_estimate.py` 覆盖。

### 5.3a 图还原的架构家族

示例：[graph.py](../plugins/model-llama/src/llmchip_model/graph.py) 中的 `qwen3`。

1. 在 `HF_TYPES` 中把模型的 `model_type` 映射到家族；张量名写进名称表（`HF_NAMES`、`LLAMA2C_NAMES`）。
2. 生成该家族解码层的节点，并把每一处差异记录为一条判断及其依据（config 字段或张量）。
3. 如果需要解释器还没有的运算，在 [execute.py](../plugins/model-llama/src/llmchip_model/execute.py) 的 `run_node` 中加上。
4. 张量名是新的，就在 [census.py](../plugins/model-llama/src/llmchip_model/census.py) 的 `ROLE_PATTERNS` 中加入，确保普查仍能识别每个张量。
5. 用 transformers 构造一个随机小模型测试，参照 [plugins/model-llama/tests/test_graph.py](../plugins/model-llama/tests/test_graph.py) 的 `test_recovered_graph_matches_transformers`：logits 相对误差在 1e-4 以内，贪心生成的 token 完全一致。
6. 在 [views/steps/S1.tsx](../apps/web/src/views/steps/S1.tsx) 和两个语言目录中给新节点和判断起可读的名字。

家族不支持的情况必须返回 `supported: false` 和原因码，绝不给出近似的图。

### 5.3b 数值模式（舍入、溢出、累加器）

示例：`half-even` 舍入。模式是芯片行为，所以有四份实现，必须逐位一致：

1. [arith.py](../plugins/numeric/src/llmchip_numeric/arith.py)（`l2.py` 和 `graph_l2.py` 使用）以及 `plugins/numeric/llmchip-plugin.json` 中 `quantize` / `quantize_graph` 的参数枚举；`spec.py` 把它连同定义写进规格。
2. [isa.py](../plugins/rtl-taalas/src/llmchip_rtl/isa.py) 中自包含的副本（它随 RTL 包一起发布），由 `modes(params)` 选择。
3. [compiler.py](../plugins/rtl-taalas/src/llmchip_rtl/compiler.py) 中 `Compiled._widths` 输出的宏（列在 `bundle.py` 的 `flags` 中），以及 [llmchip_functions.vh](../plugins/rtl-taalas/src/llmchip_rtl/rtl/llmchip_functions.vh) 中的 `ifdef` 分支。
4. `plugins/sim-verilator/cocotb_alu.py` 中的 ALU 单元测试，它从 RTL 包参数读取模式。

测试：`test_rounding_modes_match_exact_arithmetic`（与精确有理数比较），`plugins/rtl-taalas/tests/test_rtl.py` 中的 `test_isa_rules_are_the_numeric_spec_rules` 和 `test_every_numeric_scheme_compiles_to_the_same_bits_as_l2`，以及 `tests/numeric-schemes.spec.ts` 的 RTL 部分（Verilator 与 L2 对比，ALU 按该模式测试）。注意 Verilog 的有无符号：一个无符号操作数会让整个表达式变成无符号，`>>>` 变成逻辑移位（偶数舍入的第一版就因此在 9920 个随机 ALU 向量上出错）。

### 5.3c 引擎的微架构选项

示例：`memoryInterface: sqi`（[llmchip_sqimem.v](../plugins/rtl-taalas/src/llmchip_rtl/rtl/llmchip_sqimem.v)）。选项只改变周期和面积，不改变结果：

1. 在 `Compiled(options)` 中接受它，并由 `_widths` 输出一个宏（列在 `bundle.py` 的 `flags` 中）；`arch-taalas` 把它作为 `design` / `explore` 的参数，写进芯片规格的 `dataflow`，`rtl-taalas generate` 从那里读取。
2. 在 RTL 中用这个宏实现它，每个寄存器都有复位值；指令集和 L2 不变。
3. 在 [perf.py](../plugins/rtl-taalas/src/llmchip_rtl/perf.py) 中为它建周期模型：逐状态遍历（`fast=False`）逐个状态对应 RTL；长循环的闭式计算必须与之相等（`test_cycle_model_closed_form_equals_the_state_walk`）。
4. 把它加入 [tests/arch-perf.spec.ts](../tests/arch-perf.spec.ts) 的 `OPTIONS`：RTL 每一步的忙碌周期必须等于芯片规格的预测，RTL 必须与 L2 一致。
5. 实测它的面积：综合并在晶粒上做物理实现（`eda-gf180 implement`）。工艺库的面积估算不够（§7.2）。

### 5.3d 空间引擎的算子

示例：空间核心中的 SiLU 门（[generator.py](../plugins/rtl-taalas/src/llmchip_rtl/spatial/generator.py) 中的 `core_verilog`）。

1. 在 `Spatial.schedule()` 中加一个阶段，写明它的精确周期数；生成器为每个阶段输出一个状态，所以这个时序表就是周期模型。
2. 在 `core_verilog` 中输出它的逻辑：运算写成连续赋值（时钟块中不调用带局部变量的函数），常量写成 `sp_rom_*` 表模块，乘法用 `sp_mul` 实例，每个寄存器都有复位值。lint 必须保持零警告。
3. 在 [sp_model.py](../plugins/rtl-taalas/src/llmchip_rtl/spatial/sp_model.py) 中对应实现，并保持 `test_the_model_of_the_circuits_equals_l2` 通过。
4. 运行 [tests/spatial-flow.spec.ts](../tests/spatial-flow.spec.ts)：RTL 与 L2 逐位一致，每一步的周期等于时序表。算子增加了逻辑时，用综合重新实测面积，并更新 `spatialCells`。

### 5.3e 开源基准

示例：GSM8K 用自定义的 lm-eval 任务 `GSM8K_QWEN`（[bench.py](../plugins/numeric/src/llmchip_numeric/bench.py)），其他选择题与生成题基准用 lm-eval 自带的任务，代码题用 EvalPlus。

1. 数据：在 [benchdata.py](../plugins/numeric/src/llmchip_numeric/benchdata.py) 的 `CHOICE`（每科 5 题入快速集）、`GENERATION` 或 `CODE`（每个基准 5 题）中加入基准与它的 lm-eval 组名或题目文件，并写许可证。`pack` 从本机的 Hugging Face 缓存读出 lm-eval 任务实际用到的切分；只能离线读取的数据（如 CMMLU 的 CSV）像 `_cmmlu_loader` 那样转换。
2. 判对错：在 `bench.METRIC` 中写该基准判对错用的 lm-eval 指标与过滤器；自定义任务写成配置字典，由 `task_specs` 返回。
3. 检查：在 [flow-six](../plugins/flow-six/flow.json) 中为快速集与完整集各加一项 `spec.accuracy(.quick).<基准>`，题数取 `benchmark_data` 的指标，允许下降由负责人定（需求 FR-QNT-04）。
4. 测试：照 [test_bench.py](../plugins/numeric/tests/test_bench.py)（比较与 L2 的作答）与 [tests/qwen-spec.spec.ts](../tests/qwen-spec.spec.ts)（真实模型，须显式开启）。

### 5.4 内核 API 方法

示例：`kernel.pause`（FR-AGT-23）。可以按以下各层追踪：

1. Schema：在 [kernel.schema.json](../packages/kernel-api/schemas/kernel.schema.json) 中定义 `PauseParams`、`PauseResult`，然后执行 `yarn gen:schemas`。忘记生成时，`yarn check:generated` 会失败。
2. 契约：在 [kernel-api/src/index.ts](../packages/kernel-api/src/index.ts) 的 `KernelMethods` 与 `KERNEL_METHOD_DEFS` 中加入 `'kernel.pause'`。`packages/kernel-api/tests/index.spec.ts` 检查被引用的定义都存在。
3. 处理函数：在 `Kernel.handlers()` 中加一行，调用组件（`Scheduler.setPaused`）。有 `MethodHandlers` 类型约束，漏写处理函数会成为类型错误。
4. 策略：只有允许外部客户端调用时，才把方法加入 [security/policy.ts](../packages/kernel/src/security/policy.ts) 的 `EXTERNAL_METHODS`。`kernel.pause` 仅限所有者。
5. 前端：CLI 分支（`kernel pause`）和 UI 控件（[Settings.tsx](../apps/web/src/views/Settings.tsx)）。没有专用命令的方法也都可以用 `llmchip call <method> '<json>'` 调用。
6. 测试：[tests/runs.spec.ts](../tests/runs.spec.ts) 的 "holds new runs while paused and resumes them"，以及 [tests/agents.spec.ts](../tests/agents.spec.ts) 的 "stops agents at the emergency stop and at their budget"。

新的事件种类按第 1–2 步定义其负载（`KernelEventPayloads`、`KERNEL_EVENT_PAYLOAD_DEFS`）。`EventStore.registerKind` 把一个种类对应到已有的 schema 定义，只有内核代码能调用它；进程外插件不能定义事件种类。

### 5.5 投影（新的读模型）

示例：[runs/scheduler.ts](../packages/kernel/src/runs/scheduler.ts) 中的 `runsProjection`，以及 `incidentsProjection`。

1. 实现 `Projection {name, version, setup, reset, apply}`。
2. 在所属组件的构造函数中，用 `events.registerProjection` 注册它。
3. `setup` 或 `apply` 有任何改动，就提升 `version`。下次启动时，该投影会从整条日志重建。

测试：[packages/kernel/tests/events.spec.ts](../packages/kernel/tests/events.spec.ts) 的 "rebuilds a projection from the log with identical state"。

### 5.6 LLM 提供方种类

示例：`createProvider`（[agents/providers.ts](../packages/kernel/src/agents/providers.ts)）中的 `openai-compatible` 与 `anthropic`。

1. 在 schema 的 `LlmProviderConfig` 中加入新种类。
2. 加一个 `case`，在其中构造请求；任何字节发出之前，都必须先经过 `send` / `gateway.check`。
3. 只通过 `secret(config.apiKeySecret)` 读取密钥。

测试时参照 [tests/providers.spec.ts](../tests/providers.spec.ts)，对本地模拟服务器测试。智能体测试使用 `scripted` 种类。

### 5.7 智能体工具或 MCP 工具

- **智能体工具**：在 `TOOLS` 中加一个 `ToolSpec`，并在 `AgentRuntime.execute`（[agents/runtime.ts](../packages/kernel/src/agents/runtime.ts)）中加一个 `case`。先检查任务授权（`writeScope`、`runs`）；`deny()` 会记录事故。返回的 `ToolResult` 要带上内容的真实标签。测试：[tests/agents.spec.ts](../tests/agents.spec.ts) 的 "blocks out-of-scope writes, deletions, shell commands and ungranted runs, recording incidents"。
- **MCP 工具**：在 [cli/src/mcp.ts](../packages/cli/src/mcp.ts) 的 `TOOLS` 中加一个 `Tool`，通常是 `run: rpc('<method>')`。对应的方法必须在 `EXTERNAL_METHODS` 中，否则内核会拒绝并记录事故。测试：[tests/mcp.spec.ts](../tests/mcp.spec.ts)。

### 5.8 Web UI 页面或文本

示例：[views/Estimator.tsx](../apps/web/src/views/Estimator.tsx)。

1. 在 [App.tsx](../apps/web/src/App.tsx) 中，把页面加入 `Page` 联合类型和 `PAGES`，并加上它的渲染分支。
2. 把每个文本键（包括 `nav.<page>`）加入 [i18n.ts](../apps/web/src/i18n.ts) 的两个语言目录；内核消息码加入 `CODES`。
3. 只使用 `KernelClient` 调用内核。
4. 把页面加入 [tests/web-ui.spec.ts](../tests/web-ui.spec.ts) 中的 `PAGES` 列表，测试自己维护着一份。

该测试检查两种语言目录的键一致、没有未知键。它还在无头 Chromium 中以两种语言渲染列表中的每个页面，页面上原样出现任何目录键都会失败。运行前先执行 `yarn build`，确保 `apps/web/dist` 存在。

“演示流程”页面（[views/Flow.tsx](../apps/web/src/views/Flow.tsx)）每个阶段显示什么，都在 [views/flowSteps.ts](../apps/web/src/views/flowSteps.ts) 中声明。要在那里显示某个处理器的结果：

1. 把它的 `plugin/handler` 加入该阶段的 `runs`。被某阶段的检查判定过的运行，无论处理器是什么，都显示在那个阶段下。
2. 把要显示的指标加入 `facts`：标签键、格式化函数，以及可选的 `good`（决定结果块的颜色）。
3. 把用户需要打开的输出加入 `deliverables`。
4. 在 `RUN_TITLES` 和 `CHECK_TITLES` 中给处理器和检查起可读的名字。右侧面板的动态（[views/activity.ts](../apps/web/src/views/activity.ts)）使用同一套名字。

示例：S6 的“核心区面积”和“布局布线后时序”读取 `eda-gf180/implement` 的 `core_area_um2` 和 `flow_setup_violations`。

阶段还可以有详细演示视图，从卡片上的“详细演示”按钮打开。把它写成 `views/steps/<阶段>.tsx`，并在 `Flow.tsx` 的 `DETAILS` 中注册；打开的详细视图记在 `selection.stage` 中，因此左侧栏会在选中的项目下列出它，返回栏（以及浏览器的后退按钮）能回到它，就像回到之前访问过的任何页面和产物一样。示例：[views/steps/S0.tsx](../apps/web/src/views/steps/S0.tsx) 读取该阶段的 `estimate.report`，依次展示问题与回答、输入、面积与速度的计算、反推能放下的模型、检查结论，以及一个在项目之外重新运行估算器的表单。说明文字要由结构化字段生成，不要直接用插件的自由文本，这样两种语言读起来都自然。`tests/web-ui.spec.ts` 会以两种语言从流程页和左侧栏打开 S0、S1、S2 视图，并用返回栏返回。[views/steps/S2.tsx](../apps/web/src/views/steps/S2.tsx) 逐节解释数值规格，通过 `modelPipeline.ts` 的 `runScheme` 运行方案，对比所有评估过的方案，并用 `adoptScheme` 把芯片路径的方案设为正式规格（绑定 `numeric-spec` 与 `l2-weights` 槽位、判定 `accuracy.budget`、运行 `infer`）。

### 5.9 内核内系统插件（Cordis）

1. 实现 `InKernelPlugin {id, inject, provides, apply(ctx, kernel)}`（[plugin-host.ts](../packages/kernel/src/plugin-host.ts)）。
2. 只通过 `PluginHost.load` 加载。
3. 所有注册都作为 Cordis effect，以便卸载时清理。

目前只有 `KernelOptions.plugins` 向宿主提供插件，而只有测试会传入它（[packages/kernel/tests/plugin-host.spec.ts](../packages/kernel/tests/plugin-host.spec.ts)）。要让产品插件在内核内运行，还需要新增生产环境的加载路径。这是对架构 §5 防护措施的改动，须先与负责人商定。

### 5.10 测试环境的虚拟芯片

示例：玩具进程芯片 [tests/chips/toy_chip.py](../python/llmchip-vchip/tests/chips/toy_chip.py)，它提供一个玩具模型，或包装一个已有芯片并加上周期模型；由 [tests/test_harness.py](../python/llmchip-vchip/tests/test_harness.py) 中的 `test_process_chip_wrapping_l2_is_bit_exact_with_the_numeric_golden` 和各故障测试证明。

1. 建立芯片目录并编写 `chip.json`（`format: llmchip-vchip/1`；`model.context`、`model.vocab`、`tokenizer`、`host`、`capabilities`、`clock_mhz`；见 README“A chip”）。硬件用 `backend: process` 加 `command`；进程内的 Python 适配器用 `entrypoint: "module:Class"`。
2. 实现 `VirtualChip`（`metadata`、`reset`、`prefill`、`step`、`close`；[interface.py](../python/llmchip-vchip/src/llmchip_vchip/interface.py)）。token 或位置越界时抛出 `ChipError("invalid_input", …)`。进程芯片调用 `llmchip_vchip.serve.serve(chip)`；RTL 仿真也可以自己实现 [adapters/process.py](../python/llmchip-vchip/src/llmchip_vchip/adapters/process.py) 的 JSON 行协议，并在自己模拟主机帧时返回 `host_cycles`。
3. 制作黄金芯片：用同一模型与数值规格执行 `vchip new numeric`，并在芯片的 `policy` 中写明（`golden`、`quality.bit_exact: true`），再写上用于一致率的 `reference` 和速度规则（`speed.tok_s_min`）。
4. `vchip conform <chip>` 必须通过；之后 `vchip bench <chip>` 按其策略判定。

新增内置后端：在 `llmchip_vchip/adapters/` 中加一个类并登记到 `registry.BACKENDS`，测试方式与参考、数值后端相同（`test_conformance_of_the_baseline_chips`）。

经内核运行时，芯片不是目录：插件 `vchip` 从项目产物构建芯片（§2.4），因此 `chips_from_artifacts` 接受新生成器的架构后，它的 bundle 就能在那里运行。由 [plugins/vchip/tests/test_plugin.py](../plugins/vchip/tests/test_plugin.py) 与 [tests/vchip.spec.ts](../tests/vchip.spec.ts) 证明。

速度必须可信的硬件芯片（设计中的 L3）则在测试环境自己的 testbench 桥下运行：真实示例是 `rtl`（[adapters/rtl.py](../python/llmchip-vchip/src/llmchip_vchip/adapters/rtl.py)、[bridges/rtl_bridge.py](../python/llmchip-vchip/src/llmchip_vchip/bridges/rtl_bridge.py)、[bridges/tb/vchip_tb.cpp](../python/llmchip-vchip/src/llmchip_vchip/bridges/tb/vchip_tb.cpp)），由 `vchip new rtl` 生成，由 [tests/test_rtl_vchip.py](../python/llmchip-vchip/tests/test_rtl_vchip.py) 证明。任何带有 bundle 的 `tb_top` 端口（clk、rst_n、主机 SPI 引脚、busy）并实现 START/STATUS/READ 协议的 RTL bundle 都能在它下面运行；新的生成器只需写出这样的 bundle。只有自己在引脚上计数周期的桥，才能加入 `metrics.TESTBENCH_BACKENDS`。

---

## 6. 验证方法

### 6.1 命令

以下结果于 2026-10-19 在开发机上测得。环境：WSL2、Node 24.22、uv 0.13、Podman 3.4，已有固定版本的 `iic-osic-tools` 镜像。

| 命令 | 验证什么 | 实测 |
|---|---|---|
| `yarn check:workspace` | 包、引用、`workspace:*` 依赖、vendor 清单与许可证 | "5 packages, 9 vendored packages" |
| `yarn check:generated` | `kernel-api/src/generated` 与 schema 一致 | "up to date (2 files)" |
| `yarn check:python` | ruff lint 与格式检查，SDK、测试环境和全部 Python 插件的 pytest | 306 passed、1 skipped，有 EDA 镜像、沙箱镜像与 GPU 时 17 分 49 秒（2026-10-26，R5）；R1 时 165 passed、1 skipped，6 分 43 秒；R0 时 164 passed、1 skipped（R0 删除了 `packager` 的测试）；此前 158 passed、1 skipped，4 分 52 秒（2026-10-22）；其中 32 个属于测试环境，7 个是其 L3 仿真测试（无镜像时可见地跳过；跳过的那个是检查无镜像时行为的测试） |
| `yarn typecheck` | 先构建项目引用，再检查源码、测试、脚本与 Web UI 的类型 | 通过 |
| `yarn build` | 各包的 `lib/`，以及 `apps/web/dist` | 通过 |
| `yarn test` | 全部 Vitest 用例（见下表） | 36 个文件通过、2 个跳过（m4-flow、qwen-spec）；137 个测试通过、3 个跳过（m4-flow、`pipelines-flow` 的 S0–S7 与 qwen-spec，都须显式开启；2026-10-30） |
| `yarn verify` | 依次执行以上全部；每次提交前必须通过 | 退出码 0（2026-10-24）；镜像齐全时，Python 检查 9 分 15 秒、Vitest 769 秒（R3） |
| `LLMCHIP_FULL_FLOW=1 yarn test tests/m4-flow.spec.ts` | 占位模型上完整的 S0–S7，以及从 GDS 到权重的血缘 | 见 §6.3 |
| `yarn llmchip events verify` / `artifact verify` | 哈希链；已存储内容与哈希一致 | `ok: N … checked` |
| `yarn vchip bench CHIP [--reference R] [--golden G]` | 用测试环境测一颗芯片：在提示词集上生成，质量与速度，按芯片策略判定，写出运行目录 | stories260K 的数值（int8）芯片对照其参考芯片，20 个提示词生成到 64 token 上下文结束：下一 token 一致率 0.992，生成完全相同 0.65，困惑度 3.167（参考 3.159），11 秒（2026-10-22）。`vchip conform` 在 stories260K 的参考与数值芯片、tiny Qwen3 的参考芯片上均通过 |
| 先 `yarn vchip new rtl --proxy --id spatial-5k`，再 `vchip conform` / `vchip bench spatial-5k` | 设计中的 L3：5K 占位模型的 `taalas-spatial/1` RTL 在引脚级 testbench 下运行，对照其 L2 黄金芯片与浮点参考芯片 | Verilator 构建 8 秒；`conform` T-01–T-19 全部通过（T-06 不适用），12.8 分钟，`--quick`（不含 T-18/T-19）约 2 分钟；`bench` 20 个提示词生成到 64 token 上下文结束：与 L2 逐位一致（token、logits、全部观测点），每个位置都是 1,327 周期/token，主机 753 周期，8 MHz 下 3,846 tok/s，测量可信，判定通过，10.6 分钟（2026-10-22） |
| `yarn demo [--until S0..S7] [--arch spatial]` | 在运行中的内核上，把项目 `demo` 按完整流程的正向路径跑一遍，到给定阶段为止，供“演示流程”页面展示 | R0 之前（含 S8 与审批）S0–G4 全部通过，2241 秒（约 37 分钟），容器配置 16 CPU / 12 GB；R0 之后尚未重新完整运行 |
| `yarn demo --model PATH [--bits N] [--process ID]` | 从已下载的模型建立项目：S0（导入、普查、估算）与 S1（图还原、L0、对拍） | 普查、图还原、对拍检查全部通过：SmolLM2-135M 11 秒、Qwen3-0.6B 46 秒、DeepSeek-R1-Distill-Qwen-1.5B 89 秒、stories260K 1 秒；logit 最大相对误差 6.8e-6，贪心 token 完全一致；在 2.4 mm² 的 GF180 芯片上均不可行 |

没有容器镜像时，`calibration`、`m3-flow`、`ui-latency`、`containers` 会可见地跳过。这时 `yarn verify` 快得多，但证明的东西也少得多。跳过不等于通过（DP-10），报告时要说明跳过了哪些测试。

### 6.2 各测试证明什么

| 用例 | 行为 | 需求 / AC | 运行条件 |
|---|---|---|---|
| `packages/kernel-api/tests/index.spec.ts` | schema 能按严格的 JSON Schema 2020-12 编译；每个方法与负载引用的定义都存在；标签顺序 | IF-1、IF-2 契约 | 总是 |
| `packages/kernel/tests/events.spec.ts` | 哈希链、篡改检测、原子事务、脱敏、投影重建与追赶 | K1、FR-TRC-01 | 总是 |
| `packages/kernel/tests/artifacts.spec.ts` | 内容寻址、幂等、标签提升、血缘、标记删除、校验、临时文件清理 | K2、FR-TRC-02、FR-SEC-02 | 总是 |
| `packages/kernel/tests/config.spec.ts` | 配置分层、密钥加密、脱敏 | K10、NFR-07 | 总是 |
| `packages/kernel/tests/plugin-host.spec.ts`、`tests/vendor.spec.ts` | Cordis 宿主防护：挂起或抛异常的插件、卸载顺序 | ADR-001 | 总是 |
| `packages/kernel/tests/main.spec.ts`、`tests/cli.spec.ts` | `harnessd` 与 `llmchip` 命令行 | FR-INT-07 | 总是 |
| `tests/kernel-api.spec.ts` | 令牌、参数校验、上传、无缝订阅、日志中无密钥、每个主目录只有一个内核 | IF-1 | 总是 |
| `tests/runs.spec.ts` | 入队检查、崩溃、无效输出、超时、取消、暂停、遗留进程清理与重试 | K4、AC-M1-04 | 总是 |
| `tests/resources.spec.ts` | 按容量调度：放不下的运行按队列顺序等待（后面的也等）、小运行并行、GPU 一次只给一个运行（有两块 GPU 时也是）且其他运行看不到它、超过全部容量的运行单独运行 | NFR-15 | 总是 |
| `tests/checkpoint.spec.ts` | 检查点在运行中立即作为运行的产物入库；崩溃的运行留下检查点，重试以 `resume` 输入续跑；工作目录之外的检查点被拒绝 | FR-TRC-10、IF-2 | 总是 |
| `tests/pipelines.spec.ts` | 计划与缺少输入、`all` 中前面节点绑定的槽位、研发流水线（进入阶段、运行、绑定、判定）、测试复用研发运行、失败保持失败且依赖被跳过、改正后继续、取消、取消后立即继续；同一阶段只能有一条运行中的流水线；硬约束槽位不被流水线覆盖；依赖成环的流程包被拒绝；内核正常停止或被杀掉后流水线继续，被中断的节点从检查点续跑 | FR-PRJ-07/08、FR-TRC-10、AC-15 | 总是 |
| `plugins/numeric/tests/test_l2_torch.py` | 快速 L2 与参考 L2 逐位相同：logits、全部观测点与溢出次数，三种舍入、两种溢出处理与窄累加器下，llama2.c、Qwen2、Qwen3 计算图；有 GPU 时在 GPU 上；KV 缓存逐 token 与整段一次相同（两种实现）；成批计算每个序列与单独计算相同，含槽位重用；精确乘法对极值等于整数乘法，TF32 关闭 | FR-QNT-03 | 总是；GPU 用例无 CUDA 时跳过 |
| `plugins/arch-taalas/tests/test_slice.py` | 解码层的切片级别：S-1 一个 Q 头，S-2 加一个 KV 头与整个头的 O 列，S-3 加 MLP 通道；各级的权重与位数、可复现的观测点，以及列范围不是整组时的拒绝 | FR-MDL-06 | 始终 |
| `plugins/rtl-taalas/tests/test_tile_engine.py` | 流水线数据通路对 Python 参考模型的验证：随机激活与 4/8 位定点数，四种 (MACS, WORDS, BITS) 组合，引擎的累加器逐路与本地计算的和对拍；测试平台是 Verilog，由 Verilator `--binary` 运行 | FR-RTL-01/02 | 始终；没有工具目录时可见地跳过 |
| `plugins/rtl-taalas/tests/test_attention_reference.py` | 注意力参考模型对拍 `GraphL2.attention`：缩放先乘再移位（headDim 8 时是 11,585，不是 2 的幂）、KV 缓存是 `(S, kv_heads * hd)` 的全部已缓存位置、移位用 `arith.rshift` 的舍入；逐头、多个长度，并用截断移位作负对照 | FR-RTL-01/02 | 总是 |
| `plugins/rtl-taalas/tests/test_tile_attention.py` | `tile_attention` 对拍参考：DOT、注册的 exp 表读及其等待周期、restoring 除法、WSUM 与 EMIT；单/多位置、分组查询头对、满量程 int16 向量；Verilator `--binary`，exp 表就是 `tile_lut` 本身 | FR-RTL-01/02 | 总是；工具目录缺失时显式跳过 |
| `plugins/rtl-taalas/tests/test_tile_qfull_top.py` | `tile_qfull_top`：每层一个引擎、共享一套 bank 与激活向量；串行 `done` 链与流水首行链；最后一层的行等于矩阵乘参考 | FR-RTL-01 | 总是；工具目录缺失时显式跳过 |
| `plugins/rtl-taalas/tests/test_tile_normalization.py` | `tile_rmsnorm` 对 `GraphL2.rmsnorm`（attn_norm DIM 32 与逐头 q_norm DIM 8）、`tile_rope` 对 `GraphL2.rope`（Q/K 两路，位置 0/1/5/15）；测试暴露并锁住三个模板缺陷：注册的 LUT 需要等待状态、rsqrt 指数必须扫描最高置位、norm 乘积需要 64 位中间结果 | FR-VER-04、FR-RTL-01 | 总是；Verilator 缺失时显式跳过 |
| `plugins/rtl-taalas/tests/test_tile_linear.py` | 通用逐位线性单元在 o_proj（输入 v、输出 x）上的对拍，以及饱和 int16 残差加法对 `GraphL2` 的对拍 | FR-VER-04、FR-RTL-01 | 总是；Verilator 缺失时显式跳过 |
| `plugins/rtl-taalas/tests/test_tile_attention_kv.py` | `tile_attention` 接两个真实 `tile_kv` 缓存：RoPE 后的 K、v_proj 的 V，位置 0/1/3/7/15，`fq=fk=13`（q_norm/k_norm 的小数位），与 `GraphL2.attention` 逐位比较 | FR-VER-04、FR-RTL-01 | 总是；Verilator 缺失时显式跳过 |
| `plugins/rtl-taalas/tests/test_tile_mlp.py` | MLP 门控：`tile_silu`（裁剪的 sigmoid 索引、Q15 表、两次舍入移位）与 gate/up/down 投影对 `GraphL2` 的对拍 | FR-VER-04、FR-RTL-01 | 总是；Verilator 缺失时显式跳过 |
| `plugins/rtl-taalas/tests/test_tile_layer.py` | 串联的 Q-full 单层验证台：attn_norm、q/k/v、逐头 q_norm/k_norm、RoPE、两个 `tile_kv` 缓存、attention、o_proj、残差、mlp_norm、gate/up、SiLU、down_proj 与第二次残差，覆盖第 0/1 层，17 个观测点全部与 `GraphL2` 逐位比较 | FR-VER-05、FR-RTL-01 | 总是；Verilator 缺失时显式跳过 |
| `plugins/rtl-taalas/tests/test_tile_directed.py` | `tile_layer` 的定向测试：上电状态、忙碌时再次 START、推理中途复位后重新运行、单 token、上下文边界；每项都与 `GraphL2` 的观测点逐位比较（`ver.directed`） | FR-VER-04 | 总是；Verilator 缺失时显式跳过 |
| `plugins/rtl-taalas/tests/test_tile_coverage.py` | 串联层验证台的 Verilator 行覆盖，要求 ≥ 90%（当前 96%） | FR-VER-05 | 需 Verilator |
| `plugins/rtl-taalas/tests/test_formal.py` | 用 SymbiYosys 对 `tile_residual` 的饱和和性质做 BMC，并用一个故意不饱和的模块作为反例 | FR-VER-05 | 需 SymbiYosys |
| `plugins/rtl-taalas/tests/test_mutation.py` | 对 `tile_residual` 跑 MCY 变异：50 个变异体，定向/边界/随机测试台，等价变异由形式等价分类；分数须 ≥ 95%（当前 100%，12 个 `NOCHANGE`） | FR-VER-05 | 需 MCY、Yosys 与 Verilator |
| `plugins/rtl-taalas/tests/test_dft.py` | 生成 bundle 的 DFT 就绪审计（非 RAM 时序块都有复位、只有 `clk`/`host_sclk`、无 latch 写法、`initial` 只初始化 ROM），并带门控时钟/latch/非 ROM initial/缺复位四个反例 | FR-VER-05 | 总是；无 bundle 时显式跳过 |
| `plugins/syn-yosys/tests/test_syn.py` | 在固定 EDA 镜像中把 S-1 切片综合到 sky130A、GF180MCU、IHP SG13G2；OpenSTA 100 ns 时序与 1 ns 反例；sky130 上插入 Fault 扫描链（内部 1553 + 边界 142）；残差 DFT 目标上的 Fault 固定型 ATPG（三 PDK 分别 99.16% / 96.79% / 98.65%）与拉零输出反例；三 PDK 各带错误 DFF 名单反例，另有未知 PDK 反例 | FR-SYN | 需 Podman 与 EDA 镜像 |
| `tests/tile-syn.spec.ts` | SYN 阶段与三个 `syn.synth.*` 检查绑定到 `syn-yosys` 处理器 | FR-SYN | 总是 |
| `plugins/rtl-taalas/tests/test_full_model.py` | tiny 全模型 logits 路径：RTL 第 0 层 → RTL 第 1 层（消费 RTL 残差）→ final_norm → lm_head，128 个 int32 logits 与 `GraphL2` 比较 | FR-VER-05、FR-RTL-01 | 总是；Verilator 缺失时显式跳过 |
| `plugins/rtl-taalas/tests/test_tile_slice.py` | 功能化 S-1 切片：生成器的权重 bank、组尾数与偏置，来自 `GraphL2` 的真实 `attn_norm` 激活，q_proj 第 0 个头的行在四个 token 上逐位比较；Verilator `--binary` | FR-VER-04、FR-RTL-01/02 | 总是；工具目录缺失时显式跳过 |
| `plugins/rtl-taalas/tests/test_verify.py` | R4 计划（可运行与受阻项）、时钟域检查（`clk` 加文档化的 `host_sclk`），以及层/全芯片/覆盖/变异/形式/DFT 项的未运行原因 | FR-VER-01/04 | 总是 |
| `plugins/rtl-taalas/tests/test_rom_macro.py` | 由 `rom_bank0..3.hex` 构建的硬宏视图：黑盒（无内容）、仿真模型（`$readmemh`）、暂定 LEF 与面积算术，以及 Yosys 证明仿真模型与 `tile_rom` 在宏参数下等价 | FR-RTL-02、FR-SGN-07 | 总是；等价部分在 Yosys 缺失时显式跳过 |
| `plugins/rtl-taalas/tests/test_tile.py` | `taalas-tile/1` 的 RTL bundle：ROM 映射规则（`taalas-tile-rom/2`，行主序：一个字是一行的 128 个连续列）对 int4/int8 矩阵逐位往返，覆盖列数不是块整数倍、以及字宽超过 64 位的情况；生成的 S-1 切片为每个矩阵写一个 bank 及其 `$readmemh` 内容、含全部哈希的清单；`tile_check` 的五项检查全部通过——ROM 往返、规则（不得 `lint_off`、每个时钟块都有复位、不得有函数）、三种 lint（Verilator `-Wall`、Verible 与 slang-tidy）零警告、Yosys `check -assert`、三个 ROM 视图（行为、标准单元、通孔编程）由 Yosys 等价引擎证明相等（`equiv_make`/`equiv_simple`/`equiv_induct`，先 `async2sync`）；via 不一致是负对照；被改坏的 bank、注入的 `lint_off`、或来自另一个 bundle 的视图②都会被报出；bundle 还带着数值规格的 rsqrt、exp、sigmoid 表（`tile_lut` 的 `$readmemh` 文件，按条目数与摘要校验），整数 RMSNorm 模板（`tile_rmsnorm`：一个乘法器与一个累加器在 DIM 个周期内复用、偶指数、Q15 rsqrt 表、半上舍入的移位与饱和）RoPE 模板（`tile_rope`：两种表布局——元素 j 与 j+HD/2、交错的成对——，每两周期处理一对，Q14 表、半上舍入与饱和）KV 缓存模板（`tile_kv`：每个 KV 头与位置一行，一个写端口和一个读端口），流水线数据通路（`tile_engine`：S0 地址、S1 字与激活、S2 组和、S3 组累加，每周期一个字）及它的行主序映射（`taalas-tile-rom/2`：一个字是一行的 128 个连续列，阵列的 128 个乘积分成 8 组用树求和） | FR-RTL-01/02/03/04、FR-SGN-07 | 始终；没有工具目录时 lint 与综合部分可见地跳过 |
| `plugins/arch-taalas/tests/test_tlm.py` | `taalas-tile/1` 的事务级模型：按调度表的相位走一个 token 序列，记录每个产生观测点的相位的 L2 观测点（q、k、v、h1、h3、x），是确定性的，且周期数等于芯片规格的周期数；周期不符的芯片会被发现 | FR-ARC-03、计划 7.1（TLM = L2） | 始终 |
| `plugins/arch-taalas/tests/test_tile.py` | `taalas-tile/1`：调度表就是周期模型（每周期读一个含 `macs` 个权重的 ROM 字，矩阵需 `列数 x ceil(行数/macs)` 个周期）、每层一个引擎或切片用一个引擎、输出投影在各引擎间分段、不能整除形状的 MAC 数被拒绝、被篡改的调度表会被 `verify_spec` 发现 | FR-ARC-01/02 | 始终 |
| `plugins/numeric/tests/test_gptq.py` | 校准量化：PyTorch 浮点图给出 NumPy 解释器的 logits；GPTQ 保持规格的权重量化格式（电平、15 位尾数、每个矩阵一个指数），并在相关输入上使乘积 XW 的变化小于同一网格上的舍入；带逐矩阵格式的校准规格可作为 L2 加载、运行，并按各自位宽统计存储 | FR-QNT-01 | 始终（torch，CPU） |
| `plugins/numeric/tests/test_diagnose.py` | L2 的观测点（反量化后）与浮点图一致；诊断给出首个分歧 token 与偏差最大的层（或在整段窗口内两者一致时如实记录） | FR-QNT-09 | 始终（torch，CPU） |
| `plugins/numeric/tests/test_bench.py` | 比较：下降与配对区间、只有一方答对的题、拒绝不同题目 / 题集 / 数据 / 模型 / 设置的报告；L2 回答 lm-eval：对数似然等于参考 L2 的结果，贪心生成与参考相同并在停止串处停止；`spec.l2.fast` 的证据在正确的快速 L2 上没有不一致，在舍入错误的快速 L2 上（阴性对照）三类比较都报告不一致 | FR-QNT-03/04 | 总是（小 Qwen3） |
| `plugins/numeric/tests/test_benchcode.py` | 代码沙箱：参考解通过、错误解失败、试图写主机文件的代码失败且主机上没有文件；取消（SIGTERM）在 2 秒宽限期内移除容器 | FR-QNT-04、AGENTS.md “Adding a plugin” 6 | 需 Podman、沙箱镜像与工具目录中的 EvalPlus 题目，否则可见地跳过 |
| `tests/qwen-spec.spec.ts` | Qwen2.5-0.5B-Instruct 的 SPEC 步骤：负责人给出模型与基准数据后，研发流水线全部节点运行，`model.*` 与 `spec.l2.fast` 通过，7 项快速集检查都有判定；必需测试复用研发运行 | FR-MDL、FR-QNT-03/04/08 | 须 `LLMCHIP_QWEN=1`、一块 GPU，以及工具目录中的模型、基准数据与沙箱镜像；1 passed，1277 秒（2026-10-24），流程包修复后（2026-10-30）为 1211 秒 |
| `tests/pipelines-flow.spec.ts` | 内置插件：全新项目上 S0–S3 每一步的研发与测试都通过，测试复用研发运行；`all` 跑完 S0–S7 的研发与全部测试 | FR-PRJ-07/08 | S0–S3 总是运行（需要 `.venv`）；S0–S7 须同时满足 `LLMCHIP_FULL_FLOW=1` 和 EDA 镜像；1 passed，1967 秒（2026-10-24） |
| `tests/recovery.spec.ts` | 已确认的写入在反复 SIGKILL 后仍然存在 | NFR-04 | 总是 |
| `tests/workflow.spec.ts` | 解析不完整、崩溃或能力未认证时判 indeterminate；没有豁免：失败保持失败并打开事故，改正后的新设计才通过，历史保留；没有审批门时进入阶段、旧方法不存在；R0 之前写下的主目录（旧流程包上的项目、审批/豁免/导出事件、`waived` 判定、带 `gates` 的迁移事件）仍能打开、重放并通过哈希链校验；新注册的流程包若仍声明审批门则被拒绝；分支 | AC-M1-03、架构 ADR-011 | 总是 |
| `tests/agents.spec.ts` | 变更集上的迭代循环、权限、死循环与预算停止、假 PDK 出口拦截、接手、降低标准 | AC-M1-02/07/08/09/13 | 总是 |
| `tests/mcp.spec.ts`、`tests/providers.spec.ts` | MCP 边界；OpenAI 兼容与 Anthropic 适配器（对模拟服务器测试） | FR-INT-05/06 | 总是 |
| `tests/estimator.spec.ts` | 三个模型的完整分项；缺数据时结论为“数据不足”，S0 的检查判 indeterminate | AC-M1-10 | 总是；没有 `.venv` 时**失败**（不会跳过） |
| `tests/m2-flow.spec.ts` | stories260K 上 S0 → S3 各阶段由其检查判定；旧判定不针对新数值规格，旧芯片规格对新数值规格判 fail | M2、AC-04 | 总是；没有 `.venv` 时**失败** |
| `tests/m4-kernel.spec.ts` | 硬约束槽位只能由人类绑定；迁移记录重新开始的阶段，拒绝未知阶段 | FR-SGN-02b、FR-MIG-03 | 总是 |
| `tests/model-import.spec.ts` | 导入真实 Hugging Face 布局的小 Qwen3，普查、按普查估算、图还原、L0、与 transformers 的全量测试；llama2.c 与 run.c 移植对拍；项目自带的占位模型权重；没有 config.json 的模型如实报告而不猜测 | FR-MDL-01/03/05/06 | 总是；需要 `.venv` |
| `plugins/model-llama/tests/test_graph.py` | Llama（共享/不共享输出层）、带滑动窗口的 Mistral、Qwen2、Qwen3 与 transformers 对拍；llama2.c 与 run.c 移植对拍；重复存储的共享输出层；截断的文件；不支持的模型 | FR-MDL | 总是 |
| `python/llmchip-vchip/tests/test_harness.py` | 虚拟芯片测试环境：无效清单标为不可用并给出原因；分词往返，无法表示的文本被拒绝；参考芯片与 run.c 移植结果一致；prefill 与 step 一致；上下文边界与非法输入；停止 token；SPI 帧周期与 tok/s；确定性；经进程芯片与数值黄金芯片逐位一致；策略判定；崩溃、挂起、乱码被记录且不中断运行；超时会杀掉芯片的整个进程组；不报告周期的芯片不给出速度；没有可用提示词时一致性检查为 incomplete 而不是通过；两个模型家族 | 设计 T-01–T-14（负责人草案）；FR-VER-04/05 | 总是运行（`yarn check:python`） |
| `python/llmchip-vchip/tests/test_rtl_vchip.py` | 设计中的 L3：`vchip new rtl` 生成三颗芯片；校验固定版本的镜像与测量声明；自称有 testbench 的进程芯片不被信任；有镜像时，RTL 芯片以 `--quick` 通过一致性检查（T-15–T-17 通过；T-18/T-19 为 `not_run`，结果为 incomplete 而非通过），在 4 个提示词 × 64 个位置上与 L2 逐位一致，周期由 testbench 计数、与设计一致，且 > 1000 tok/s；被杀掉的桥不留下容器 | 设计 T-15–T-19 | 仿真部分需要 EDA 镜像（缺失时可见地跳过）；全部 20 个提示词需 `LLMCHIP_VCHIP_L3=1` |
| `tests/kernel-footprint.spec.ts` | harnessd 启动时间与空闲 RSS | NFR-01（内核部分） | Linux |
| `tests/spatial-flow.spec.ts` | 占位模型的空间引擎从 S2 到 S5：完成设计（周期取自固定时序表，≥ 1000 tok/s，晶粒按实测单元面积选取，`verify_spec` 重新计算周期，并判定周期目标）；生成电路，并从网表读回做往返检查；lint 无警告；`verification-plan-spatial` 的每一项都通过：观测点经主机接口读出，L3 与 L2 逐位一致，每个位置的忙碌周期相同，位串行单元有单元测试，行覆盖率 ≥ 90% | FR-ARC-04/05、FR-RTL-01/02/03、FR-VER-04 | 需 EDA 镜像（2026-10-21 用时 261 秒） |
| `tests/arch-perf.spec.ts` | S3 的速度是精确的：占位模型在四种微架构选项下（SPI / SQI，缓冲 none / attention / all），RTL 每一步的忙碌周期逐位置等于芯片规格的预测，且 RTL 与 L2 一致 | FR-ARC-04/05、FR-EST-07 | 需 EDA 镜像（2026-10-20 用时 790 秒） |
| `tests/numeric-schemes.spec.ts` | 任何已还原的计算图（小 Qwen3、从自身产物导入的占位模型）量化并评估 L2 对 L0，窄累加器确实溢出，判定 `numeric.accuracy`，向量引擎拒绝计算图规格；有 EDA 镜像时，把偶数舍入 / 回绕 / 24 位累加器方案从数值规格带到芯片规格，RTL 在指令级模拟器和 Verilator 中与 L2 一致，ALU 按这些模式测试 | FR-QNT、FR-RTL-01、FR-VER | 计算图部分总是运行；RTL 部分需 EDA 镜像（约 4 分钟） |
| `plugins/vchip/tests/test_plugin.py` | 不经内核测试插件的处理器：从项目产物构建芯片（输入文件没有后缀，与内核交来的一样）；设计周期与时钟只取自正是这个设计的芯片规格（别的权重、没有哈希、别的除法器数或架构都被拒绝），作为参数给出的时钟会如实标明；L2 对照由模型 IR 构建的浮点参照，后者与由检查点构建的参照结果相同；报告不含策略和判定，测不到的指标不出现；由别的权重生成的 bundle 和向量引擎的 bundle 被拒绝；一致性检查以计数报告；有镜像时，RTL 与 L2 逐位一致，周期由 testbench 计数（1,327，等于设计，> 1000 tok/s），快速一致性检查使 T-18/T-19 未运行 | 设计 L4；ADR-010、AP-7 | 总是运行；RTL 部分需 EDA 镜像（缺失时可见地跳过） |
| `tests/vchip.spec.ts` | 经内核运行插件：占位模型的 L2 在 `evalset-proxy` 的 20 个提示词上对照其浮点参照，报告是产物且不含判定，`vchip.l3` 对 L2 判为 indeterminate（不是硬件），`vchip.conformance` 在 20 个提示词上通过、在 3 个提示词上为 indeterminate，手输提示词；有镜像时，由 `rtl-taalas generate` 生成的空间引擎 RTL 与 L2 逐位一致，周期与时钟取自芯片规格，被取消的运行不留下仿真容器 | 设计 L4 | L2 部分总是运行（需 `.venv`）；RTL 部分需 EDA 镜像；`LLMCHIP_VCHIP_L3=1` 时跑全部 20 个提示词，`vchip.l3` 必须通过（2026-10-22：3 个测试通过，661 秒；本次改动后完整运行 `vchip conform spatial-5k`：T-01–T-19 通过，T-06 不适用，12.7 分钟，没有遗留容器） |
| `tests/web-ui.spec.ts` | 语言目录一致；在浏览器中以两种语言渲染每个页面；S0、S1、S2、S3 详细视图，S3 也覆盖空间引擎项目（电路、面积模块、时序表、设计空间）；虚拟芯片页面上的一致性报告，以及被 `vchip.l3` 判为 indeterminate 的生成测试 | AC-M1-12 | 浏览器部分需 `apps/web/dist` 与 Chromium |
| `tests/containers.spec.ts` | 无网络、输入只读、取消时移除容器 | IF-3 | 需 `python:3.12-slim` 镜像 |
| `tests/calibration.spec.ts` | 每个存储候选都得出可用或不可用；按视图的阶段准入 | AC-M15-01/02/03 | 需 EDA 镜像 |
| `tests/m3-flow.spec.ts` | 验证计划、注入缺陷的定位（判定只针对有缺陷的 bundle） | M3、AC-03/09 | 需 EDA 镜像 |
| `tests/ui-latency.spec.ts` | EDA 运行期间 UI 查询 p95 < 200 ms | NFR-03（内核侧） | 需 EDA 镜像 |
| `tests/m4-flow.spec.ts` | S0–S7、DRC/LVS/STA、时序与 SDF 反例、从 GDS 到权重的血缘、迁移评估 | M4、AC-02/05/06/08/12/14 | 须同时满足 `LLMCHIP_FULL_FLOW=1` **和** EDA 镜像 |

### 6.3 须显式开启的完整流程

`LLMCHIP_FULL_FLOW=1 yarn test tests/m4-flow.spec.ts` 会运行物理工具。修改 `rtl-taalas`、`eda-gf180` 或签核检查后都要运行它。2026-10-20 在四线 SPI 设计上的结果（R0 之前，含 S8 与 G4）：通过（1 个测试，2132 秒，约 36 分钟；WSL2 上的 Podman 3.4）。R0 之后（S0–S7，无审批门，血缘含工具版本与判定）于 2026-10-24 重新运行：通过（1 个测试，2070 秒，约 34.5 分钟）。

修改空间引擎生成器（`llmchip_rtl/spatial/`）或测试环境通往 RTL 的桥（`llmchip_vchip/bridges/`）之后，必须运行一次完整的 L3 一致性检查（硬约束，负责人 2026-10-22 决定；见 AGENTS.md “Containers”）：先 `yarn vchip new rtl --proxy --id spatial-5k`，再不带 `--quick` 运行 `yarn vchip conform spatial-5k`；或运行 `LLMCHIP_VCHIP_L3=1 uv run --frozen pytest python/llmchip-vchip/tests/test_rtl_vchip.py`。2026-10-22 的结果见 §6.1 中 `vchip` 的各行。2026-10-25 在修复桥的容器清理之后重跑（见 §7.2）：T-01–T-19 通过，T-06 n/a，负载下约 20 分钟，没有留下容器。

`LLMCHIP_QWEN=1 yarn test tests/qwen-spec.spec.ts` 端到端运行 Qwen2.5-0.5B-Instruct 的 SPEC 步骤：研发流水线（导入、图还原、与 transformers 对拍、量化、快速 L2 对参考 L2、L0 与 L2 的快速集、二者比较），随后是必需测试，再次判定研发运行。2026-10-24 在 RTX 5060 Ti 上的结果：通过（1 个测试，1277 秒，约 21 分钟）；在 2026-10-30 的流程包修复后（设计由自己的 `verify` 节点判定、`rtl.tile.equivalence`、tile 的事务级模型得以运行）再次通过（1 个测试，1211 秒，约 20 分钟）：`spec.design` 与 `spec.tlm` 首次给出判定，且每项必需测试都复用了研发运行。用逐元素舍入（RTN，R2a）时 7 项快速集检查全部失败，这是未校准 W4 的预期结果；用 GPTQ（R2b，2026-10-25）后 MMLU 变为无法判定，其余 6 项仍失败。两者都不是修改检查的理由：

| 快速集（900 题） | L0 % | L2 RTN % | L2 GPTQ % |
|---|---|---|---|
| MMLU | 40.7 | 36.1 | 40.4 |
| CMMLU | 50.4 | 34.0 | 31.3 |
| C-Eval | 43.8 | 39.2 | 25.4 |
| GSM8K | 40 | 0 | 20 |
| IFEval | 40 | 20 | 0 |
| HumanEval+ | 40 | 0 | 0 |
| MBPP+ | 60 | 0 | 0 |

GPTQ 一行是规格 `c030b380`（W4、分组 128、WikiText-2 的 64 个 2048 token 窗口、169 个矩阵、GPU 上 2 分 37 秒）；其 MMLU 分差 0.35 点、95% 区间 [−5.96, 7.02]，检查判无法判定（区间上界高于 2 点的允许值），其余 6 项失败。校准把 MMLU 比 RTN 提高 4 点，却把 C-Eval 降低 14 点、CMMLU 降低 3 点：在英文文本上校准的 W4 模型损失了中文能力。这是实测结果，如实记录，既不找借口，也不靠改标准掩盖；定位它用的就是下面的诊断。

生成类基准在快速集中各只有 5 题，因此百分数按 20 点一档变化。完整集于 2026-10-24 在 L0 上运行（2098 秒：MMLU 327、CMMLU 237、C-Eval 36、GSM8K 662、IFEval 562、HumanEval+ 105、MBPP+ 170；bf16，上下文 2048 token），与 Qwen 为同一模型公布的数值并列：

| 基准 | 题数 | L0 % | Qwen % | 设置 |
|---|---|---|---|---|
| MMLU | 14042 | 42.1 | 47.5 | Qwen 的是基座模型、5-shot；指令模型未公布 |
| CMMLU | 11582 | 47.4 | — | 未公布 |
| C-Eval | 1346 | 43.9 | — | 验证集；未公布 |
| GSM8K | 1319 | 48.9 | 49.6 | 零样本，用 Qwen 的 instruction，取 boxed 答案，贪心 |
| IFEval | 541 | 26.8 | 27.9 | 严格 prompt 级准确率 |
| HumanEval+ | 164 | 34.1 | 35.4 | Qwen 的是 HumanEval，仅基础测试 |
| MBPP+ | 378 | 41.5 | 49.6 | Qwen 的是 MBPP，仅基础测试 |

L2 的完整集于 2026-10-25 启动（校准后的 W4 规格 `c030b380`）；7 个基准已于 2026-10-25（01:15 UTC）全部完成，而且**每一个都超过允许值**（FR-QNT-04：选择题基准 2 点、GSM8K 与 IFEval 3 点、代码基准 5 点）：

| 完整集 L2（部分，2026-10-25） | L0 % | L2 % | 分差（95% 区间） | 允许值 |
|---|---|---|---|---|
| MMLU | 42.05 | 36.97 | 5.08 [4.21, 5.98] | 2 |
| CMMLU | 47.39 | 30.56 | 16.83 [15.71, 17.94] | 2 |
| C-Eval | 43.91 | 27.19 | 16.72 [13.15, 20.28] | 2 |
| GSM8K | 48.90 | 16.45 | 32.45 [29.64, 35.25] | 3 |
| IFEval | 26.80 | 19.04 | 7.76（严格，541 题；此处未记配对区间） | 3 |
| HumanEval+ | 34.15 | 5.49 | 28.66（pass@1，164 题；此处未记配对区间） | 5 |

| MBPP+ | 41.53 | 13.23 | 28.30（pass@1，378 题；此处未记配对区间） | 5 |

目前的结论是：暂定的 W4 规格即使经过 GPTQ 校准也不满足精度判据；R2 的后续方向是混合精度或对敏感矩阵用更宽的格式，而不是降低标准。

GSM8K、IFEval 与 HumanEval+ 与公布值相差约 1 点以内。MBPP+ 低 8 点，是因为 EvalPlus 的 plus 测试比 Qwen 计分的基础测试更严；两者不是同一个基准。MMLU、CMMLU 与 C-Eval 没有指令模型的公布值。这些 L0 数值是完整集比较判定 L2 的基线；L2 的完整集已于 2026-10-25 运行（§6.3 与 §7.2）。

---

## 7. 当前限制

### 7.1 未实现

| 缺口 | 相关需求 |
|---|---|
| Tauri 桌面外壳、Windows 安装器、专用 WSL 发行版、首次启动引导、自动拉取镜像 | FR-UI-01、FR-UI-03、NFR-08、NFR-13、AC-M1-01、AC-01、ADR-002、ADR-005 |
| CHARTER.md 生成与历史、阶段手册、示例项目 | FR-PRJ-02、FR-PRJ-06、FR-AGT-11 |
| 项目 git 工作目录；合并变更集只记录状态 | FR-TRC-09、架构 §6.2–6.3 |
| 训练参考模型（负责人推迟；暂以占位模型替代） | FR-QNT-05、需求 §11.1 |
| Llama 家族以外（GPT-2、MoE 等）、GGUF（只做普查）以及带 RoPE 缩放（如 Llama 3.1/3.2）的图还原；在工作台内从 Hugging Face 下载 | FR-MDL-01、FR-MDL-02、FR-MDL-03 |
| 项目导出与导入、复现命令、审计导出 | FR-PRJ-05、FR-TRC-07、FR-TRC-08 |
| 产物清除、GC 与保留策略 | 架构 §4.2、FR-AGT-20 |
| 由内核管理的外部编码智能体 | FR-AGT-03 |
| `images/` 构建定义 | 架构 §14 |

### 7.2 部分实现

- **估算器**：多芯片只给出芯片数（FR-EST-06）。没有参数扫描或 Pareto（FR-EST-08）。标定结果不会自动更新模型（FR-EST-10）。
- **标定**：SRAM 的四种配置中只覆盖 512×8（FR-EST-13）。sky130 与 IHP sg13g2 的档案是新增的（2026-10-25）：单元面积取自各 PDK 的 liberty 文件，但 `library-130nm` 里的 ROM/SRAM 密度是按 GF180MCU 标定值缩放的**经验值**，两个档案都写明 `calibration: not run`。57,344 个权重的切片在 128 MAC 下估算为 sky130 2.65–5.21 mm²（典型 3.79）、IHP 4.05–7.25 mm²（典型 5.66），对照 3.0 mm² 的占位 prerun 晶粒：sky130 有条件可行，IHP 在密度标定之前不可行。
- **验证**：只有 `rtl-functional-sim` 是已声明并认证的能力，门级仿真没有能力门控。SDF 仿真能运行，但 `sdf.timing` 无法通过（§1.3、FR-VER-01/03、AC-12 正例）。
- **存储内容检查**（FR-SGN-07）：映射可逆，生成时做往返检查。由于设计使用标准单元 ROM，没有比较宏内容的签核检查。
- **物理实现**：芯片为 1.2×2 mm，不是 2 mm²（负责人决定，需求 v0.6）。设计对象是未训练的占位模型。空间引擎放不进这个晶粒，按实测单元面积需要 5.0 × 3 mm（负责人决定，2026-10-20：晶粒按需加大；布局确认尺寸后再更新需求文档 §11.1）。它还没有做物理实现。
- **空间引擎**（2026-10-21 实测，占位模型，8 MHz）：每个 token 1,327 周期（6,029 tok/s）；加上主机的 START 与 STATUS 帧为 2,059 周期（约 3,900 tok/s）。综合后单元面积 5.96 mm²：KV 环形缓存 2.09，矩阵电路 1.94（串行加法器 0.80、行输出 0.75、延迟线 0.39），核心逻辑 0.97 及其 16 个 softmax 除法器 0.05，核心寄存器 0.42，注意力乘法器 0.41，查找表 0.07，主机接口 0.02。其他形状的核心逻辑（按维度）和查找表面积是外推值。KV 寄存器的复位值来自复位后的清零过程（AGENTS.md，负责人决定，2026-10-21），门级仿真的证明随物理实现一起完成。生成器实现维度为 2 的幂（头大小至少 4）、2 个头、1 个 KV 头、上下文 64、任意层数、隐藏维度不超过 255 的模型；其他形状会被拒绝并给出原因。不给 `die`（或 `targetAreaMm2`）时，面积上限是工艺档案的 prerun 面积（2.4 mm²），空间引擎放不下；PPA 会给出建议的晶粒（`suggestedDie`），在 prerun 约束更新之前（P4），设计使用这个晶粒。
- **迁移**：评估可用，确认后记录重新开始的阶段。下游产物与判定不会被标记为失效（FR-MIG-03、FR-TRC-05）。从 S3 或更早重新开始须由人类确认（FR-MIG-04），这一点没有测试：唯一不是人类的调用方是外部令牌，而 `migration.apply` 不在外部方法白名单中。没有能自动打标签的 NDA PDK 导入通道（FR-INT-04、FR-SEC-01）。
- **智能体**：变更集没有大小阈值（FR-AGT-06）。没有软约束流程（FR-AGT-09）。没有按角色区分的工具集或 LLM（FR-AGT-14/19）。去密接手包只作为建议给出，不会实际生成（FR-AGT-13a）。
- **调度与流水线**（R1）：内置插件只在需要超过默认值时声明 `resources`：`numeric` 的 `benchmark` 与 `l2_fast_check` 要求 4 核、8 GiB 和一块 GPU（有 GPU 时用 GPU，否则回退到 CPU；机器没有 GPU 时它们独占运行，因为超出整机容量的运行可以单独运行）；其余处理器仍按默认需求调度（进程插件 1 核、1 GiB；容器插件 4 核、8 GiB）。因此在 15 GB 内存的开发机上，两个按默认上限（8 GiB）的容器运行不再并行，而是先后运行（此前会超额使用内存）；`tests/ui-latency.spec.ts` 为了制造负载，把容量声明为 32 GiB。队列严格按顺序：放不下的运行会挡住后面的运行，利用率因此偏低，但不会有运行被饿死。容器插件不分配 GPU。测试复用以前的运行时只比较处理器、参数和输入，不比较插件版本（运行记录不保存插件版本）。没有测试“运行全部测试”在内核被杀掉后的续跑（研发流水线的续跑有测试，两者走同一条路径）。流水线是否可以写入硬约束槽位（目前：只在为空时写入）待负责人决定。`flow-default` 的流水线参数固定在流程包里，只驱动 5K 占位模型；Qwen 的流水线随 R2 起的六步流程包提供。
- **UI**：没有事故筛选与跳转链接。除 PNG 外，没有 RTL、波形或版图查看器。设置页缺少 LLM、配额与出口配置（FR-UI-10/11/13、FR-AGT-25/26）。流水线面板列出节点的状态、运行、判定与原因，但没有每个节点的用时、资源和实时日志（FR-UI-14 的节点图只部分实现）。演示流程页的“完成”仍是近似：阶段内每项已判定检查的最新判定都通过、每个处理器的最新运行都成功，并且已进入下一阶段（最后一个阶段除外）；它还没有改为按流程包的必需测试判断。
- **工具版本的追溯**：运行记录只保存插件和处理器，不保存当次运行使用的容器镜像；镜像只能从插件清单中查到（摘要固定），清单改变后无法从运行本身得知旧镜像（FR-TRC-02、AC-08）。
- **R0 之前的判定**：在旧主目录中，`accuracy.budget` 或 `phy.drc` 的版本 1 判定若为 `waived`，之后版本 2 的通过会打开 `standard_lowered` 事故，尽管标准实际上变严了（取消了豁免）。关闭事故时说明即可。
- **CLI**：去密、迁移没有专用命令，需要用 `llmchip call`（FR-INT-07）。
- **TypeScript 插件 SDK** 没有自己的测试，只通过 `stub-tool` 间接覆盖。
- **导入的 Hugging Face 模型**在 `flow-default` 中只能以分析的形式进入 S2：S3–S7 使用 llama2.c 模型 IR（`model.ir`），arch-taalas 会带着原因拒绝计算图数值规格。NumPy 的 L0 解释器没有 KV 缓存（每步重算整个序列）；计算图 L2 与快速 L2 有。
- **Qwen 的 SPEC 步骤**（R2a）：数值规格自 R2b 起是在 WikiText-2 上做 GPTQ 校准的 W4，而完整集表明这还不够：已完成的六个基准上，L2 对 L0 的分差为 MMLU 5.1 点、CMMLU 16.8 点、C-Eval 16.7 点、GSM8K 32.5 点、IFEval 7.8 点、HumanEval+ 28.7 点、MBPP+ 28.3 点（2026-10-25，完整集）——R2 后续用混合精度或对敏感矩阵更宽的格式，而不是降低允许值；混合精度（逐矩阵格式）已实现但流水线未使用；答错回溯诊断是 `numeric/diagnose`（`spec.diagnosis`：首个分歧 token、偏差最大的层及其矩阵）；KV int8、`taalas-tile/1` 的设计与切片已经实现（`arch-taalas design` 带 `architecture: taalas-tile/1`，以及 `arch-taalas slice`）；- **完整集之后的模型质量**：已完成的四个基准（MMLU、CMMLU、C-Eval、GSM8K）用校准后的 W4 规格全部超过允许值（§6.3），所以下一个校准实验是 W8，或对敏感矩阵用 W8 的 W4（`calibrate_graph` 的逐矩阵 `overrides`）：W8 矩阵的 ROM 翻倍（整模型 W4 ROM 已 1.98 Gbit，`taalas-tile/1` 的面积模型在 W4 已判 Q-full 不可行，所以 W8 不改变 Q-full 的可行性结论，切片 S-1 不受影响）。W8 规格上的快速集是第一项测量。

- **数值格式方案矩阵（A1，2026-10-30 实测，快速集）**：用 `calibrate_graph` 的逐矩阵 `overrides` 建了三份规格，各在 GPU 上跑一遍快速集（900 题），与同一份 L0 报告比较（`tests/qwen-schemes.spec.ts`；`LLMCHIP_QWEN=1`，47 分钟）：

  | 方案 | 权重存储 | MMLU | CMMLU | C-Eval | GSM8K | IFEval | HumanEval+ | MBPP+ |
  |---|---|---|---|---|---|---|---|---|
  | 全 W8（int8、组 128） | 3.95 Gbit | 1.1 [-1.8, 3.9] 无法判定 | -0.6 [-3.3, 2.1] 无法判定 | 0.4 [-3.5, 4.2] 无法判定 | 20.0 [0, 60] fail | 0 pass | 0 pass | 0 pass |
  | 敏感矩阵 W8（o/down/嵌入 48 个矩阵 W8，其余 W4） | 2.47 Gbit | -3.9 [-10.5, 3.2] 无法判定 | 4.2 [-0.9, 9.6] fail | 1.9 [-4.6, 8.1] 无法判定 | 20.0 fail | 20.0 fail | 40.0 fail | 20.0 fail |
  | W4、组 64 | 1.98 Gbit | 0.0 [-5.3, 5.6] 无法判定 | 17.0 [10.7, 23.3] fail | 22.3 [14.6, 29.6] fail | 20.0 fail | 20.0 fail | 40.0 fail | 60.0 fail |

  表内是 L2 相对 L0 的下降（百分点）与配对 bootstrap 95% 区间；负值表示 L2 高于 L0。L0 的快速集得分为 MMLU 40.7、CMMLU 50.45、C-Eval 43.85。**W8 是唯一在三个选择题基准上点估计都落在允许值内、并且 IFEval、HumanEval+、MBPP+ 零下降的方案**；它的 GSM8K 从 2/5 降到 1/5（20 点）而 fail，这是快速集每个生成类基准只有 5 题的直接后果。敏感矩阵混合精度没有救回 CMMLU（4.2 点），分组 64 反而把 CMMLU/C-Eval 拉低 17.0/22.3 点——更细的分组没有帮助。**A3 的结论：W8 是唯一接近达标的方案，代价是 ROM 约为 W4 的两倍（3.95 Gbit）；正式判定只能由全量集给出（A2 尚未运行：`LLMCHIP_QWEN=1 LLMCHIP_SCHEME_SET=full yarn test tests/qwen-schemes.spec.ts`，小时级、可检查点续跑）。是否接受 W8 的 ROM 翻倍，或继续换方案（而不放宽 `spec.accuracy` 的允许值），由负责人决定。**
- **事务级模型已实现**（`arch-taalas tlm`：调度表的相位、每个相位对应的 L2 观测点、周期数，并在同一 token 序列上与 L2 逐位一致）；sky130/IHP 档案在 R2 的后续部分。在校准规格上，tile 为 24 个引擎、每个 128 个 MAC（128 整除 896 与 4864；计划里的 448 除不尽 4864，最大公约数是 128），ROM 字宽 512 位，8 MHz 下每 token 189,564 个周期、42.2 tok/s，输出投影在 24 个引擎间分段；64 个 MAC 时 379,085 个周期、21.1 tok/s。流水化之后周期模型是行主序：一个矩阵需要 `rows × ceil(columns/macs)` 个字，加上每个相位 3 个流水周期（S1 字、S2 组和、S3 累加），调度表、`verify_spec` 与事务级模型都用它。模型还加了两个调度开关：`classifierOverlap` 让分段的分类头成为独立的调度行，于是一个 token 付的是「层流水」与「分类头流」中较慢的一个，而不是两者之和；`dualMlp` 把 `gate_proj`/`up_proj` 融成一个双流相位。在校准规格上（24 引擎 × 448 MAC），两者合起来把每 pass 周期从 54,320 降到 31,927（1.70×；8 MHz 下 147.3 → 250.6 tok/s，50 MHz 下 1,566），`design` 会把两个开关记录进 `dataflow`。双流 MLP 的 RTL 侧是 `tile_engine2`：两个 `tile_engine` 实例从两套 banked ROM（`INIT_A0..3`、`INIT_B0..3`）同步走同一批地址，共享一个激活写端口并同时输出两个行值——代价是 MAC 阵列翻倍，换来 gate/up 相位只花一个矩阵的时间；两路输出都对着 Python 参考逐位校验。同样的复制方式实现批处理：`tile_engine_b` 例化 `B` 个引擎，一起启动，并按 token 把激活写端口解复用，于是每个 token 有自己的激活缓冲与累加器，而所有引擎读同一批字——这就是模型里 `batch` 的周期行为。在这个 RTL 模型里每个副本各带一份 ROM 实例（即计划里的阵列复制）；量产的布局会把一份 ROM 扇出给 `B` 个阵列，那只改变面积、不改变周期。模型还接受 `batch` 参数（N9，草案里的吞吐计划）：一次通过产生 `batch` 个 token，矩阵相位为它们共享一次权重流、周期不变，按 token 计算的注意力与向量相位随 batch 增长，`performance.cyclesPerPass` 记录一次通过。448 MAC、B=4 时一次通过是 78,947 个周期、4 个 token：8 MHz 下 405 tok/s，50 MHz 下 2,533，100 MHz 下 5,067；计划里的分类头重叠与 gate/up 并行是调度改动，尚未进模型。事务级模型在 8 个 token 上覆盖 12 个相位、194 个观测点，0 处不一致，周期同为 189,564，第二次运行完全一致。面积模型判定 Q-full 不可行（即便最乐观的晶粒也是 18,440 mm²，而掩模版上限 850 mm²）：整模型的 W4 ROM 放不进任何晶粒，所以 Q-full 只做 RTL 与验证，进入综合、物理设计与签核的是切片（S-1，57,344 个权重）。第 0 层的 S-1 切片是 57,344 个权重（229,376 位），与计划一致；S-2 与 S-3 比计划表多一个 Q 头（286,720 与 630,784 个权重，而非 229,376 与 573,440），因为 O 投影必须取两个完整的头（128 列）才落在 128 个权重的组边界上——计划假定 O 的分组为 64。完整集还没有单独的入口（需求 FR-QNT-08）：`benchmark` 带 `set: full` 只能用 `runs.start` 或 CLI 启动，再用 `verdicts.evaluate` 按比较报告判定 `spec.accuracy.<基准>`。快速集中每个生成类基准只有 5 题：此时分差是 20 点的整数倍，所以只有 L0 与 L2 在完全相同的题上答对时快速集检查才通过（分差 0、区间 [0, 0]；例如双方都是 0/5），分差超过允许值时失败，其余情况判 indeterminate——5 题也可能出现这种“通过”（冲突 C8，§7.3）；正式结论只来自完整集。L0 的得分与 Qwen 公布的数值（[benchmark-reference.json](../plugins/numeric/data/benchmark-reference.json)）只在比较报告中并列，不判定；它是插件自带的数据，不是运行输入。GSM8K 的评测设置由前 200 题的 L0 实测选定（带重复惩罚时：lm-eval 默认 33.5%、8-shot CoT 36.5%、零样本 CoT 42.0%、`GSM8K_QWEN` 44.5%；Qwen 公布 49.6%），去掉重复惩罚后的完整集数值见 §6.3。代码沙箱把工作目录可写地挂载，被测代码理论上能改写自己的结果文件；被测代码来自本工作台的模型，不是对抗性的，记为已知风险。`l2_fast_check` 的参考 L2 在 CPU 上运行，20 个提示约 3 分钟，没有检查点。负责人 2026-10-24 的决定：上述 GSM8K 设置继续沿用；R2c 的校准量化（GPTQ）在 WikiText-2 上校准，数据已下载到工具目录（`tools/hf/datasets/Salesforce___wikitext`），不进仓库；L2 的完整集已于 2026-10-25 启动（带检查点；单卡需数小时）；在它完成之前，完整集检查还不能给出判定。
- **tile 的 RTL（R3）**：`taalas-tile/1` 的 bundle 能为切片生成（S-1 是 57,344 个权重），也能为整个模型的每个层生成 ROM bank（Q-full：24 层 × 7 个矩阵，加上绑定分类器的 bank），每个 bank 都有视图①、综合后视图②和通孔编程视图③，三者都由 Yosys 证明等价。注意力模块与 Q-full 顶层现在已是 bundle 里的模板：`tile_attention` 用一块专用的 17×17 乘法器复现 `GraphL2.attention`（注册的 exp 表读及其等待周期、每个位置一次 restoring 除法、加权和累加器）；`tile_qfull_top` 每层一个引擎，按 `done` 串行或在首行输出时流水，生成器还为每层写一套 bank（`layer<n>_bank0..3.hex`）以及加载它们的 `tile_qfull_banked` 顶层。按调度表逐相位编码已在 RTL 里（`tile_layer`，B1）：一个状态对应一个相位，驱动完整的解码层；整模型的层链仍按 `done` 串行，残差缓冲握手与全芯片自跑随 B3–B5。ROM 三视图已齐备：视图①、视图②，以及视图③（通孔编程阵列 `rom_view3.v`/`rom_via.json`，在与视图②相同的 64 字前缀上证明与视图①等价；`rom_view3_equiv.v` 与 via 不一致的负对照都在测试里）。RTL lint 采用 R3 的三工具标准：Verilator `-Wall`、Verible（制表符、行尾空格、文件末尾换行、禁用宏、120 列行长）和 slang-tidy 的综合检查，全部零警告；`rtl.tile.lint` 与 `rtl.tile.rom.views` 升到版本 2。数据通路现在按设计草案确定的行主序形式存在：一个 ROM 字存放一个输出行的 128 个连续权重，`tile_mac` 把 128 个权重与该列块的 128 个激活相乘并用 8 个组加法器求和（S2 寄存），8 个组累加器（S3 寄存）累加一行的各块和，行的值是 8 个累加器的树，在该行最后一个块之后三个周期与行号一起输出（`tile_engine`）。映射规则是 `taalas-tile-rom/2`；S-1 的 57,344 个权重是 448 个 512 位字（229,376 位），与草案修正后的数目一致。在 GF180MCU tt 角综合（Yosys + OpenSTA，`tt_025C_1v80`）：单字 ROM 的引擎 10,884 um²（其中 55% 是时序单元），10 ns 时钟下关键路径 24.7 ns（WNS -16.3 ns，把 MAC 阵列改为自由运行、去掉宽使能网之前是 -20.1 ns）——这条路径是控制而非算术：七级门、每级 4–5 ns 都来自扇出。更重要的是，单独综合 448 字、512 位字宽的 ROM（随机内容）是 0.34 mm²、地址到输出 478.7 ns（tt 角）——切片的瓶颈是 ROM 而不是 ALU，正如设计草案所警告。因此必须先做「分 bank + 每 bank 输出寄存」的 ROM（草案 §2.2：4 个 bank × 112 字，读分成 bank 译码与列选择），再谈频率。

N5 用 LibreLane 3.1.0.dev3 的 Classic 流程（Yosys、OpenROAD、OpenRCX、Magic、KLayout，本地 GF180MCU PDK 加 `--manual-pdk`）在 10 ns 时钟下布局布线了带 448 字 ROM 的引擎，产出 GDS、DEF、LEF、SDC 与三个角的 SPEF。两次布局的结果：600×600 µm 芯片上标准单元 78,669 µm²（占 21.9%）、12,973 个实例、63.7 mW、最差 IR 压降 2.18 mV，tt/25C/5.00V 的 WNS −0.714 ns（10.71 ns 周期，93.3 MHz）、ss/125C/4.50V 的 WNS −9.97 ns（19.97 ns，50.1 MHz）；900×900 µm 的宽松布局代价是时序：91,522 µm²、21,349 个实例、64.1 mW、1.65 mV，tt 的 WNS −0.87 ns、ss 的 −10.12 ns。保持时间在所有角都是 0，KLayout 与 Magic 的 DRC 报告为空，天线与电源网格违例为 0。所以物理设计达到了 ss 角允许的 50 MHz 与 tt 下的 93 MHz。50 MHz 现在已按构造收口：`tile_mac` 的组和原本是循环累加，综合后是一条 GroupSize 深的加法链（ss 角 22.3 ns 的关键路径），现在改成 8 条 GroupSize/8 的子链再加三级树合并（模 2^ACC 加法可结合，所有引擎向量逐位相同，68 个 rtl-taalas 测试原样通过）。用 LibreLane 在 20 ns、450 um 芯片、窄激活口（65 脚）重测：tt/25C/5.0V +8.61 ns、nom ss/125C/4.5V +0.13 ns、max ss −0.019 ns、保持 +0.11 ns、标准单元 181,266 um2、27.2 mW、最差 IR 1.63 mV；同一配置在改树之前 nom ss 是 −0.91 ns。因此 50 MHz 在典型角与标称慢角都收口，离最严苛的角只差 19 ps；ROM 分 bank 因此不再是 50 MHz 的必要条件，而是冲 100 MHz 的手段，与设计草案 §2.1 的 S2 拆分一起。不过分 bank 仍在 RTL 里（`taalas-tile-rom/3`：4 个 bank，每个 2^(ADDR-2) 字，每个 bank 一个 $readmemh 文件，用地址高两位选 bank，读寄存器仍是引擎的 S1）：四个 bank 在综合里不改变任何值——Yosys 把四个数组映射成与平面数组相同的混选网络，流程指标逐位相同——所以它的价值在物理侧（层次化布局或硬 ROM 宏），不在逻辑侧；每个 bank 一个文件正是将来做硬宏的输入。设计草案 §2.1 的拆分也已进 RTL：`tile_mac` 在组树（S2b）之前先寄存乘积（S2a），首块标志随数据穿过两级（`clear` 改名为 `first`、内部delay 两次），引擎在 drain 期间把激活多保持一周期；10 ns 时钟下 tt 角 −0.82 ns（450 um 芯片，不拆分时为 −1.39 ns）。再加第四级流水——8 个组累加器的行和先寄存再输出 `y`——450 um 上变成 −0.286 ns、500 um 上 **−0.062 ns**（100 MHz 的 99.4%；450 um 版本 181,266 um2、74.8 mW、最差 IR 3.48 mV，500 um 版本 226,747 um2、75.7 mW、保持 0.22 ns）。也就是说 tt 角 100 MHz 只差 62 ps，而 50 MHz 仍是已收口的实测目标。频率结论原来取决于工艺角而不是数据通路：标准单元触发器本身在 ss/125C/1.62V 下 clk→Q 6.29 ns、建立时间 3.18 ns（不含逻辑就已 9.5 ns），而 tt/25C/1.80V 下是 2.50 + 1.00 ns（3.5 ns）；两级触发器经一个反相器的路径在 ss 下测得 10.6 ns，与该 liberty 一致。因此 100 MHz 在 tt 下宽裕、在 ss 下用这个库做不到，频率声明所用的工艺角与电压成为需要业主决定的事项，记录在设计草案（drafts/tile-180nm-pipeline.md 的 N4 发现）。注意力模块已经实现，不只是草图：`tile_attention` 一次处理一个头，用一块专用的 17×17 乘法器，分数为 `(dot * att_scale) >> (fq + fk + 15 - SCORE_FRAC)` 并饱和到 int32，因果掩码按 j ≤ position 推进，`d = min(smax − s, EXP_RANGE·2^SCORE_FRAC − 1)` 以 `d >> (SCORE_FRAC − 5)` 索引 exp 表（表输出寄存，因此状态机等一个周期），`p = (e << 15) / Σe` 是余数左移再与除数比较的 restoring 除法，输出为 `(Σ p·v) >> 15` 饱和到 int16。参考模型是 `llmchip_rtl.tile.attention`，由 `test_attention_reference.py` 对拍 `GraphL2.attention`，RTL 由 `test_tile_attention.py` 对拍参考；这块专用乘法器不是调度模型里共享的 MAC 阵列，因此它的周期数还没有进入调度表。生成器还写切片的 bank 套的硬宏视图（`rom_macro/`：黑盒、仿真模型、暂定 LEF 与清单），内容就是四个 `rom_bank0..3.hex`。整模型的 W4 ROM 约 1.98 Gbit，因此 Q-full bundle 的十六进制内容有数百 MB，生成是一次长运行；每个引擎的寄存器都带复位，符合规则要求。
- **tile 的 R4 验证（R4）**：`rtl-taalas` 的 `tile_verify` 处理器运行模板套件、功能化 S-1 切片对 `GraphL2` 的逐位比较、时钟域检查与串联层验证台；`flow-six` 的 VER 阶段用 `ver.unit`、`ver.slice`、`ver.cdc`、`ver.layer`、`ver.coverage`、`ver.formal`、`ver.mutation`、`ver.dft`、`rtl.tile.equivalence` 判定。层内各算子现在都已逐一对 `GraphL2` 验证：线性（q/k/v/o/gate/up/down）、RMSNorm（attn/mlp 与逐头 q/k）、RoPE、KV 缓存与 attention、SiLU 门控和残差加法。逐位线性的 `tile_linear` 按 `GraphL2.linear` 应用量化组尾数、矩阵指数、半上舍入移位与偏置；`tile_slice_top` 是 S-1 的 q_proj 切片，`test_tile_slice.py` 用真实的 `attn_norm` 激活比较四个 token（第 0 个头的 q 行）逐位一致。串联验证台运行 `ver.layer` `tile_layer_attn_tb.v`，覆盖两个 Q-full 层：每层 17 个观测点（attn_norm、q/k/v、q_norm/k_norm、RoPE、KV 缓存上的 attention、o_proj、残差、mlp_norm、gate/up、SiLU、down_proj 与第二次残差）都与 `GraphL2` 逐位一致，共 34 次比较。`ver.coverage` 对同一验证台报告 96% 行覆盖，`ver.formal` 证明残差加的饱和和性质（含反例），`ver.mutation` 对 `tile_residual` 跑 50 个变异体的 MCY（等价分类后 100%，12 个 `NOCHANGE`），`ver.dft` 是就绪审计（四个反例），不是扫描插入或 MBIST；`rtl.tile.equivalence` 把两个层验证台与 final_norm/lm_head 尾部串起来（用 `rtl.tile.equivalence` 而非 `rtl.equivalence`：后者在 `flow-default` 中由 `sim-verilator/verify` 定义）：第 0 层残差输入第 1 层，再经 final_norm 和 lm_head 产生全部 128 个 logits，与 `GraphL2` 逐位一致（1 项测试、128 个 logits）。`verif` 插件边界目前由 `tile_verify` 处理器承担。
- **片上调度与定向测试（B1/B2，2026-10-30）**：`tile_layer` 是 bundle 里的模板：一个状态对应调度表的一个相位，驱动完整的解码层（attn_norm、q/k/v、逐头 q/k norm、RoPE、KV 写、attention、o_proj、残差、mlp_norm、gate/up、SiLU、down_proj、残差），相位时序不再写在验证台里。测试以四个连续位置驱动它（KV 缓存自然写入），第 0/1 层的 17 个观测点全部与 `GraphL2` 逐位一致。`ver.directed` 覆盖上电、忙碌时再次 START、推理中途复位后重新运行、单 token 与上下文边界；主机接口 `tile_host` 用 SPI 帧接收 START、24 位 TOKEN、LENGTH 与观测地址，非法命令和忙碌时的 START 都置错误位而不改变状态，状态字节可读回（`test_tile_host.py`）。层的残差宽度已参数化（`RES`，默认 16，与 tiny 夹具一致）：`tile_rmsnorm` 的输入宽度、`tile_residual` 的两个操作数与结果宽度、`tile_layer` 的残差缓冲都随它变；16 位路径有回归（层、定向与 bundle 测试），Qwen 校准规格的 24 位路径尚未做逐层回归，随整模型的层链一起做。整模型的层链、芯片自跑 8 token、24 层逐层回归、≥20 提示的全芯片回归与 vchip 的 tile 后端（B3–B6）仍未做：`tile_qfull_top` 仍按 `done` 串行，芯片还没有主机 START+token 接口，`vchip` 仍只支持 `taalas-spatial/1`。
- **按 CUDA 构建的 torch**：Linux x86-64 上 `.venv` 安装 CUDA 13 版 torch（没有 GPU 时退回 CPU），安装体积比 CPU 版大约 3 GB。
- **S3 的速度和面积**（参考模型的占位模型，三值，2 条通道，8 MHz，2026-10-20 实测）：旧公式给出 10.1 tok/s；旧设计（SPI、无缓冲）的精确周期是每 token 1,024,601，即 7.8 tok/s。四线 SPI 为 352,505 个周期、22.7 tok/s（2.9 倍）；加注意力缓冲 34.6 tok/s；全部缓冲 43.8 tok/s。即便用四线 SPI，仍有 77.7% 的周期在等片外存储。在 1.2 × 2 mm 晶粒上只有“四线 SPI、无缓冲”能放下：综合出的单元面积 287k µm²，核心区 497k µm²。布局已到极限，而且对密度设置不单调：同一网表在 70% 和 85% 时放下并满足时序（建立时间余量 +22 ns，约 9.7 MHz 也能满足），在 75% 和 80% 时详细布局失败；预流片取 70%（`PRERUN.placementDensityPct`）。旧的 SPI 设计在原来的 62% 时也已无法布线。加注意力缓冲（332k µm²）或全部缓冲（398k µm²）在试过的每个密度（62–85%）下详细布局都失败；把两条通道改成一条也无法布线。四线 SPI 的焊盘是双向 `bi_t` 单元，只在不驱动时打开输入（IE = ¬OE）：焊盘库只在 IE 为低时定义 A → PAD 的时序弧，IE 固定为高时输出方向不受时序约束，STA 报告这四个引脚未约束。门级仿真用的焊盘功能模型（`plugins/eda-gf180/sim/pads.v`）原先把 `bi_t` 的输入通路固定为 0，这只在没有双向引脚时成立；现在 IE 为高时它输出焊盘上的值。SDF 时序仿真的测试平台按设计的实际引脚（SPI 或四线 SPI）描述片外存储。工艺库的面积估算看不出这一点：它的控制逻辑是 0.05 mm²（经验值），综合结果约 0.19 mm²；利用率取 0.7（经验值），实测 0.58 能放下、0.67 放不下。在用这些实现结果校准工艺库之前，`explore` 给出的“放得下”偏乐观；设计能否放下只能由 `eda-gf180 implement` 证明。
- **数值分析结果**（int8、分组 32、四舍五入、饱和、逐层小数位、残差流 24 位；4 个提示 × 8 个 token、3 段文本；开发机，2026-10-19）：stories260K 困惑度 3.16 → 3.17，下一 token 一致率 98.6%，1 秒；SmolLM2-135M 12.64 → 12.76，94.3%，30 秒；Qwen3-0.6B 23.00 → 22.39，88.8%，136 秒；DeepSeek-R1-Distill-Qwen-1.5B 41.41 → 35.92，88.8%，584 秒。int4 时 SmolLM2 升到 22.6，Qwen3 升到 28.6。每类张量只用一个 16 位格式时 SmolLM2 会崩溃（困惑度约 290 万）：少数层的残差流值接近 20000。`numeric.accuracy` 要求下一 token 一致率 ≥ 0.9，Qwen3 和 DeepSeek 在 int8 时略差一点没有达到。
- **虚拟芯片的容器清理**（R2a 中发现，2026-10-25 修复）：`llmchip_vchip/bridges/` 原用 `podman rm -f -t 0` 移除仿真容器，而本机的 Podman 3.4 没有 `rm -t` 选项，这条命令直接报错退出，容器没有被移除；`tests/vchip.spec.ts` 给容器 30 秒自行结束，所以没有发现。现在 `remove_container` 先杀再删（`podman kill`、`podman rm -f`），并有断言命令顺序、且不留下容器的单元测试；SIGTERM 路径每次调用给 1 秒，落在内核 2 秒的取消宽限内。改动后的完整 L3 一致性检查通过（T-01–T-19，T-06 n/a，2026-10-25）。基准沙箱用同样的顺序。
- **虚拟芯片测试环境**（`llmchip-vchip`，负责人草案设计的 L1 与 L3）：后端为 `reference`、`numeric`、`process`、`rtl`。RTL 路径只覆盖一个生成器 `taalas-spatial/1`（5K 占位模型；受该生成器的形状限制）；`taalas-rom/1` 还没有 RTL 生成器。没有门级后端（设计 T-20，L3+），也没有性能模型后端。完整的 20 个提示词 L3 运行须显式开启（`LLMCHIP_VCHIP_L3=1`，约 11 分钟）；有镜像时默认测试只跑 4 个提示词。困惑度需要 `logits` 能力。独立运行（`vchip`）时，运行不是产物，按芯片策略自行判定（ADR-010）；经内核插件 `vchip`（L4）运行时，运行与报告是产物，由流程的检查判定，但这些检查不属于流程的任何阶段。插件只从 `llmchip-numeric-spec/1` 规格构建芯片（llama2.c 模型的 S2；导入模型的计算图数值规格会被拒绝），也只运行 `taalas-spatial/1` 的 bundle。它的仿真器在自己的会话和容器中运行：运行被取消时会被移除，但内核崩溃后，内核的遗留进程清理只杀插件的进程组，仿真容器可能留下（`podman ps --filter name=llmchip-vchip`）。Web UI 页面发起运行并显示报告；没有单独的 HTTP API（内核 API 就是 API），也没有采样。
- **分析检查不通过也会开事故单**，与所有不通过的检查一样（FR-VER-06），所以在 S2 试验台上尝试较差的方案会让事故列表变长。

### 7.3 代码与上游文档的冲突（未解决，已上报）

| # | 上游表述 | 代码 | 范围 |
|---|---|---|---|
| C1 | ADR-008 / 架构 §4.8：网关是唯一持有对外连接的模块；提供方只构造请求 | `createProvider` 先调用 `gateway.check`，再自己调用 `fetch` | K8 |
| C2 | 架构 §9.3 与 §12.1：容器根文件系统只读，只能写运行工作区的 `out/` | `Scheduler.containerCommand` 没有传 `--read-only`；`out/` 与 `work/` 都以可写方式挂载 | IF-3 |
| C3 | 架构 §14 的目录布局（`agent-runtime/`、`llm-providers/`、`mcp-server/`、`plugin-sdk-ts/` 包） | 智能体运行时与提供方在 `packages/kernel/src/agents/`，MCP 在 `packages/cli/src/mcp.ts`，SDK 为 `packages/plugin-sdk` | 布局（“建议”） |
| C4 | 架构 §6.2：变更集写入项目 git 工作目录“随 M2 的生成器到来” | M2–M4 都没有实现 | §6 |
| C5 | 架构 §3、§6.1：智能体运行时与 LLM 提供方是内核内系统插件 | `AgentRuntime` 是 `Kernel` 构造函数直接创建的普通类；提供方是内核代码；都不经过 `PluginHost` | §6、ADR-001 |
| C6 | 架构 §4.3：运行成功结束后，才把它的输出一次性登记为产物（检查点除外，R1） | 输出逐个入库；后面的输出无效时，前面的输出留在库中，运行记录却不列出它们 | K3/K4 |
| C7 | 架构 §5 第 5 条：关闭时先停止智能体运行时，再刷写事件库 | `Kernel.shutdown` 从不停止 `AgentRuntime`，也不先结束正在运行的 `tasks.run` 循环。它能否在数据库关闭后追加事件，尚未核实 | K1、§6 |
| C8 | FR-QNT-08：快速集判定“题少时如实判无法判定” | FR-QNT-04 的规则按分差及其区间判定。只有 5 题时分差是 20 点的整数倍，所以 L0 与 L2 在完全相同的题上答对时（分差 0、区间 [0, 0]，例如双方都是 0/5），快速集检查会通过，而不是无法判定 | FR-QNT-04/08、检查标准 |

负责人决定之前，保持代码现状，不要基于这些条目的任何一方继续开发。

## R5 综合与 DFT

SYN 阶段已接入：`syn-yosys` 在固定的 `iic-osic-tools` 镜像中运行 Yosys，把 S-1 切片映射到 sky130A `sky130_fd_sc_hd`、GF180MCU `gf180mcu_fd_sc_mcu7t5v0` 与 IHP SG13G2 `sg13g2_stdcell`。`syn.synth.<pdk>` 要求核心面积、单元数与 PDK 指标非零。2026-10-26 在 S-1 切片上的实测：sky130 118,591 um^2 / 12,267 cells / 1,506 flops；GF180MCU 372,025 um^2 / 14,883 cells / 1,506 flops；IHP SG13G2 215,659 um^2 / 15,533 cells / 1,506 flops。OpenSTA 在 100 ns 时钟下报告 sky130A 最差裕量 78.15 ns、GF180MCU 45.73 ns、IHP SG13G2 87.59 ns，`syn.timing.<pdk>` 要求 WNS 非负。Fault 适配器现在能在三个 PDK 上插入扫描链：内部 1553 + 边界 142，总长 1583；`syn.dft.scan.<pdk>` 要求内部链非空。全 S-1 的 cut 网表上 Fault 0.9.4 的 ATPG 直接 core dump；`syn.dft.atpg.<pdk>` 因此在有记录的组合 `tile_residual_dft` 目标上运行（10,304 个固定型故障点、20 组向量；sky130 99.16%、GF180MCU 96.79%、IHP SG13G2 98.65%），并要求拉零输出的反例低于 95%。S-1 全片 ATPG 仍作为工具限制记录，不冒充通过：从源码构建的第二个工具 Atalanta 2.0 在 61k 故障的 RMSNorm bench 上也崩溃，本机可用的开源 ATPG 都覆盖不了完整 S-1 网表。
