# 当前实现与扩展指南

[English](implementation.md) | 中文

| 项目 | 内容 |
|---|---|
| 文档版本 | v0.1 |
| 日期 | 2026-10-19 |
| 对应代码 | 提交 `9cc76a0`（M4），以及演示流程页面、`yarn demo`、模型导入与图还原 |
| 上游文档 | [需求分析](<../Coding Harness MVP Requirement Analysis.zh.md>) v0.6、[微内核架构](<../Microkernel Architecture.zh.md>) v0.9 |

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
| M2 模型到规格（S1–S3，G0–G2） | `9284a99` | 在 stories260K 与占位模型上完成；没有训练好的参考模型 | `tests/m2-flow.spec.ts` |
| M3 RTL 与验证（S4–S5，G3） | `3842361` | 在占位模型上完成 | `tests/m3-flow.spec.ts`（需 EDA 镜像） |
| M4 物理实现、签核、流片包（S6–S8，G4） | `9cc76a0` | 在占位模型上完成，芯片 1.2×2 mm | `tests/m4-flow.spec.ts`（须显式开启，实测 39 分钟） |
| M5 迁移到流片工艺 | — | 只有评估与失效机制（FR-MIG）；没有 NDA PDK | `tests/m4-kernel.spec.ts`、`plugins/estimator/tests/test_migration.py` |
| M6 回片测试（S9） | — | 未开始 | — |

因此 M1 整体**未**通过验收：缺少 AC-M1-01（安装器）和桌面外壳，见 §7。

### 1.2 已实现

| 领域 | 能做什么 | 入口 |
|---|---|---|
| 内核守护进程 | 启动、单实例锁、恢复、有序关闭 | `harnessd serve`、[kernel.ts](../packages/kernel/src/kernel.ts) `Kernel.start` |
| K1 事件库 | 追加式 SQLite 日志、哈希链、投影、重放 | [events/store.ts](../packages/kernel/src/events/store.ts) |
| K2 产物库 | 内容寻址、只读对象、血缘、标签传播 | [artifacts/store.ts](../packages/kernel/src/artifacts/store.ts) |
| K3/K6/K7 工作流 | 项目、槽位、分支、阶段、判定、快照、审批、条件、豁免、放行、导出、迁移 | [workflow/engine.ts](../packages/kernel/src/workflow/engine.ts) |
| K4 调度器 | 持久队列、进程与容器执行、超时、取消、重试、暂停 | [runs/scheduler.ts](../packages/kernel/src/runs/scheduler.ts) |
| K5 API 策略 | 区分所有者令牌与外部令牌；方法白名单 | [security/policy.ts](../packages/kernel/src/security/policy.ts) |
| K8 出口网关 | 标签、目的地上限、NDA 指纹、去密规则、审计 | [security/egress.ts](../packages/kernel/src/security/egress.ts) |
| K9 插件注册表 | 清单扫描、校验、数据导入 | [plugins/registry.ts](../packages/kernel/src/plugins/registry.ts) |
| K10 配置与密钥 | 分层配置、AES-256-GCM 密钥文件、脱敏 | [config/](../packages/kernel/src/config/) |
| 智能体运行时 | 托管智能体：工具检查、变更集、接手包、死循环与预算停止 | [agents/runtime.ts](../packages/kernel/src/agents/runtime.ts)、[agents/providers.ts](../packages/kernel/src/agents/providers.ts) |
| IF-1 内核 API | WebSocket `/v1/rpc` 上的 JSON-RPC 2.0，以及 HTTP `/v1/artifacts` | [server.ts](../packages/kernel/src/server.ts)、[client](../packages/client/src/index.ts) |
| IF-2 插件协议 | stdio 上的 JSON-RPC 2.0，每行一条消息 | [plugins/process.ts](../packages/kernel/src/plugins/process.ts)、两个 SDK |
| IF-3 工具容器 | Podman，无网络，输入与插件目录只读 | `Scheduler.containerCommand` |
| CLI 与 MCP | `llmchip` 命令、通用 `call`、stdio 上的 MCP | [cli/src/main.ts](../packages/cli/src/main.ts)、[cli/src/mcp.ts](../packages/cli/src/mcp.ts) |
| Web UI | 十个页面，中英文。默认页面“演示流程”用通俗语言展示项目的 S0–S9 路径、结果、检查和审批门 | [apps/web/src](../apps/web/src) |
| 领域流程 | 内置插件跑通 S0–S8 与 G0–G4（见 §2.4 的表） | [plugins/](../plugins) |
| 真实模型导入与图还原 | 按本机路径导入已下载的模型目录（safetensors + config.json + tokenizer.json，或 llama2.c），从文件头做张量普查并估算，还原计算图（Llama 家族：Llama、Mistral、SmolLM、Qwen2、Qwen3、llama2.c），执行为 L0，并与 transformers / run.c 移植逐项测试：权重逐位、结构参数、参数总量、每层中间结果、每个提示的 logits、top-1 与贪心生成、单 token 与长序列边界用例。项目自带的 llama2.c 权重（如占位模型）直接从产物导入。S1 页面可选择任一已导入模型或新路径，填写自定义提示，并展示每个测试项 | [model-llama/src/llmchip_model/pipeline.py](../plugins/model-llama/src/llmchip_model/pipeline.py) |

### 1.3 只有接口（已声明，无行为）

- `PluginKind` 中的 `silicon-test`、`ui-panel`、`llm-provider` 和 `parser`（[kernel.schema.json](../packages/kernel-api/schemas/kernel.schema.json)）。没有插件声明它们。LLM 提供方是内核代码，不是插件。
- [flow-default/flow.json](../plugins/flow-default/flow.json) 中的阶段 S9。它有入口审批门（G4），但没有处理器，也没有检查。
- 默认流程中的检查 `sdf.timing` 和 `external.signoff`。它们已定义，但没有审批门使用。`sdf.timing` 要求能力 `sdf-timing-sim`，而没有插件声明该能力，所以它只会判为 `indeterminate`。
- 验证报告（sim-verilator `verify`）中的 `formal` 字段，始终为空（FR-VER-07）。
- 内核内 Cordis 插件的宿主 `PluginHost`（[plugin-host.ts](../packages/kernel/src/plugin-host.ts)）。它能工作，也有测试，但只有 `KernelOptions.plugins` 会向它加载插件，而只有测试会传入这个选项。没有产品插件在内核内运行（见 §7.3 的 C5）。

### 1.4 未实现

Tauri 桌面外壳、安装器、专用 WSL 发行版、首次启动引导、自动拉取镜像。此外还有：CHARTER.md 生成、示例项目、项目导出与导入、项目 git 工作目录（架构 §6.3）、S9、模型训练（FR-QNT-05）、Llama 家族以外和 GGUF 的图还原、`images/` 和 `docs/adr/`。§7.1 按需求编号列出。

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
| 12 | K3/K6/K7 | `Workflow` | 流程包、检查定义、`workflow` 投影 | 事件、产物、注册表、调度器、事故 |
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

在全新主目录上，`plugins.list` 列出以下插件。阶段与审批门来自 `flow-default`（流程版本 0.4.0）。

| 插件 | 运行方式 | 处理器 | 用于 | 单元测试 |
|---|---|---|---|---|
| `flow-default` | data | 流程包：S0–S9、G0–G4、16 项检查、硬约束槽位 | 全部 | 经由 `tests/*-flow.spec.ts` |
| `process-library` | data | 工艺档案 `gf180mcu`、`sephonchitech-180bcd`；`library-180nm`；`package-gf180mcu`；`signoff-gf180mcu` | S0、S3、S6–S8 | 经由估算器与流程测试 |
| `estimator` | process（Python） | `estimate`、`check_views`、`migrate` | S0/G0、S4/S6/S7 阶段准入、FR-MIG | `test_estimate.py`、`test_migration.py` |
| `model-llama` | process（Python） | `synthesize`、`intake`、`reference`、`import`、`recover`、`run_graph`、`compare` | S0（导入）、S1 | `test_model.py`、`test_graph.py` |
| `numeric` | process（Python） | `quantize`、`infer`、`evaluate` | S2/G1 | `test_numeric.py` |
| `arch-taalas` | process（Python） | `design`、`verify_spec` | S3/G2 | `test_arch.py` |
| `rtl-taalas` | process（Python） | `generate`、`check_isa` | S4、S5 | `test_rtl.py` |
| `sim-verilator` | container | `lint`、`qualify`、`verify`、`gatesim`、`sdfsim`、`generate` | S5/G3、S7、Playground | `test_simlib.py` |
| `eda-gf180` | container | `calibrate`、`constraints`、`implement`、`drc`、`lvs`、`sta` | M1.5、S6、S7 | `test_calibrate.py`、`test_physical.py` |
| `packager` | process（Python） | `build`、`check`、`import_external` | S8/G4 | `test_packager.py` |

仅测试用的插件在 [tests/fixtures/plugins](../tests/fixtures/plugins)：

- `stub-tool`（TypeScript）的处理器有 `report`（参数 `violations`、`truncate`、`coverage`、`crash`）、`qualify`、`sleep`、`hang`、`crash`、`fail` 和 `escape`。
- `container-stub`（在容器中运行的 Python）的处理器有 `probe`（探测沙箱）和 `sleep`。
- `test-flow` 是一个三阶段流程包。

各领域插件产出什么：

- **estimator**：可行性、面积、成本与性能估算（FR-EST），给出下限/典型/上限值、每项的来源与置信度，以及缺失的参数。`check_views` 按阶段报告已标定存储实现具备哪些集成视图。`migrate` 依据设计对工艺能力的依赖评估迁移影响（FR-MIG）。
- **process-library**：工艺档案（含迁移所需的工艺数据）、实现库、GF180 封装模板与签核规则。每个数值都记录来源与置信度，经验值在标定前始终是经验值。
- **flow-default**：阶段 S0–S9，审批门 G0–G4 及其成员槽位和检查，阶段准入检查，以及硬约束槽位 `constraints`、`timing-exceptions`、`signoff-rules`。
- **model-llama**：
  - 模型导入与图还原（见下文）。
  - 把 llama2.c 检查点与分词器导入为 S2–S8 使用的 llama2.c 模型 IR。
  - 生成占位模型：采用某个规划模型的结构，固定种子，未训练，附带 `evalset-proxy`。
  - 提供 L0 FP32 参考，即 `run.c` 的 NumPy 移植。它与 `plugins/model-llama/upstream/` 中随仓库提供并编译的 `run.c` 对照检查。
- **numeric**：量化（int8/int4/int3/三值，分组缩放）、数值规格（需求 §3.4.2），以及带观测点轨迹的 L2 逐位精确整数模型。它还做 L2 对 L0 的精度评估（G1 证据），并附带评估集。
- **arch-taalas**：Taalas 风格的硬连线模板。由冻结的数值规格推出芯片规格与 PPA，并检查二者一致（G2 证据）。
- **rtl-taalas**：把数值规格编译成微码化向量引擎，产出一个确定性的 `rtl.bundle`。其中包含：
  - Verilog 核心（数据放在片外 SPI SRAM），以及 SPI 主机接口；
  - 生成为 case 表的 ROM，以及微码；
  - 带 SRAM 模型的 Verilator 测试平台，以及用于门级仿真的 `gl_tb.v`；
  - 指令级模拟器 `isa.py`。

  权重映射规则可逆，并做往返检查。`check_isa` 对照 L2 验证微码。
- **sim-verilator**（在固定版本的 `iic-osic-tools` 镜像中运行 Verilator、Icarus Verilog 和 cocotb）：
  - lint 与仿真器资格认证；
  - 验证计划（回归、定向、边界、穷举、随机、覆盖率；内置默认计划），带首个分歧定位（算子、元素、周期、VCD 窗口）；
  - 布线后网表的门级仿真（L4，使用 PDK 单元模型的 Icarus Verilog）；
  - 带反标统计的 SDF 时序仿真（补充能力，尚未认证）；
  - 为芯片 Playground 生成 L3 结果。
- **eda-gf180**（同一镜像，含 gf180mcuD PDK、Yosys、OpenROAD、Magic、KLayout、Netgen、OpenSTA）：
  - 存储实现标定（FR-EST-13）；
  - 由芯片规格生成约束；
  - 带焊盘环的物理实现（LibreLane Chip 流程）；
  - DRC、LVS，以及含约束完整性检查的 9 角 STA，每项检查只在它所判定的产物上运行。
- **packager**：按工艺模板生成流片包，检查其完整性，并导入与 GDS 绑定的外部签核报告（FR-PKG、FR-SGN-06）。

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
4. `Workflow.loadFlows` 读取每个 `flow.pack` 产物，汇总检查定义。对同一检查 id 有冲突定义的流程包会被拒绝，记为 `FLOW_INVALID` 问题。
5. `Scheduler.recover` 把仍记为 `running` 的运行结束为 `interrupted`。它杀掉该运行遗留的进程组（仅当 `/proc/<pid>/environ` 含 `LLMCHIP_RUN=<id>` 时）及其容器，还有剩余次数时就重试，然后清空 `runs/`。接着加载 `KernelOptions.plugins` 中的内核内插件（只有测试会传入）。
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

### 3.2 路径 B：从运行到审批门放行（S0 → G0 → S1）

单独的估算运行还不是证据。判定只有在其运行的输入全部属于审批门已绑定的成员时，才算作该审批门的证据。以下命令按顺序实际执行过：

```sh
yarn llmchip project create chip --title demo                    # project.created（流程 "default"）
yarn llmchip artifact put req.json --type estimate.request --media-type application/json
yarn llmchip artifact put charter.md --type project.charter
yarn llmchip project bind chip charter <hash>                    # 另有 estimate-request、process-profile、impl-library
yarn llmchip stage enter chip S1                                 # STAGE_ENTRY_BLOCKED: needs released gates: G0
yarn llmchip run start estimator estimate --input request=<req> --input profile=<p> --input library=<l> --wait
yarn llmchip verdict evaluate estimate.feasibility <run> --project chip   # pass
yarn llmchip gate snapshot chip G0                               # 4 个成员、1 条证据、无缺失
yarn llmchip call approvals.decide '{"project":"chip","gate":"G0","decision":"approved"}'
yarn llmchip gate release chip G0                                # approved (released)
yarn llmchip stage enter chip S1                                 # stage.entered
yarn llmchip project bind chip charter <new hash>                # approval.invalidated（由 kernel:workflow 写入）
yarn llmchip gate release chip G0                                # invalidated；再次进入 S2 被阻止
```

[workflow/engine.ts](../packages/kernel/src/workflow/engine.ts) 内部发生了什么：

- **`Workflow.evaluate`**（K6）先确认运行属于检查所指定的插件和处理器，再由 `judge` 按顺序应用检查。
  - 判 `indeterminate` 的情形：执行状态不被接受；解析不是 `complete`；`requiredCapability` 未认证；`completeness` 条件不成立；缺少指标。
  - `criteria` 不满足判 `fail`。若存在检查所允许种类的适用豁免，`fail` 改为 `waived`。
  - 判定的 `subjects` 是运行的输入哈希。引擎追加 `verdict.recorded`。
  - `fail` 或 `indeterminate` 会打开 `check_failed` 事故；运行报告了 `divergence_*` 指标时，事故会带上它们。
  - 若旧版本检查未通过、新版本检查却通过，会打开 `standard_lowered` 事故（AC-M1-13）。
- **`Workflow.snapshot`**（K7）包含两部分。成员是审批门已绑定的槽位，其哈希即快照哈希。证据是每项审批门检查的最新判定，且该判定的 subjects 非空并全部落在成员之内。已确认的迁移涉及该审批门时，迁移之前记录的判定不再计入。
- **`Workflow.decide`** 只允许人类调用。它要求快照完整；决定不是 `rejected` 时，还要求每项审批门检查都已通过或已豁免。审批与快照哈希和证据哈希绑定。
- **`Workflow.release`** 是唯一的放行检查，用于阶段进入与导出。最新审批必须与当前快照哈希一致，未被作废，不是拒绝，且所有条件都已关闭。
- **`Workflow.bind`** 追加 `project.slot_bound`，并在同一事务中，对快照发生变化的每个审批门追加 `approval.invalidated`。`hardConstraintSlots` 中的槽位只能由人类绑定；其他人绑定会打开 `hard_constraint_change` 事故。
- **`Workflow.enterStage`** 要求每个入口审批门都已放行，并且每项入口检查（`{check, slot}`）在已绑定产物上的最新判定为通过。否则以 `STAGE_ENTRY_BLOCKED` 失败并给出原因，包括 `views.<stage>.missing` 指标（FR-PHY-08）。

`tests/flow-helpers.ts` 的 `throughG2` 和 `throughG3Proxy` 用真实插件把同一路径走到 G2 和 G3。`tests/m4-flow.spec.ts` 再继续到 G4 导出。

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
| 运行日志（`run.log`） | `Scheduler.execute` | 任何客户端 | 每次运行都保存，与执行状态无关 |
| 上传（`PUT /v1/artifacts`） | `KernelServer`，仅限所有者令牌 | — | 外部令牌被拒绝并记事故 |
| 运行工作区 `runs/<id>/` | `Scheduler.execute` 创建并删除 | 插件（`in/` 只读） | 每次运行后删除；启动时清空整个 `runs/` |
| 插件进程与容器 | `PluginProcess`（独立进程组）、`podman run --rm --name=llmchip-run-<id>` | — | 结束、取消、超时或内核停止时杀掉；遗留进程在下次启动时杀掉 |
| 判定、审批、豁免、约束、项目 | `Workflow` | API | 都是事件；审批只会被作废，从不删除 |
| 事故 | `Incidents.open` / `close`，由 `Policy`、`Workflow`、`EgressGateway`、`AgentRuntime` 调用 | UI、API | 关闭时必须给出原因和措施（FR-AGT-27） |
| 出口决定 | `EgressGateway.check`，在每次 LLM 请求和每次 MCP 下载之前 | 审计 | 每个请求一条 `egress.decided` |
| 切换到其他提供方时的接手包 | `AgentRuntime.switchProvider` 自行比较接手包的标签与提供方上限 | API | 接手包存为 `agent.handoff`；切换被拒时打开 `handoff_blocked` 事故，不产生 `egress.decided` |
| 任务、智能体步骤、变更集 | `AgentRuntime` | API、UI | 状态都在事件中，新会话可从日志重建（接手） |
| 密钥 | `SecretStore.set` / `remove`（`secret.changed` 只含名称） | 提供方通过 `secret(name)` 读取 | 值从不进入事件或日志（`Redactor`） |
| `run/harnessd.json`、锁 | `Kernel.start` / `shutdown` | CLI、MCP 桥、测试 | 关闭时删除；已退出进程留下的锁会被接管 |

按写入者划分的事件种类：

- Kernel：`kernel.started/stopped`、`projection.rebuilt`
- Scheduler：`kernel.paused`、`run.*`
- ArtifactStore：`artifact.*`
- SecretStore：`secret.changed`
- PluginHost：`plugin.*`
- Workflow：`verdict.*`、`project.*`、`stage.*`、`approval.*`、`waiver.*`、`constraint.*`、`migration.*`、`package.*`
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
  3. 停止调度器（把所有运行中止为 `interrupted`，并等待各自记录结果）；
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

### 5.2 检查、审批门成员或阶段准入检查

示例：阶段准入检查 `impl.views.S4`。它在 [flow-default/flow.json](../plugins/flow-default/flow.json) 中定义，判定 `estimator/check_views` 的运行，并以 `entry.checks: [{check: "impl.views.S4", slot: "storage-impl"}]` 挂在阶段 S4 上。另一个示例是 G0 检查 `estimate.feasibility`。

1. 让处理器报告检查需要的指标，不做任何判定。
2. 在流程包的 `checks` 中加入检查：
   - `id`、`version`、`plugin`、`handler`。
   - `completeness`：结果有意义的前提，不满足即判 `indeterminate`。
   - `criteria`：决定通过或失败。
   - `waiver`：`none`、`risk_acceptance` 或 `foundry_waiver`。
   - 可选的 `requiredCapability`、`requireParse` 和 `acceptExitCodes`。

   标准一旦改变就修改 `version`，这是检测降低标准的依据（AC-M1-13）。
3. 在审批门的 `checks` 中引用它，或在阶段的 `entry.checks` 中引用。前者要求审批门 `members` 槽位中的产物就是运行的输入，否则判定永远不会算作该审批门的证据。
4. 多个流程包可以定义同一检查 id，但所有定义必须完全相同。否则冲突的流程包不会被加载，`plugins list` 会显示一个指明该检查的 `FLOW_INVALID` 问题。
5. 测试：
   - [tests/workflow.spec.ts](../tests/workflow.spec.ts) 用 `test-flow` 验证判定语义。
   - [tests/estimator.spec.ts](../tests/estimator.spec.ts) 覆盖缺数据时的 G0 检查。
   - [tests/calibration.spec.ts](../tests/calibration.spec.ts) 覆盖按视图的阶段准入（需 EDA 镜像）。

新的流程包是带 `flow.pack` 数据文件的 `data` 插件，参见 [test-flow](../tests/fixtures/plugins/test-flow)。项目用 `projects.create {flow}` 选择流程包。

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

阶段还可以有详细演示视图，从卡片上的“详细演示”按钮打开。把它写成 `views/steps/<阶段>.tsx`，并在 `Flow.tsx` 的 `DETAILS` 中注册。示例：[views/steps/S0.tsx](../apps/web/src/views/steps/S0.tsx) 读取该阶段的 `estimate.report`，依次展示问题与回答、输入、面积与速度的计算、反推能放下的模型、检查与审批门，以及一个在项目之外重新运行估算器的表单。说明文字要由结构化字段生成，不要直接用插件的自由文本，这样两种语言读起来都自然。`tests/web-ui.spec.ts` 会以两种语言打开 S0 视图。

### 5.9 内核内系统插件（Cordis）

1. 实现 `InKernelPlugin {id, inject, provides, apply(ctx, kernel)}`（[plugin-host.ts](../packages/kernel/src/plugin-host.ts)）。
2. 只通过 `PluginHost.load` 加载。
3. 所有注册都作为 Cordis effect，以便卸载时清理。

目前只有 `KernelOptions.plugins` 向宿主提供插件，而只有测试会传入它（[packages/kernel/tests/plugin-host.spec.ts](../packages/kernel/tests/plugin-host.spec.ts)）。要让产品插件在内核内运行，还需要新增生产环境的加载路径。这是对架构 §5 防护措施的改动，须先与负责人商定。

---

## 6. 验证方法

### 6.1 命令

以下结果于 2026-10-19 在开发机上测得。环境：WSL2、Node 24.22、uv 0.13、Podman 3.4，已有固定版本的 `iic-osic-tools` 镜像。

| 命令 | 验证什么 | 实测 |
|---|---|---|
| `yarn check:workspace` | 包、引用、`workspace:*` 依赖、vendor 清单与许可证 | "5 packages, 9 vendored packages" |
| `yarn check:generated` | `kernel-api/src/generated` 与 schema 一致 | "up to date (2 files)" |
| `yarn check:python` | ruff lint 与格式检查，SDK 和全部 Python 插件的 pytest | 51 passed |
| `yarn typecheck` | 先构建项目引用，再检查源码、测试、脚本与 Web UI 的类型 | 通过 |
| `yarn build` | 各包的 `lib/`，以及 `apps/web/dist` | 通过 |
| `yarn test` | 全部 Vitest 用例（见下表） | 24 个文件通过、1 个跳过（m4-flow）；93 个测试通过、1 个跳过 |
| `yarn verify` | 依次执行以上全部；每次提交前必须通过 | 退出码 0，镜像齐全时约 12 分钟 |
| `LLMCHIP_FULL_FLOW=1 yarn test tests/m4-flow.spec.ts` | 占位模型上完整的 S0–S8、G4 与导出 | 见 §6.3 |
| `yarn llmchip events verify` / `artifact verify` | 哈希链；已存储内容与哈希一致 | `ok: N … checked` |
| `yarn demo [--until G0..G4]` | 在运行中的内核上，把项目 `demo` 按完整流程的正向路径跑一遍，供“演示流程”页面展示 | S0–G4 全部通过并导出，2241 秒（约 37 分钟），容器配置 16 CPU / 12 GB |
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
| `tests/recovery.spec.ts` | 已确认的写入在反复 SIGKILL 后仍然存在 | NFR-04 | 总是 |
| `tests/workflow.spec.ts` | 解析不完整、崩溃或能力未认证时判 indeterminate；豁免；审批、作废、条件、分支 | AC-M1-03/05/06 | 总是 |
| `tests/agents.spec.ts` | 变更集上的迭代循环、权限、死循环与预算停止、假 PDK 出口拦截、接手、降低标准 | AC-M1-02/07/08/09/13 | 总是 |
| `tests/mcp.spec.ts`、`tests/providers.spec.ts` | MCP 边界；OpenAI 兼容与 Anthropic 适配器（对模拟服务器测试） | FR-INT-05/06 | 总是 |
| `tests/estimator.spec.ts` | 三个模型的完整分项；缺数据时结论为“数据不足”，G0 判 indeterminate | AC-M1-10 | 总是；没有 `.venv` 时**失败**（不会跳过） |
| `tests/m2-flow.spec.ts` | stories260K 上 S0 → G2；新数值规格使 G1/G2 作废 | M2、AC-04 | 总是；没有 `.venv` 时**失败** |
| `tests/m4-kernel.spec.ts` | 导出已放行快照及其血缘、硬约束槽位、迁移导致的作废 | FR-PKG-04、AC-08/10/13 | 总是 |
| `tests/model-import.spec.ts` | 导入真实 Hugging Face 布局的小 Qwen3，普查、按普查估算、图还原、L0、与 transformers 的全量测试；llama2.c 与 run.c 移植对拍；项目自带的占位模型权重；没有 config.json 的模型如实报告而不猜测 | FR-MDL-01/03/05/06 | 总是；需要 `.venv` |
| `plugins/model-llama/tests/test_graph.py` | Llama（共享/不共享输出层）、带滑动窗口的 Mistral、Qwen2、Qwen3 与 transformers 对拍；llama2.c 与 run.c 移植对拍；重复存储的共享输出层；截断的文件；不支持的模型 | FR-MDL | 总是 |
| `tests/kernel-footprint.spec.ts` | harnessd 启动时间与空闲 RSS | NFR-01（内核部分） | Linux |
| `tests/web-ui.spec.ts` | 语言目录一致；在浏览器中以两种语言渲染每个页面 | AC-M1-12 | 浏览器部分需 `apps/web/dist` 与 Chromium |
| `tests/containers.spec.ts` | 无网络、输入只读、取消时移除容器 | IF-3 | 需 `python:3.12-slim` 镜像 |
| `tests/calibration.spec.ts` | 每个存储候选都得出可用或不可用；按视图的阶段准入 | AC-M15-01/02/03 | 需 EDA 镜像 |
| `tests/m3-flow.spec.ts` | 验证计划、G3、注入缺陷的定位 | M3、AC-03/09 | 需 EDA 镜像 |
| `tests/ui-latency.spec.ts` | EDA 运行期间 UI 查询 p95 < 200 ms | NFR-03（内核侧） | 需 EDA 镜像 |
| `tests/m4-flow.spec.ts` | S0–S8、DRC/LVS/STA、时序与 SDF 反例、G4、导出 | M4、AC-02/05/06/11/12/14 | 须同时满足 `LLMCHIP_FULL_FLOW=1` **和** EDA 镜像 |

### 6.3 须显式开启的完整流程

`LLMCHIP_FULL_FLOW=1 yarn test tests/m4-flow.spec.ts` 会运行物理工具。修改 `rtl-taalas`、`eda-gf180`、`packager` 或签核检查后都要运行它。2026-10-19 的结果：通过（1 个测试，2361 秒，约 39 分钟；WSL2 上的 Podman 3.4）。

---

## 7. 当前限制

### 7.1 未实现

| 缺口 | 相关需求 |
|---|---|
| Tauri 桌面外壳、Windows 安装器、专用 WSL 发行版、首次启动引导、自动拉取镜像 | FR-UI-01、FR-UI-03、NFR-08、NFR-13、AC-M1-01、AC-01、ADR-002、ADR-005 |
| CHARTER.md 生成与历史、阶段手册、示例项目 | FR-PRJ-02、FR-PRJ-06、FR-AGT-11 |
| 项目 git 工作目录；合并变更集只记录状态 | FR-TRC-09、架构 §6.2–6.3 |
| S9 回片测试：固件、上位机程序、Playground 中的 L7 | FR-SIL-*、M6 |
| 训练参考模型（负责人推迟；暂以占位模型替代） | FR-QNT-05、需求 §11.1 |
| Llama 家族以外（GPT-2、MoE 等）、GGUF（只做普查）以及带 RoPE 缩放（如 Llama 3.1/3.2）的图还原；在工作台内从 Hugging Face 下载 | FR-MDL-01、FR-MDL-02、FR-MDL-03 |
| 项目导出与导入、复现命令、审计导出 | FR-PRJ-05、FR-TRC-07、FR-TRC-08 |
| 产物清除、GC 与保留策略 | 架构 §4.2、FR-AGT-20 |
| 由内核管理的外部编码智能体 | FR-AGT-03 |
| `docs/adr/` 记录、`images/` 构建定义 | 架构 §14、§15 |

### 7.2 部分实现

- **估算器**：多芯片只给出芯片数（FR-EST-06）。没有参数扫描或 Pareto（FR-EST-08）。标定结果不会自动更新模型（FR-EST-10）。
- **标定**：SRAM 的四种配置中只覆盖 512×8（FR-EST-13）。
- **验证**：只有 `rtl-functional-sim` 是已声明并认证的能力，门级仿真没有能力门控。SDF 仿真能运行，但 `sdf.timing` 无法通过（§1.3、FR-VER-01/03、AC-12 正例）。
- **存储内容检查**（FR-SGN-07）：映射可逆，生成时做往返检查。由于设计使用标准单元 ROM，没有比较宏内容的签核检查。
- **物理实现**：芯片为 1.2×2 mm，不是 2 mm²（负责人决定，需求 v0.6）。设计对象是未训练的占位模型。
- **迁移**：评估与审批门作废可用。下游产物不会被标记为失效（FR-MIG-03、FR-TRC-05）。没有能自动打标签的 NDA PDK 导入通道（FR-INT-04、FR-SEC-01）。
- **智能体**：变更集没有大小阈值（FR-AGT-06）。没有软约束流程（FR-AGT-09）。没有按角色区分的工具集或 LLM（FR-AGT-14/19）。去密接手包只作为建议给出，不会实际生成（FR-AGT-13a）。
- **UI**：没有条件审批与关闭条件的界面。没有事故筛选与跳转链接。除 PNG 外，没有 RTL、波形或版图查看器。设置页缺少 LLM、配额与出口配置（FR-UI-09/10/11/13、FR-AGT-25/26）。
- **CLI**：审批、豁免、去密、导出、迁移没有专用命令，需要用 `llmchip call`（FR-INT-07）。
- **TypeScript 插件 SDK** 没有自己的测试，只通过 `stub-tool` 间接覆盖。
- **导入的 Hugging Face 模型**目前止于 S1：S2–S8 使用的是 llama2.c 模型 IR（`model.ir`），而不是还原的计算图（`model.graph`）。L0 执行计算图时没有 KV 缓存（每步重算整个序列），大模型长生成会很慢。

### 7.3 代码与上游文档的冲突（未解决，已上报）

| # | 上游表述 | 代码 | 范围 |
|---|---|---|---|
| C1 | ADR-008 / 架构 §4.8：网关是唯一持有对外连接的模块；提供方只构造请求 | `createProvider` 先调用 `gateway.check`，再自己调用 `fetch` | K8 |
| C2 | 架构 §9.3 与 §12.1：容器根文件系统只读，只能写运行工作区的 `out/` | `Scheduler.containerCommand` 没有传 `--read-only`；`out/` 与 `work/` 都以可写方式挂载 | IF-3 |
| C3 | 架构 §14 的目录布局（`agent-runtime/`、`llm-providers/`、`mcp-server/`、`plugin-sdk-ts/` 包） | 智能体运行时与提供方在 `packages/kernel/src/agents/`，MCP 在 `packages/cli/src/mcp.ts`，SDK 为 `packages/plugin-sdk` | 布局（“建议”） |
| C4 | 架构 §6.2：变更集写入项目 git 工作目录“随 M2 的生成器到来” | M2–M4 都没有实现 | §6 |
| C5 | 架构 §3、§6.1：智能体运行时与 LLM 提供方是内核内系统插件 | `AgentRuntime` 是 `Kernel` 构造函数直接创建的普通类；提供方是内核代码；都不经过 `PluginHost` | §6、ADR-001 |
| C6 | 架构 §4.3：运行成功结束后，才把它的输出一次性登记为产物 | 输出逐个入库；后面的输出无效时，前面的输出留在库中，运行记录却不列出它们 | K3/K4 |
| C7 | 架构 §5 第 5 条：关闭时先停止智能体运行时，再刷写事件库 | `Kernel.shutdown` 从不停止 `AgentRuntime`，也不先结束正在运行的 `tasks.run` 循环。它能否在数据库关闭后追加事件，尚未核实 | K1、§6 |

负责人决定之前，保持代码现状，不要基于这些条目的任何一方继续开发。
