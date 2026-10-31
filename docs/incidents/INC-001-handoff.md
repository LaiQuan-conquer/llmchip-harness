# INC-001 交接（新会话从这里开始）

- 仓库：`llmchip-harness`，分支 `r3`，最新提交见下方；工作树干净。
- 事故状态：**P0 未关闭**（B5/B6 长跑未完成）。
- 权威记录：`docs/incidents/INC-001-analysis-and-remediation.zh.md`（§12～§16）。

## 已完成

- §16.7 第 1 项：T-17 按后端声明检查集合（tile 为 `busy_pin_counted`）；conform/bridge 13 passed。
- §16.7 第 2 项：`build.json` 记录实际容器命令；`TileSim` 启动前校验 `build.json` 与二进制 sha256，缺失/篡改即拒绝；阴性测试通过。
- B5 准备脚本 `runs/inc001/b5_prepare.py`：已用 `--n 1 --tokens 8` 验证，产出 `/root/llmchip-runs/inc001/{prompts.json,golden.json,prepare-identity.json}`。
- 干净构建产物：`/root/llmchip-runs/inc001/build/0c2a1cf38930e479/`（含 `build.json` 与匹配的二进制）。

## 当前脚本与已知问题

- `runs/inc001/b5_run.py`：TileSim 逐 token、分片写 `shards/<id>.json`、带 build/prepare 身份；但 `find_build()` 只扫描 `/tmp/qwen_build_gated`，需加入 `/root/llmchip-runs/inc001/build/*/`（干净构建在那里）。
- `/tmp/qwen_build_gated/*` 的二进制都被 TICKS 调试重链接过，与 `build.json` 不匹配，**不要**用它们做 B5。
- 预填是逐 token step（每步约 385 秒）；验证时用 `--prompt-tokens 2 --tokens 2`（约 4 步）。
- 全量 golden（20 提示 × 64 token）需 `b5_prepare.py --n 20 --tokens 64`，CPU 上约数小时，建议 nohup。

## 新会话的下一步（按序）

1. 修 `b5_run.py` 的 `find_build` 扫描根，加入 `/root/llmchip-runs/inc001/build`；
2. 用干净构建跑 `b5_run.py --index 0 --tokens 2 --prompt-tokens 2`，确认 PASS 与分片身份；
3. 后台跑 `b5_prepare.py --n 20 --tokens 64`（nohup），产出完整 golden；
4. 1/2/4 并发校准（RSS、吞吐、缺页），再决定 20 并行；
5. nohup 启动 20×64 与 vchip conform/bench 长跑，结果写 `/root/llmchip-runs/inc001`；
6. 据实更新 INC-001 §15/§16 与事故状态；未跑/失败项如实列出。

## 约束

- 不降低判据、不删检查、不把未运行写为通过；
- 每个结果绑定 `build.json`/`prepare-identity.json`/提示 token IDs；
- 长跑用 nohup，生命周期不依赖交互会话。
