# 11 — LLM 分层、养号文本与操作日志

最后更新：2026-07-19

## 结论

- **不要**再叠一层 prompt 过滤（`ai_review` / `content_filter` 已存在）。
- **L0 自带 GPT**（号池逆向文本）可做真实短聊、成熟期轻量养号、总结类任务；**不需要** tool-calling。
- **L2 Tool Agent** 适合多步运维 RCA；P0 指标本身靠确定性 API（SCHED-001），Agent 只是编排层。
- **禁止**机械「每 N 张图插一句」假聊（与 `09` / `10` 一致）。协议细节见 `12-protocol-gap-vs-web.md`。

## 三层模型

| 层 | 路径 | tools | 用途 | 禁止 |
|----|------|-------|------|------|
| L0 自带 GPT | `/v1/chat/completions` → `conversation.stream_text_deltas` → `get_text_access_token` | 否 | 真实业务聊、成熟期短聊、喂 excerpt 做文字总结 | 假聊灌水；塞进生图 `conversation_id` |
| L1 运维总结 | 外部模型或 L0；控制台先拉 JSON | 否 | 总结提示词、空池报告人话化；真图/图标走生图或 editable | 把推测写成封禁结论 |
| L2 Tool Agent | Admin 运维台 + tool facade | **是** | health→breakdown→probe→proxy 多步 RCA；mutate 建议 | 自治删号/清证据/批量注册/改协议 |

### P0 RCA 与 tools 的关系

| 能力 | 实现 | 是否必须 tool-calling |
|------|------|----------------------|
| `excluded_by_*` 空调度归因 | `AccountService` 谓词 + SCHED-001 API | 否（纯代码） |
| admission / inflight 对账 | `ai.py` + `ImageTaskService` | 否 |
| CF vs `token_invalid` 分类 | `conversation` / `openai_backend_api` classifiers | 否 |
| 多步自动探查与叙述 | L2 Agent 调上述工具 | **是**（编排层） |
| 无工具 LLM 总结 | 控制台注入 JSON | 否 |

可自动写操作极少：`pause_register`（止损）。进出台调度、刷额度、OTP 恢复一律 HITL。

## 养号文本红线（协议对齐后）

当前默认文本 body 强制 `history_and_training_disabled=True` 且不传 `conversation_id`（永久 Temporary Chat）。养号路径应与 API 反代分叉，见 `12`。

1. 独立文本会话；**勿**复用生图 `conversation_id`。
2. 养号/成熟期：`history_and_training_disabled=false` + 真 `parent_message_id` 续聊。
3. 仅真实 `Qtext` / 业务队列驱动；节奏跟 `humanlike_scheduler` 文本间隔（约 30s 基线 + 抖动）。
4. tz / `OAI-Language` / Accept-Language 跟 sticky egress（SG），勿写死上海+zh-CN。
5. 不伪造官网 tool-calling（web_search / 通用 function）。

## LLM 操作日志（已落地）

事件类型：`llm_ops`（`log_service`）。

| 字段 | 说明 |
|------|------|
| `source` | `L0` / `L1` / `L2` / `ai_review` |
| `account_hash` | token 短哈希；禁止 raw token |
| `kind` | `chat` / `summarize` / `ops_rca` / `review` |
| `latency_ms` | 墙钟 |
| `outcome` | `ok` / `reject` / `error` + 短码 |
| `prompt_shape` | 脱敏形状（长度、是否含图）；不落全文 |

接入点：`stream_text_deltas`（L0）、`content_filter.check_request`（ai_review）。管理页筛选 UI / L2 facade 仍后置。

## 风控拟人可视化与半小时巡检（已落地）

- **看板**：`GET /api/ops/humanlike-dashboard` + `/ops` Tab「风控拟人」——KPI、半小时平滑曲线、GitHub 式日历、admission/ETA、队列 EWMA、burst、poll 耗尽、streak、cohort、llm_ops、receive 漏斗、养号水位、巡检时间线。
- **日历 / 报告**：`GET /api/ops/risk-calendar?days=`、`GET /api/ops/risk-checks`、`POST /api/ops/risk-checks/run`。
- **落盘**：`data/risk_metrics.jsonl`、`data/risk_check_reports.jsonl`（`risk_metrics_store`）。
- **巡检**：`risk_audit`（默认 `enabled=false`，`interval_sec=1800`）。DeepSeek（NewAPI / `ai_review` 凭据）草稿 → L0 GPT 短终稿；只写报告与 `llm_ops`，不改 receive_state / 不删号。
- **账号页**：soft_band% / 熔断 / maturity / cohort 列；流量 Top；FP 完备摘要。
- **样式案例**：Cursor canvas `humanlike-risk-viz.canvas.tsx`（mock · Panda 量级）。
- **禁止**：宣称「降封 −X%」；调度配置编辑 UI；把 Camoufox `humanize` 当生产指标。

## 下一工程切片

1. PROTO-ALIGN 现网 canary（单号开 `chat_persist_history` + HAR 对照）
2. `request_shape` 漂移正式时序图（一期仅巡检 snapshot 记基数钩子）
3. 开启 `risk_audit.enabled` 后观察半小时报告质量

## 相关文档

- `12-protocol-gap-vs-web.md` — 文本/生图差距与 HAR 缺口
- `09-outlook-longevity-99-plan.md` §8 — 文本/生图策略域
- `10-human-like-workload-plan.md` — 禁止假聊天、SG 日历
- `04-improvement-backlog.md` — PROTO-ALIGN / LLM-OPS / TEXT-NURTURE / RISK-VIZ
