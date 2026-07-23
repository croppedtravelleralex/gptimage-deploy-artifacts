# 维护文档总入口

最后更新：2026-07-22

本目录是维护真相源。冲突时：代码/命令结果 > `02` > `03`/`04` > `06` > `logs/`。

## 按意图检索

| 你想… | 先读 |
|--------|------|
| 现在池面/生产事实 | `02-current-state.md` |
| **新号怎么产出（固定链路）** | `16-camoufox-stable-pipeline.md` → `scripts/outlook_camoufox_stable_register.py` |
| 继续上次工作 | `06-handoff.md` |
| **CF 403 / 能不能协议绕过** | `17-cf403-and-egress.md` |
| **严格纯 HTTP 生图 / 不出图 / Turnstile** | `20-pure-http-image-sentinel-todo.md` → `04` **PROTO-PURE-HTTP** |
| **新开对话：按逆向结果改造生产** | `04` **PROTO-REFACTOR**（上传等）；生图 Sentinel 优先 `20`；任务书 `18`（挖矿已完成） |
| **协议全量逆向目录（A–F / 机房 IP 前提）** | `19-protocol-full-reverse-catalog.md` → `captures/spa/` |
| 空池 / 不可调度归因 | `04` SCHED-001；`11` L2；脚本模式 `_panda_why_not_schedulable` |
| estuary / SSE / 协议差距与改造 | `12`；挖全进度 `19`；生图阻塞 `20`；改造待办 `04` |
| 拟人化 / 降封是否有效 | `12` 风控复盘；`10-human-like-workload-plan.md`（对照未齐） |
| 养号 / 自带 GPT / LLM 分层 | `11-llm-ops-and-text-nurture.md` |
| 性能占用 / Go·Rust 重写预估 | `13-performance-and-rewrite-estimate.md` |
| Rust 重写（进度 / Phase A→E） | `14-rust-rewrite-plan.md` → `../gptimage-gateway-rs/plan.md`（权威） |
| 任务库归档 / 日志轮转 | `15-store-archive-and-log-rotation.md` |
| 调度进出台开关 | `02` 摘要；API `/api/accounts/scheduling` |
| Outlook 长寿 / sticky / 成熟期 | `09-outlook-longevity-99-plan.md` |
| NewAPI 生图 / admission | `08-image-pipeline-newapi-async-plan.md` |
| **生图调度 / 前端 P-C / 多阶段流水线** | `21-image-scheduling-and-pipeline.md` |
| 部署 Panda | `deployment.md`（artifacts → pull overlay；禁 scp/远程 build） |
| 额度三态 | `quota-semantics.md` |
| AI 接手纪律 | `05-ai-maintenance-playbook.md` |

## 阅读顺序（新人）

1. `02-current-state.md`（只看文首摘要）
2. `16-camoufox-stable-pipeline.md`（新号产出）
3. `06-handoff.md`
4. `11` + `12`（LLM / 协议差距）
4. `09` / `10`（长寿与拟人化方案）
5. `04-improvement-backlog.md`（待办）
6. `logs/YYYY/`、`archive/`（历史，非当前事实）

## 目录地图

| 文件 | 用途 |
|------|------|
| `01-project-charter.md` | 愿景与边界 |
| `02-current-state.md` | **当前事实**（摘要权威；下文历史勿覆盖摘要） |
| `03-roadmap.md` | Now / Next / Later |
| `04-improvement-backlog.md` | 工程待办 |
| `05-ai-maintenance-playbook.md` | AI 接手规则 |
| `06-handoff.md` | 短交接 |
| `07` / `sync-strategy` / `performance-acceptance-*` | 号池性能与验收（偏历史方案） |
| `08-image-pipeline-newapi-async-plan.md` | 生图 sync/async |
| `09-outlook-longevity-99-plan.md` | Outlook 99+ |
| `10-human-like-workload-plan.md` | 拟人化容量 |
| `11-llm-ops-and-text-nurture.md` | LLM 分层与养号红线 |
| `12-protocol-gap-vs-web.md` | 逆向差距与风控复盘 |
| `16-camoufox-stable-pipeline.md` | **Camoufox 新号固定链路**（取号→检查→注册→观察） |
| `17-cf403-and-egress.md` | CF 403 裁决（不可协议绕过） |
| `18-openai-web-reverse-proxy-brief.md` | 新对话：SPA→反代任务书 |
| `19-protocol-full-reverse-catalog.md` | **协议全量逆向 A–F 目录/看板**（机房 IP 前提） |
| `20-pure-http-image-sentinel-todo.md` | **严格纯 HTTP 生图：已做/未做/验收全量待办**（Turnstile VM） |
| `quota-semantics.md` / `deployment.md` | 额度 / 部署 |
| `upstream-sse-conversation.md` | SSE 事件语义 |
| `logs/` / `archive/` | 月度日志 / 归档长文 |

## 更新纪律

- 先证据后文档；推测标「待确认」。
- `logs/` 只追加；不回写旧月日志改历史。
- 状态变化优先改 `02` 摘要；路线/待办同步 `03`/`04`。
