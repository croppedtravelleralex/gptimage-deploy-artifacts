# 维护文档总入口

本目录是本项目的维护真相源入口。以后接手、排障、做路线图调整，先看这里，再进代码。

## 本目录用途

- 固定沉淀当前事实、长期目标、路线图、改进待办、交接摘要和月度日志。
- 避免维护知识只停留在聊天记录里。
- 让后续接手的人先看文档，再看代码。

## 阅读顺序

1. `02-current-state.md`：当前事实主档（含生产环境 Panda VPS 信息）
2. `05-ai-maintenance-playbook.md`：后续 AI 的接手规则
3. `06-handoff.md`：本轮交接摘要
4. `07-account-pool-performance-upgrade.md`：账号池与性能升级落地方案
5. `sync-strategy.md`：Panda 同步、水位线与公网入口保护
6. `performance-acceptance-test-plan.md`：多轮测试与验收计划
7. `08-image-pipeline-newapi-async-plan.md`：IMG-012 方案与实施状态（§13：busy_6=0 已验收，24 路成功率待修 NewAPI 传输层）
8. `01-project-charter.md`：项目长期目标和边界
9. `03-roadmap.md`：近期 / 中期 / 远期路线图
10. `04-improvement-backlog.md`：长期改进池
11. `logs/YYYY/YYYY-MM.md`：月度历史

## 目录地图

- `01-project-charter.md`：项目愿景、完成态、边界
- `02-current-state.md`：当前状态、生产部署、已验证事实、风险、下一步
- `03-roadmap.md`：Now / Next / Later
- `04-improvement-backlog.md`：长期待办与技术债
- `05-ai-maintenance-playbook.md`：AI 接手和回写纪律
- `06-handoff.md`：简短交接摘要
- `07-account-pool-performance-upgrade.md`：账号池、本地 SQLite、Panda SQLite、maintenance、b64 回传落地设计
- `sync-strategy.md`：Panda 水位线、动态公网 IP 同步保护、HMAC、幂等和限频
- `performance-acceptance-test-plan.md`：优化完成后的多轮测试、压测和验收标准
- `08-image-pipeline-newapi-async-plan.md`：IMG-012 sync-over-async（已部署 Panda；§13 实施状态与压测结果）
- `quota-semantics.md`：**额度三态规范**（真无限额 / 未知 / 数值）
- `deployment.md`：部署与升级（含 Panda 生产热更新）
- `logs/YYYY/YYYY-MM.md`：每月工作记录

## 真相来源优先级

按下面顺序判断事实：

1. 代码、配置、测试、数据库、命令结果
2. `docs/02-current-state.md`
3. `docs/03-roadmap.md`
4. `docs/06-handoff.md`
5. `docs/logs/`
6. 项目根目录 `README.md`
7. 其他专题文档

如果冲突，以更高优先级来源为准，并在同一轮把文档修正。

## 更新纪律

- 先确认事实，再写文档。
- 不把推测写成结论；不确定项统一标成“待确认”。
- 历史日志只追加，不回写旧日志改历史。
- 代码、测试或命令结果发生变化时，优先更新 `02-current-state.md`。
- 若路线、优先级或长期待办变化，再同步更新 `03-roadmap.md` 和 `04-improvement-backlog.md`。

## 当前维护入口

- 想快速看现在：先读 `02-current-state.md`
- 想继续上次工作：先读 `06-handoff.md`
- 想改额度 / 健康页 / 账号展示：先读 `quota-semantics.md`
- 想上生产热更新：先读 `deployment.md`「Panda 生产热更新」
- 想按固定规则接手：先读 `05-ai-maintenance-playbook.md`
- 想执行账号池性能升级：先读根目录 `../plan.md`，再读 `07-account-pool-performance-upgrade.md`、`sync-strategy.md` 和 `performance-acceptance-test-plan.md`
- 想执行 NewAPI 生图入口异步化 / 6+burst8 / 带宽窗口：先读 `08-image-pipeline-newapi-async-plan.md`

## 专题资料（非事实主档，作补充）

- `docs/feature-status.en.md`
- `docs/flaresolverr-cloudflare.md`
- `docs/upstream-sse-conversation.md`
- `docs/review.md`

根目录 `README.md` 为对外介绍，不作为内部维护真相源。



