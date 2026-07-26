# 生图调度：前端 P-C、分阶段耗时与多阶段流水线

状态：**设计记录 v2.5**（2026-07-25）  
关联：`08-image-pipeline-newapi-async-plan.md`（IMG-012）、`20-pure-http-image-sentinel-todo.md`、**§12 吸收 Orchestrator v2**、**[`26-slot-lifecycle-rust-roadmap.md`](26-slot-lifecycle-rust-roadmap.md)**（释槽路径 / Rust 评估）

### 2026-07-25（槽位事故 + 取号优化）

| 项 | 说明 |
|----|------|
| 槽位 | **账号 inflight（Python）先于 sS 槽**；Rust 不持槽。见 `26` §2 |
| conc10 | `040240Z` 4/10（CF）；`034701Z` 0/10（inflight 泄漏）；account_queue **0.1%** |
| dispatchable=6 | conc10 后 10 号 `image_next_ok_ts` 冷却 → ready=6（非猜测） |
| 待做 | sS **75s** 阶段超时；`failure_retry` API；Rust **SlotLedger** Layer 1 |

### 2026-07-24 晚（PROD serial10 / conc10 阶段分解）

| 项 | 说明 |
|----|------|
| 验收 | 同步 API serial10 **10/10**、conc10 **10/10**；证据 `PROD-serial10-20260724T143921Z`、`PROD-conc10-20260724T150152Z` |
| 占墙钟 | serial：sS **0%** / SSE **79%**；conc10：task_queue **9.7%** / sS **8.6%**（2/10 任务）/ SSE **77.5%** |
| 生产并发 | `submit_workers=10`、`per_user_running=10`、`image_global_concurrency=10`、`sse_slots=10`、`image_account_concurrency=2` |
| FAQ | 阶段口径与「为何 10 槽仍排队」→ `captures/spa/PROD-latency-phase-breakdown-20260724.md` §8 |

### 2026-07-23 已落地（Panda `conc10-gantt-metrics-20260723`）

| 项 | 说明 |
|----|------|
| `preferred_account_email` | API body + Header；conc10 验收脚本轮询 13 个 `image_schedulable` 邮箱 |
| `image_account_concurrency` | 默认 **2**（单账号最多 2 路并发生图） |
| 甘特分段 | `queue_wait`（admit+ss_queue+account_queue）→ `sse_active` → `poll_resolve` → `download`；工具 `utils/image_gantt_segments.py` |
| 部署注意 | `web_dist` 变更后须 `docker compose up -d --force-recreate`（仅 restart 挂载不刷新） |
| 暖号 | `account_warmup`：`rotate_per_tick=0`、CF×2 block 24h；见 `22` §3 |
| 验收 | serial5 5/5；单账号 conc10 30/30；多账号 conc10 4/10（见 `acceptance-90s-multiacct-20260723`）；**own-proxy unique-egress conc10 10/10**（R3 换绑后，见 `Q` §12–13） |

---

## 1. 背景问题（用户侧）

1. UI 一提交即「排队中」，与 bench 直连验收体验不一致。
2. 完成态显示 50–60s，用户墙钟感知约 120s（排队 + 执行口径不一致）。**UI `durationMs` 优先 `phase_timings_ms.wall_clock_ms`，否则 `duration_ms`（worker 开跑起算），不含提交后 `queued` 等 worker 的空转**；生成中角标用前端 `startTime` 墙钟。见 §4.4。
3. 后端真实耗时由上行、上游 SSE、poll、下行构成，**SSE 占大头**；上下行受带宽限制。

---

## 2. 生图额度与调度（总览）

---

## 2.1 生图额度探测、核对与调度闸门（2026-07-23）

**原则**：没额度 / 未核对额度 **不参与调度**，更 **不上 sS**。

### 额度状态机（单账号 `image_quota_state`）

| 状态 | 含义 | 能否进 ready 池 | 能否上 sS |
|------|------|----------------|-----------|
| `unlimited` | Pro/ProLite | ✅ | ✅ |
| `unknown` | `image_quota_unknown=true` | ❌ | ❌ |
| `ready` | schedulable + quota>0 + 新鲜核对 | ✅ | ✅ |
| `unverified` | schedulable + quota>0 + 从未远程核对 | ✅（取号必拉 limits） | ✅（首次必 preflight） |
| `stale` | 有核对记录但超过新鲜度窗口 | ❌ | ❌ |
| `blocked` | 账面 quota>0 但未过 schedulable 门槛 | ❌ | ❌ |
| `refresh_pending` | quota=0 且 restore 窗口已到 | ✅（懒刷新） | ✅（取号时拉 limits） |
| `exhausted` | quota=0 且窗口未到 | ❌ | ❌ |

### 核心 API 字段

| 字段 | 层级 | 说明 |
|------|------|------|
| `quota` | 账号 | 本地缓存账面值（可能陈旧） |
| `available_image_quota` | 账号/池 | **当前可参与生图调度的额度**（UI 主展示） |
| `image_schedulable` | 池 | 通过 schedulable 闸门的账号数 |
| `dispatchable_candidate_count` | 池 | ready 且未占满 inflight、可立即派发 |
| `verified_total_quota` | 池 | 与 `available_image_quota` 同口径合计 |
| `total_quota` | 池 | 仅账面加总（**勿用于 UI 可用额度**） |

### 闸门链路

```text
_is_image_account_available
  → _has_confirmed_image_quota（拒绝 unknown）
  → _is_image_account_schedulable（失败证据 / receive / dup_binding / 新鲜度）
  → _list_ready_candidate_tokens（ready 池 = schedulable）
  → get_available_access_token（preflight skip 仅 recent + confirmed）
  → StageAccountProvider.acquire_for_ss
```

### 配置

- `image_pipeline.require_quota_freshness`（默认 `true`）：pipeline 开启时，已核对但过期额度不进池。
- `image_require_recent_quota_refresh`：全局强制新鲜度（与 pipeline 开关 OR）。

### UI 展示约定

- **账号页 / 生图页顶栏**：展示 `available_image_quota`（可用生图额度），悬停显示 `账面 · 生图候选 · 可派发`。
- **账号表格「额度」列**：展示 `available_image_quota`；badge 颜色反映 `image_quota_state`；tooltip 区分账面与可调度。

---

## 3. 当前架构续（三级生产者-消费者）


```text
用户点击
  → [前端对话队列 runConversationQueue]     # 每对话单消费者；多图 Promise.all 洪峰提交
  → POST /api/image-tasks
  → [ImageTaskService submit_workers]       # queued → running；SQLite 持久化
  → [account_service 账号槽 / binding]      # image_account_concurrency、binding_inflight
  → [conversation + openai_backend_api]     # SSE / poll / download
  → 轮询 GET /api/image-tasks（前端 2s）
```

**痛点**：前端是「过猛的生产者」；后端多层限流（`per_user_running`、`submit_start_min_interval_ms=1500`、`image_account_concurrency`）；任务面板 `concurrencyLimit=4` **仅展示、不限制提交**。

---

## 3. React 侧生产者-消费者（可行，定位明确）

### 3.1 结论

| 问题 | 答案 |
|------|------|
| 可不可行？ | **可行**，补前端提交层 P-C，不替代后端 |
| 做什么？ | 有界 submit pool、跨对话全局 slot、接 `/status` 的 `queue_position` |
| 不做什么？ | 账号选择、preflight、quota、binding、timeout_pending 续轮询 |

### 3.2 推荐形态

```text
Producer：handleSubmit / 重试 / 重新生成 → enqueue ImageJob
Buffer：  全局 FIFO（可按 turn 保序）
Consumer：N 个 submit slot（N 从 settings 读 per_user_running）
Poll：    独立协调器，优先 /api/image-tasks/status
```

### 3.3 落地顺序

| 阶段 | 内容 |
|------|------|
| P0 | `ImageJobQueue` + 有界 submit pool，替换 `Promise.allSettled` 洪峰 |
| P1 | 轻量 status 轮询 + 墙钟耗时（enqueuedAt → finishedAt） |
| P2 | 跨对话全局 slot；`concurrencyLimit` 接 settings |
| P3 | 按 dispatchable 动态调 N（需后端 stats API） |

---

## 4. 分阶段耗时：后端量、React 展示

### 4.1 原则

**上行 / SSE / poll / 下行发生在 Panda→OpenAI，只能在后端计量。**

| 阶段 | 计量位置 | 现有基础 |
|------|----------|----------|
| preflight / requirements | 后端 | bench `timings_ms` |
| 上行 upload | 后端 | `RequestPhaseTracker` → `download` 映射 |
| 上游 SSE | 后端 | `_last_image_sse_gen_ms`、`sse_image_gen_ms`（bench） |
| poll | 后端 | `poll_resolve_ms`（bench） |
| 下行 download | 后端 | `image_return_window_service` 已限回传 |
| 排队等待 | 前端 + 后端 | `created_ts` / `queue_position` |
| 瀑布图展示 | **React** | 消费 `task.phase_timings_ms`（待暴露） |

### 4.2 代码现状

- `services/request_phase.py`：`RequestPhaseTracker`，阶段含 `upstream_submit`、`sse_ready`、`poll`、`download` 等；**只打日志**，未写入 `ImageTask`。
- `ImageTask` API：仅 `progress`（字符串）+ `duration_ms`（整段 worker 时间）。
- bench：`scripts/_tmp_spa_image_bench3.py` 有完整 `timings_ms`，与生产 API **口径不一致**。

### 4.3 待办（后端小改 + React 展示）

1. `RequestPhaseTracker.durations_ms()` → 任务完成时写入 `phase_timings_ms`。
2. `progress_callback` 可带 `{step, phase_elapsed_ms}` 做实时累加。
3. React：生成中显示当前阶段 + 已用分段；完成后瀑布条；总耗时用墙钟 `created_ts → finished_ts`。

### 4.4 墙钟 vs 完成态「55s」（2026-07-24）

| 口径 | 起算点 | 典型用途 |
|------|--------|----------|
| **用户墙钟** | 前端点击提交（`startTime` / `created_ts`） | 生成中角标、体感等待 |
| **`duration_ms`** | 后端 worker `_run_task` 开跑 | 不含 `queued` 等 worker 前排队 |
| **`phase_timings_ms.wall_clock_ms`** | Pipeline `begin_run`（≈ worker 开跑） | 含 admit/ss/account 队列，仍不含 worker 前 `queued` |

因此 **2 并发生图**：第一张 ~48s 开跑即执行；第二张若等第一张释放账号槽 / binding inflight / 全局 inflight，墙钟可 **>100s**，完成态仍只显示 **~55s 执行段**。

**与 conc10 压测关系**：验收脚本在 **helper 容器独立进程**跑，不占 UI 侧 `ImageTaskService` 队列；但同期压测会抢号池出口与上游带宽，可能**间接**拉长第二张的排队或 SSE，不是「UI 显示 bug」的主因。主因是 **计量口径不含 worker 前排队**（`21` §1 第 2 点）。

---

## 5. 真正优化吞吐：多阶段流水线（非甘特图算法）

### 5.1 用户设想（摘要）

- SSE 占大头；文生图上行可「直接打」。
- 上下行按**带宽**排队；SSE 期间账号占槽但**不占满出口带宽**。
- A 号在 SSE 时，用 B/C 号继续生图。
- 已生成图进入**返回队列**，等带宽允许再下载/回传给调用方。

### 5.2 这是什么（计算机术语）

**不是「甘特图算法」。** 甘特图是**调度结果的可视化**，不是调度器本身。

更准确的名字：

| 术语 | 含义 |
|------|------|
| **多阶段流水线（Pipelining）** | 上传、SSE、poll、下载、回传各为一站，站点可并行 |
| **异构资源调度（Heterogeneous Resource Scheduling）** | 账号槽、binding、CPU、**带宽**是不同资源，不能共用一个并发数字 |
| **SEDA（分阶段事件驱动架构）** | 每阶段独立队列 + 有限 worker |
| **令牌桶 / 加权公平队列** | 带宽阶段的准入控制 |
| **关键路径（CPM）** | SSE 是墙钟关键路径；优化应优先「多账号并行 SSE」，而非盲目加 download worker |

核心洞察（与 IMG-012 §4 一致）：

```text
上游 SSE 等待期 = 带宽真空期 → 应继续占用「账号 SSE 槽」启动其他号；
下载/回传 = 带宽尖峰 → 应用独立小窗口，与 SSE 并发数解耦。
```

### 5.3 与 IMG-012 的对应

`08-image-pipeline-newapi-async-plan.md` 已定义目标队列：

```text
asset_ingress_queue       # 参考图上行（已有 image_asset_service 并发）
upstream_submit_queue     # 提交上游（ImageTaskService submit）
upstream_generating_set   # SSE 等待集合（占账号，基本不占带宽）
result_download_queue     # 结果下载窗口（建议 2~3）← 待独立服务化
client_response_queue     # b64/回传窗口 ← 部分由 image_return_window 实现
```

带宽保护（IMG-012 §5.3）：`bandwidth_soft/hard/emergency` + EWMA — **设计有，代码未落地**。

### 5.4 已落地 vs 缺口

| 能力 | 状态 | 位置 |
|------|------|------|
| 异步任务队列 | ✅ | `ImageTaskService` |
| 账号槽 / binding 限流 | ✅ | `account_service` |
| 参考图上传并发 | ✅ | `image_asset_service` |
| 回传窗口（返回队列） | ✅ 部分 | `image_return_window_service`（默认 size=3） |
| SSE 期间多账号并行 | ✅ 理论可行 | 受 schedulable 池 + `image_account_concurrency` + binding |
| 独立 result_download 队列 | ❌ | 与回传窗口未拆分 |
| 带宽 EWMA 调速 | ❌ | IMG-012 未实现 |
| `phase_timings_ms` 暴露 API | ❌ | 见 §4.3 |
| 前端提交 P-C | ❌ | 见 §3 |

### 5.5 推荐调度策略（可落地算法）

**资源维度拆开，每维独立准入：**

```text
R_account_sse   = min(schedulable_accounts × account_concurrency, burst_cap)
R_binding       = per binding inflight_max（通常 1）
R_upload        = upload_per_user_concurrency（图生图才占）
R_download      = result_download_window（建议 2~3）
R_client_return = image_return_window_size（现有 3）
R_bandwidth     = token bucket（Mbps EWMA，IMG-012）
```

**任务状态机（比单一 queued/running 更细）：**

```text
WAIT_UPLOAD → UPLOADING → WAIT_SSE_SLOT → SSE_STREAMING → POLLING
  → WAIT_DOWNLOAD_SLOT → DOWNLOADING → WAIT_RETURN_SLOT → READY → DELIVERED
```

- **文生图**：跳过 `WAIT_UPLOAD`，可直接 `WAIT_SSE_SLOT`。
- **SSE_STREAMING**：占用 `R_account_sse`；不占用 `R_download` / `R_bandwidth`（仅长连接空闲读）。
- **READY**：上游已拿到 URL/file_id，在内存/磁盘缓冲，等下载+回传窗口。
- **调度器**：各阶段独立 FIFO + 资源信号量；**不是**一个 `per_user_running` 管到底。

**ETA 估算**：应用分段 EWMA（SSE、download 分开），而非单一 `duration_ms` EWMA（现有 `note_success_duration_ms`）。

### 5.6 什么不会显著优化市场/耗时

| 做法 | 原因 |
|------|------|
| 仅加 `submit_workers` | SSE 关键路径在上游，worker 多只会堆 queued |
| 前端量 SSE 时长 | 物理上不可行 |
| 下载与 SSE 绑同一并发数 | 带宽尖峰叠加，IMG-011 已验证更差 |
| 无限并行同 binding | CF/429 风险上升 |

---

## 6. 与 React P-C 的分工

```text
后端：多阶段流水线 + 资源信号量 + phase_timings + 带宽桶（权威调度）
前端：提交 P-C（别把 20 个 POST 一次打满）+ 瀑布图/墙钟展示（消费 API）
```

甘特图可作为 **ops 调试 UI**（每任务各阶段条），由 `phase_timings_ms` + 队列深度绘制，**不是**调度算法本身。

---

## 7. 下一步建议

1. **文档**：本文件；与 `08` 合并实施 checklist 时标缺口。
2. **后端 P0**：`phase_timings_ms` 写入 `ImageTask`；独立 `result_download` 信号量（或扩展现有 return window 为两阶段）。
3. **后端 P1**：带宽 EWMA 准入（IMG-012 §5.3）。
4. **前端 P0**：`ImageJobQueue` 有界提交（§3）。
5. **验收**：24 路混合输入下，对比「单池 running」vs「分阶段流水线」的 p50/p95/p99 墙钟与带宽曲线。

---

## 8. 双槽流水线 v2：pS（Prompt Slot）+ sS（SSE Slot）

状态：**设计 v2.3**（2026-07-23）  
待办：**IMG-SCHED-022**（见 `04-improvement-backlog.md`）  
v2 对照：§12（Sync Orchestrator vs Async Job 双轨）

### 8.1 槽位定义

| 槽 | 全称 | 默认并发 | 占用资源 | 说明 |
|----|------|----------|----------|------|
| **pS** | Prompt Slot | **10** | 文本对话账号槽（不占生图 quota） | 短 prompt 增强；可走 `/conversation` 单轮文本 |
| **sS** | SSE Slot | **10** | 生图账号槽 + binding | 上游 `image_gen` SSE + poll；**拿到 sediment 即释放** |
| **UQ** | Upload Queue | 3（沿用） | 带宽 + upload 并发 | 仅编辑/多参考图 |
| **DQ** | Download Queue | 3（沿用） | 带宽 + return window | 结果下载 + b64 回传 |

**原则**：pS 与 sS **独立账号、独立 FIFO、独立信号量**；某任务在 sS 时，另一任务可同时占 pS（不同号）。

### 8.2 调度顺序（用户指定）

```text
queue → upload → queue → pS → queue → sS → queue → download → deliver
  Q0      UQ      Q1    pS     Q2    sS     Q3      DQ
```

- **文生图**：`Q0 → Q1 → pS? → Q2 → sS → Q3 → DQ`（跳过 UQ）
- **编辑/附图**：必经 UQ

任务状态机（细化）：

```text
QUEUED → UPLOADING → WAIT_PS → PS_RUNNING → WAIT_SS → SS_RUNNING
  → POLLING → READY_BUFFER → WAIT_DOWNLOAD → DOWNLOADING → DELIVERED
```

`sS` 在 `POLLING` 结束且已有 `sediment_ids` 时**立即释放账号槽**，后续只占用 DQ。

### 8.3 Prompt 增强（pS）规则 — **v2.1 修订**

| 条件 | 是否进 pS |
|------|------------|
| 未传 `prompt_enhance` / 默认 | **不进**（默认关闭，与现网「增强」作为可选项） |
| `prompt_enhance=true`（显式开启） | **进** |
| `prompt_enhance=false` | **不进** |
| 长 prompt 自动 skip | `len(prompt) ≥ 长阈值`（建议 **256 字符** 或 **80 token**）时**即使 true 也 skip**，省 4–10s |

**API**（默认不传 = 不增强）：

```json
{ "prompt": "一只猫", "prompt_enhance": true }
```

**pS / sS 账号隔离（首选异号，禁止任务级永久 exclude）**：

- 与 Orchestrator v2 对齐：**绝不用「exclude 至 DELIVERED」**（死锁根源，§11.3）。
- sS 选号时 **首选**排除本请求 pS 用过的 `account_id`；`candidates(exclude) ≥ 3` 时严格执行。
- `candidates < 3`：允许复用 pS 号 + `image_next_ok_ts += 3s` + 慢速通道。
- `dispatchable_count < 2` → `429 image_pool_starved`，不入队空转。

### 8.3.1 账号对话指数（ACI，供 sS 调度）

**定义**：每个可调度账号维护一个 **Account Conversation Index（ACI）**，仅用于 **sS 槽选号排序**，不用于 pS。

| 维度 | 字段/来源 | 权重方向 | 说明 |
|------|-----------|----------|------|
| 对话新鲜度 | `last_chat_at` / 养号文本时间 | ↑ 略优先 | 有近期正常文本的号，上游 SSE 更稳 |
| 热池 | `account_warmup_service._hot` | ↑ 优先 | 已 bootstrap，requirements 更快 |
| 生图成功率 | `success` / `fail` / `image_fail_streak` | ↑ 高成功 | 近期失败降权 |
| 额度 | `quota` / `limits_progress` | ↑ 多额度 | 避免快速打光 |
| 间隔就绪 | `image_next_ok_ts` | 未到期 ↓ | 拟人间隔 |
| CF/代理健康 | `cf_*` / `proxy_quarantine` | 异常 ↓↓ | 硬排除 |
| binding 负载 | 同 binding inflight | 已满 ↓↓ | 硬排除 |
| 对话深度 | `chat_turn_count_7d`（新字段） | 适中 ↑ | 极冷号略降权 |

**ACI 计算**（建议 0–100，每小时衰减 + 事件增量）：

```text
ACI = clamp(
  base_schedulable_score          # 现有 _image_candidate_sort_key 归一化
  + w1 * warmup_hot_bonus
  + w2 * recent_chat_decay(hours_since_last_chat)
  + w3 * image_success_rate_24h
  - w4 * fail_streak_penalty
  - w5 * binding_contention
)
```

**sS 选号**：在 `WAIT_SS` 队列出队时，对 `dispatchable_candidate_set` 按 **ACI desc** 取 top-1；同分则现有 `used_ts` LRU。

**ACI 更新时机**：

1. 每次文本会话结束（含 pS、养号、用户聊天）→ `chat_turn_count` + `last_chat_at`
2. 每次生图 success/fail → 调整 `image_success_rate_24h`
3. CF demote / quarantine → 置 0 或剔除
4. 可选：后台每 5min 全量衰减

**与现有代码关系**：扩展 `account_service._image_candidate_sort_key` 或并行 `get_ss_candidate_rank()`；复用 `account_warmup_service` 热池信号；**不替代** `schedulable_breakdown` 硬门槛。

### 8.3.2 pS 实现：对话增强

- 用 **文本 conversation**（`/backend-api/conversation` 或现有 `openai_v1_chat_complete`），**禁止**复用生图 `conversation_id`。
- System：按 `prompt_enhance_locale` 切换；**默认英文**模板；`same_as_user` 时跟输入语种。
- 单轮 user：原始 prompt → assistant：增强后 prompt → 写入 `task.enhanced_prompt` → 进入 Q2。
- **workload**：`decide_for_account(..., channel="text")` 或专用 `ps_account_pool`，**不扣 `image_gen` quota**；pS 完成后 `release` 文本槽。
- 预估耗时：p50 **4s**，p95 **10s**，p99 **18s**（短 prompt TTFT+生成）。

### 8.4 在 v1 基础上的再优化（v2.1）

1. **pS / sS 强制异号**（见 §8.3）；pS 走 text lease，sS 走 image lease + ACI 排序。
2. **ACI 供给 sS**：热号优先、冷号降权；指标进 `GET /api/accounts/schedulable-breakdown` 扩展桶。
3. **prompt_enhance 默认关**；仅 `true` 时进 pS；长 prompt 自动 skip。
4. **sS 内不再持槽 download**：poll 结束即 `release_image_slot`。
5. **enhanced_prompt 落盘**：sS 重试不重复 pS。
6. **DQ 令牌桶**：IMG-012 带宽 EWMA。
7. **分段 EWMA ETA**：`wait_ps` / `wait_ss` / `ss_exec` / `wait_dq`。
8. **动态槽上限** | ~~首版不做~~ → **P2 自适应**（号池 >30 再启用） |
9. **pS 失败降级**：对话失败 → 原 prompt 直接进 Q2，不阻塞。
10. **ACI 反哺养号**：长期无对话且 ACI 低 → 文本养号队列优先补分（与 TEXT-NURTURE 联动，可选）。

### 8.5 当前基线耗时（墙钟，用于对比）

数据来源：生产压测 `docs/logs/2026/2026-07.md`、串行 5 证据 `O-*`、用户 UI 4 并发观测。

| 场景 | 指标 | p50 | p95 | p99 | 备注 |
|------|------|-----|-----|-----|------|
| **A. 单张、低负载** | 墙钟总耗时 | **~60s** | **~75s** | **~90s** | `duration_ms`≈56s + 少量排队；证据 serial5 `total_ms` p50=46–57s 执行段 |
| **B. UI 4 张批量** | 单张墙钟 | **~90s** | **~130s** | **~180s** | 用户感知整批 ~120s；显示 50–60s 仅为执行段 |
| **B. UI 4 张批量** | 整批完成（最后一张） | **~120s** | **~150s** | **~200s** | `per_user_running≈2`、`image_account_concurrency≈1` |
| **C. 12 路 async 压测** | 单任务墙钟 | **~203s** | **~295s** | **~304s** | `per_user_running=2`；生产实测 |
| **D. 执行段 only**（bench） | `sse_image_gen_ms` | **~29s** | **~65s** | **~65s** | 不含排队/下载；serial5 五轮 |

说明：当下 UI **完成态数字 ≈ 场景 D + 下载**，用户墙钟 ≈ **场景 B/C**。

### 8.6 双槽实现后预估（墙钟总耗时）

**假设**：Panda 可调度账号 **≥12**；pS=10、sS=10、DQ=3；**`prompt_enhance` 默认关**（多数请求不经 pS）；带宽未打满。

**单任务服务时间分量（中位，默认不增强）**：

| 阶段 | p50 | p95 |
|------|-----|-----|
| wait Q0+Q1 | 1s | 5s |
| pS 执行（仅 enhance=true） | 0s / **4s** | 0s / **10s** |
| wait Q2（等 sS 槽） | 2s | 12s |
| sS+polling | 42s | 58s |
| wait Q3 + download | 3s | 8s |
| **合计（默认不增强）** | **~48s** | **~70s** |
| **合计（enhance=true）** | **~52s** | **~75s** |

#### 预估对比表

| 场景 | 指标 | 当前 p50 | 当前 p95 | 当前 p99 | **v2 预估 p50** | **v2 预估 p95** | **v2 预估 p99** | 降幅（p95） |
|------|------|----------|----------|----------|-----------------|-----------------|-----------------|-------------|
| A. 单张低负载 | 墙钟 | 60s | 75s | 90s | **48s** | **65s** | **80s** | ~13% |
| B. 4 张批量 | 单张墙钟 | 90s | 130s | 180s | **58s** | **85s** | **105s** | **~35%** |
| B. 4 张批量 | 整批完成 | 120s | 150s | 200s | **~65s** | **~90s** | **~110s** | **~40%** |
| C. 12 路并发 | 单张墙钟 | 203s | 295s | 304s | **~75s** | **~115s** | **~140s** | **~61%** |
| D. 24 路并发 | 单张墙钟 | ~280s¹ | ~360s¹ | ~400s¹ | **~95s** | **~155s** | **~190s** | **~57%** |

¹ 24 路按 12 路 p50×1.4 外推（`per_user_running=2` 下近似线性恶化），实现后应以 R5.6 实测替换。

**关键收益来源**：

- **12/24 路**：sS 10 并行 vs 现 `per_user_running=2` → 排队项从主因降为次要；p95 改善最大。
- **4 批量**：4 路可同时占 sS，整批 ~1 个 SSE 周期 + pS，而非 2 波 × 60s。
- **单张低负载**：收益有限（~10%），主要省排队口径统一 + 可选跳过 pS。

**pS 成本**：仅 `prompt_enhance=true` 时 +4s p50；默认关闭时对 p95 **无影响**。

| 长 prompt 阈值 | **256 字符 或 80 token**（满足任一即 skip pS） |
| pS 输出语言 | **默认英文**；UI/API 可选 `prompt_enhance_locale=same_as_user` |
| pS 写 chat 历史 | **是**（`chat_persist_history` 的号记入历史 → 抬 ACI） |
| 槽位配置 | **固定 pS=10、sS=10**（号池后续扩大，首版不动态收口） |
| NewAPI sync 优先 | **采用 WFQ 轻优先**（见 §8.11） |
| ACI 初版 | **v1 = 现有 sort_key + 热池 + last_chat_at**（见 §8.12） |
| 额外建议 | **全部纳入实现**（见 §8.13） |

### 8.10 NewAPI sync 优先（裁决）

**建议：sS 队列 Q2 使用 WFQ，sync 权重 ×2，async 权重 ×1。**

| 项 | 说明 |
|----|------|
| 识别 | `task.source in {v1_images_sync, newapi_sync}` 或 `sync_waiter=true` |
| 队列 | 与 async 共用 sS 10 槽，**不单独划槽**（避免 sync 占槽/async 饿死） |
| 调度 | `virtual_finish_time = now + size / weight`；sync weight=2 → 等效优先约 2 倍 |
| 上限 | 连续 5 个 sync 后强制插入 1 个 async（防 async 饥饿） |
| 超时 | sync 仍受 `image_return_window` + hard_timeout；不无限插队 |

**不采用**「sync 独立 3 槽」：号池小时会浪费 sS 容量。

### 8.11 ACI 初版权重（v1）

**策略：先等价 `_image_candidate_sort_key`，再叠加 3 个可观测分量；w4/w5 硬排除不进 ACI 公式。**

```text
ACI_v1 = clamp(0, 100,
  50                                                    # 基底
  + 20 * warmup_hot(email)                              # 0/1
  + 15 * exp(-hours_since(last_chat_at) / 48)           # 对话新鲜度
  + 15 * (success_24h / max(1, success_24h + fail_24h))
  - 10 * min(3, image_fail_streak)
)
```

| 规则 | 说明 |
|------|------|
| 硬门槛 | CF、quarantine、quota=0、preflight backoff → **不进候选集**（不算 ACI）。**2026-07-26**：已绑定账号的 `proxy_cf_ok` 缓存优先于 `cf403_scan` 批量隔离（`proxy_cf_eligibility`）；见 `17` §「批量 scan 隔离」 |
| 排序 | sS 出队：`ACI desc` → 现有 `weight_rank, used_ts` 作 tie-break |
| 持久化 | `accounts.aci_score` 每次事件更新；`last_chat_at`、`chat_turn_count_7d` |
| v2 迭代 | 稳定后再加 binding_contention、7d 深度、养号联动 |

### 8.12 已确认的工程项（原「额外建议」）

1. **同 turn 账号绑定表**：`turn_id → [email_a, email_b?, slot_index % 2]`；4 张 **2×2 轮换**，单 turn 最多 2 号。
2. **pS 仅占 `text_inflight`**：不计入 `image_inflight` / sS 容量判断。
3. **ACI 只影响排序**：`schedulable_breakdown` 硬排除不变。
4. **UI 墙钟 + 分段瀑布**：`wall_clock_ms` + `phase_timings_ms`（排队/pS/sS/下载分列）。
5. **pS 异号 exclude 列表**：任务级 `excluded_tokens_for_ss` 至 DELIVERED。
6. **enhanced_prompt + ps_conversation_id 落盘**：sS 重试不重复 pS。

### 8.13 pS 语言与历史（细则）

```json
{
  "prompt": "一只猫",
  "prompt_enhance": true,
  "prompt_enhance_locale": "en"
}
```

| `prompt_enhance_locale` | 行为 |
|-------------------------|------|
| 未传 / `en` | 英文扩写（**默认**） |
| `same_as_user` | 跟用户输入同语种扩写 |
| `zh` / `ja` / … | 显式语种（可选白名单） |

- pS 走 **persist history**（账号 `chat_persist_history=true` 时写入）；更新 `last_chat_at`、`chat_turn_count_7d`、ACI。
- System 模板按 locale 切换；输出仍单行 prompt，无解释。

---

## 9. 可选开关目录（UI + API）

双边均可独立配置；**未传 = 服务端默认**。

### 9.1 任务级（生图请求）

| 开关 | API 字段 | UI 控件 | 默认 | 说明 |
|------|----------|---------|------|------|
| Prompt 增强 | `prompt_enhance` | Toggle「增强描述」 | **off** | 进 pS |
| 增强语种 | `prompt_enhance_locale` | 下拉：英文 / 跟用户 | **en** | 仅 enhance=on |
| 跳过增强 | （长 prompt 自动） | 只读提示 | — | ≥256 字 / 80 token |
| 异步模式 | `async=true` / task API | 默认 async | on | sync 走 `/v1/images` |
| 协议路径 | `image_spa_tool_path` | 高级：SPA / picture_v2 | spa | 内部/调试 |
| 轮询超时 | `poll_timeout_secs` | 高级数字 | 120 | 超长 prompt 可加长 |
| 硬超时 | `task_hard_timeout_secs` | 高级 | auto | 0=自动 |
| 取消宽限 | `cancel_grace_secs` | — | 1 | API only |
| 客户端任务 ID | `client_task_id` | 自动 | uuid | 幂等 |

### 9.2 调度与账号（API 优先，UI 设置页可选）

| 开关 | API 字段 | UI | 默认 | 说明 |
|------|----------|-----|------|------|
| 同 turn 双号轮换 | `turn_account_mode=split2` | 高级 | split2 | `single` / `split2` / `distinct4` |
| sticky 生图号 | `sticky_image_account` | off | off | 整单绑一号（调试） |
| 跳过上传统计 | `skip_upload` | — | auto | 文生图自动 |
| 优先 sync | `priority=high` | — | normal | NewAPI 可设 high |
| 指定邮箱 | `account_email` | 账号选择器 | 自动 | 绕过 ACI |
| 跳过 ACI | `account_pick=random` | — | aci | 随机/轮询 |

### 9.3 可观测（UI 展示，API 只读）

| 字段 | 说明 |
|------|------|
| `queue_position` / `estimated_start_after_secs` | 当前段队列 |
| `phase` | `WAIT_SS` / `SS_RUNNING` / … |
| `phase_timings_ms` | 分段耗时 |
| `wall_clock_ms` | 提交→交付 |
| `aci_picked_score` | 本次 sS 选号 ACI |
| `accounts_used[]` | 本任务用过的邮箱（脱敏） |

### 9.4 全局（settings / admin）

| 开关 | 配置键 | 默认 | 说明 |
|------|--------|------|------|
| pS 槽数 | `image_ps_slots` | 10 | |
| sS 槽数 | `image_ss_slots` | 10 | |
| DQ 窗口 | `image_return_window_size` | 3 | |
| sync WFQ 权重 | `image_sync_queue_weight` | 2 | |
| 长 prompt 阈值 | `prompt_enhance_max_chars` | 256 | |
| | `prompt_enhance_max_tokens` | 80 | |
| 拟人间隔 | `scheduler.enabled` | 现有 | 不动 |

---

## 10. 框架层继续优化（待办分级）

### P0（与双槽同批）

| 项 | 说明 |
|----|------|
| `ImagePipelineOrchestrator` | 替代单 `per_user_running`；各段独立队列 + worker |
| `phase_timings_ms` + `wall_clock_ms` | 全 API 暴露 |
| 前端 `ImageJobQueue` | 有界提交，读 `image_ss_slots` |
| WFQ sync 优先 | §8.10 |

### P1（双槽后）

| 项 | 说明 |
|----|------|
| 带宽 EWMA 桶 | IMG-012 §5.3，仅 DQ |
| `READY_BUFFER` 磁盘溢出 | 大图并发时防 OOM |
| ACI v2 + 养号联动 | TEXT-NURTURE 补低 ACI 号 |
| Ops 甘特图 | `/ops` 读 `phase_timings` |

### P2（可选增强）

| 项 | 说明 |
|----|------|
| **自适应 sS 槽** | 号池 >30 时启用 `min(config, dispatchable-2)` |
| **按 prompt 复杂度估 ETA** | 长短 prompt 不同 EWMA 桶 |
| **binding 亲和** | 同用户多 turn 优先同 binding 已热号 |
| **失败快速换号** | sS CF403 立即换 ACI 次优，不等 poll 超时 |
| **ε-greedy ACI 探索** | §11.4，防冷号萎缩 |
| **retry_phase_cursor 恢复** | §11.2，P0 |
| **READY_BUFFER 水位反压** | §11.5，P1 |
| **duplicate prompt 并行** | 已有 dedup；可加「同 prompt 共享 sediment」缓存（高风险，默认 off） |
| **SSE 早释放优化** | sediment 到手即释 sS 槽（已设计，强测） |
| **分租户公平** | 多 API key 时 WFQ per `owner_id` |

### 10.1 架构命名（实现时）

```text
services/image_pipeline/
  orchestrator.py      # 状态机 + 段间 handoff
  ps_pool.py             # PromptSlotPool(10)
  ss_pool.py             # SseSlotPool(10) + WFQ
  aci_ranker.py          # ACI v1
  turn_binding.py        # 2×2 账号轮换
  phase_metrics.py       # timings 写入 task
```

---

### 8.7 风险与约束

| 风险 | 缓解 |
|------|------|
| 账号池 < 20，10+10 逻辑槽抢号 | pS 用 text 通道；sS 才占 image quota；异号轮换 |
| pS 对话触发 CF/429 | pS 独立 backoff；失败 **降级为原始 prompt** 进 sS |
| 10 路 SSE 放大上游 429 | 保留 `submit_start_min_interval` 抖动；binding inflight=1 |
| 带宽尖峰 | DQ=3 + EWMA 桶；READY_BUFFER 可先存盘 |

### 8.8 验收口径

1. 同一 12 路 / 24 路压测脚本，对比实现前后 **墙钟** `created_ts→delivered_ts`（非 `duration_ms`）。
2. 报告 p50/p95/p99 + 分段 `phase_timings_ms`（wait_ps/wait_ss/ss_exec/wait_dq）。
3. 4 张 UI 批量：最后一张墙钟 p95 **< 95s**（当前 ~150s）。
4. `prompt_enhance=true` + 长 prompt skip；`prompt_enhance_locale`；pS 写历史抬 ACI；同 turn 2×2 轮换单测。

---

## 11. 故障模式、恢复与单点风险（v2.3）

### 11.1 评审结论：四项「潜在致命」是否必须解决？

| 风险 | 是否必须 | 优先级 | 说明 |
|------|----------|--------|------|
| 状态机复杂 + 重启恢复黑洞 | **是** | **P0** | 无 `retry_phase_cursor` 会卡死、重复生图、重复扣 quota |
| 强制异号 → 小池饥饿/伪死锁 | **是**（号池扩大前） | **P0** | 改为「首选异号 + 小池降级」 |
| ACI 马太效应 / 冷号萎缩 | **是**（长期） | **P1** | ε-greedy 探索 + 冷号探测任务 |
| READY_BUFFER OOM | **是**（高并发） | **P1** | 水位线反压 sS 出队 |

四项**均需纳入实现**；前两项为 **P0 阻塞上线**，后两项可与双槽同批或紧随其后。

### 11.2 状态机：压缩对外、细化对内

**对外 API** 仍用 4 态：`queued | running | success | error`（兼容现有客户端）。

**对内 `pipeline_phase` + `retry_phase_cursor`（持久化到 task）**：

```text
对外 running 映射：
  UPLOADING | WAIT_PS | PS_RUNNING | WAIT_SS | SS_RUNNING | POLLING  → running(progress=…)
  READY_BUFFER | WAIT_DOWNLOAD | DOWNLOADING                         → running(progress=receiving_image)
```

**`retry_phase_cursor`（幂等恢复锚点）**：

| cursor | 含义 | 重启/失败后跳转 |
|--------|------|-----------------|
| `NONE` | 未开始执行段 | `WAIT_*` 队首 |
| `PS_DONE` | enhanced_prompt 已落盘 | `WAIT_SS`（不重复 pS） |
| `SS_DONE` | `sediment_ids` + `conversation_id` 已落盘 | `WAIT_DOWNLOAD`（**不重复 SSE**） |
| `DL_DONE` | 文件已落本地/索引 | `DELIVERED` 或补传 client |

**失败回滚规则（必须写死）**：

| 失败点 | 动作 | 是否重复扣 quota |
|--------|------|------------------|
| DOWNLOADING 4xx/5xx/超时 | `HEAD/GET` 重试同一 `sediment_id`；仍失败则 **probe conversation** | **否**（未 mark_image_result） |
| sediment probe 404/空 | 回退 `SS_RUNNING`，**仅 1 次**；再失败 → error | 仅重跑成功时扣 1 次 |
| SS_RUNNING 中断（无 sediment） | 续 poll（沿用现有 `timeout_pending`）或重开 SSE（≤1） | 同上 |
| PS_RUNNING 失败 | 降级原 prompt → `PS_DONE` + skip | 否 |
| 进程重启 | 读 cursor + 落盘字段恢复，**禁止**从 `QUEUED` 重头 | 见上 |

**计费护栏**：`mark_image_result(success=True)` **仅在** `DELIVERED`（或 client 已 ACK 的 `DL_DONE`）时调用一次；中间态永不扣 quota。

**与现网**：`ImageTaskService.timeout_pending` + `resume_polling` 已覆盖「有 conversation 无图」；双槽需把其纳入 `SS_DONE` 子状态，而非平行第三套逻辑。

### 11.3 异号策略：首选异号 + 小池降级（替代绝对强制）

```text
if dispatchable_count >= 3:
    sS 候选 = schedulable \ task.excluded_ps_tokens
else:
    允许复用 pS 号，但：
      - image_next_ok_ts += 3s（专属慢速通道）
      - 记 metrics: same_account_ps_ss_fallback
      - 该任务 sS 权重降级（WFQ 虚拟时间 +5s）
```

**伪死锁缓解**：pS 与 sS **永不互斥同一信号量**；pS 占 `text_inflight`，sS 占 `image_inflight`。号2 在 exclude 列表只影响**同一任务**的 sS 选号，不阻塞其他任务的 pS。

**硬底线**：`dispatchable_count < 2` 时 **拒绝新入队**（HTTP 429 `image_pool_starved`），而非无限排队——避免全队列假死。

### 11.4 ACI：探索因子（ε-greedy）

```text
with prob ε=0.05: 从「可调度但 ACI 底部 30%」随机选号
on 探索成功: aci_score += 15（一次性 bootstrap bonus）
on 探索 CF/429: 仅该号降权，不拖累全局
```

另：**冷号探测**（P2）——TEXT-NURTURE 对 `aci_score<40` 且 7d 无对话的号每日轻量文本，防止「永远得不到验证」。

### 11.5 READY_BUFFER 水位线反压

| 水位 | 动作 |
|------|------|
| 内存+磁盘缓冲 **>512MB** 或条数 **>32** | **暂停 sS 新出队**（pS/UQ 可继续） |
| **>768MB** |  additionally 暂停 pS 出队 |
| DQ 冲刷至 **<256MB** | 恢复 sS；滞后 5s 防抖动 |

大图落盘路径：`READY_BUFFER/{task_id}.meta.json` + 外链，避免 b64 堆内存。

### 11.6 单点故障（SPOF）矩阵

| 单点 | 若故障 | 影响 | 是否避免 | 避免/缓解 |
|------|--------|------|----------|-----------|
| **进程重启** | 内存态队列丢失 | 任务挂起或重复执行 | 必须 | SQLite task + cursor；worker 启动 `reconcile_unfinished()` |
| **SQLite image_tasks.db** | 损坏/锁死 | 全队列停 | 必须 | WAL + busy_timeout（已有）；定时备份；reconcile 只读副本 |
| **accounts.db** | 损坏 | 无法取号 | 必须 | 启动前只读探活；降级只服务已 running 任务完成，拒新单 |
| **单账号 token 失效** | 该号全失败 | 局部成功率降 | 必须 | 硬门槛剔除；ACI 降权；不阻塞队列（换号） |
| **单 binding / 代理挂** | 同出口任务失败 | 局部 CF/429 | 必须 | `proxy_quarantine`；binding inflight=1 |
| **上游 OpenAI 区域性故障** | 全 sS 慢/失败 | 全局 p95 升 | 无法消除 | 熔断 + ETA 诚实展示；暂停 burst |
| **DQ 带宽打满** | 下载排队变长 | READY_BUFFER 涨 → 反压 sS | 必须 | EWMA 桶 + §11.5 水位线 |
| **10+10 槽配置 vs 实际号数** | 空转等号 | 假「排队」 | 缓解 | `dispatchable<2` 拒新单；号池扩大后缓解 |
| **WFQ sync 优先** | async 饥饿 | 异步 p95 恶化 | 缓解 | 每 5 sync 插 1 async（§8.10） |
| **pS 写历史** | 单号文本频控 | 该号暂不可用 | 缓解 | pS 独立 backoff；失败降级 skip pS |

**不存在单一物理 SPOF 应拖垮全池**；最大真实风险是 **恢复语义不清（cursor 缺失）** 与 **小池 + 绝对异号**——已上升为 P0。

### 11.7 改进项与文档映射

| 建议 | 状态 |
|------|------|
| ε-greedy ACI | **采纳** → §11.4，P1 |
| 宽松异号（小池降级） | **采纳** → §11.3，**P0** |
| `retry_phase_cursor` 幂等恢复 | **采纳** → §11.2，**P0** |
| READY_BUFFER 水位反压 | **采纳** → §11.5，P1 |

### 11.8 验收（故障专项）

1. **重启演练**：任务停在 `SS_DONE` / `DOWNLOADING`，kill -9 后恢复 → 不重复 SSE、不双扣 quota。
2. **小池演练**：`dispatchable=3` + enhance 全开 → 无无限排队；或 429 `image_pool_starved`。
3. **缓冲演练**：模拟 40 张同时 READY → sS 出队暂停、内存 < 阈值。
4. **探索演练**：运行 24h 后冷号 `last_ss_at` 分布不出现「从未被选中」的长尾 >50%。

---

## 12. 吸收 Image Orchestrator v2（边界对齐与 Python 优化）

> 来源：Rust `gptimage-gateway-rs` Image Orchestrator v2 设计评审（2026-07-23）。  
> 目的：把 v2 已验证的**调度结构优点**迁入本仓，同时保留 Panda **sediment + 异步 Job** 能力。

### 12.1 双轨定位（避免状态机爆炸）

| 维度 | **Sync Orchestrator**（v2 同款） | **Async Image Job**（本仓 v3 / IMG-SCHED-022） |
|------|--------------------------------|-----------------------------------------------|
| 入口 | `POST /v1/images/*` sync、`image_sync_adapter` | `POST /api/image-tasks/*` |
| 生命周期 | **request-scoped**；响应结束即销毁 | SQLite 持久化；跨重启恢复 |
| 产物 | 可直出 URL；或 sediment→download 同事务 | `sediment_ids` + poll + download |
| 恢复 | **进程内**分阶段重试 + 客户端重试 | **`retry_phase_cursor` 入库**（§11.2） |
| 状态机 | 内存 checkpoint（`PS_DONE`/`SS_DONE`） | `pipeline_phase` + cursor |
| Trace | `phase_timings_ms` / segment 写 task 或日志 | 同左，且可 poll |

**明确不做**：在 Sync 路径上实现跨重启 `WAIT_DOWNLOAD` 续跑（那是 Async Job 职责）。

**明确保留**：Panda 生产路径的 **sediment + conversation poll**（v2 Lite 无 sediment，本仓不能丢）。

### 12.2 v2 揭示的现网反模式（Python 今日仍在犯）

与 v2 `Admit() 占 10 lane 全程` 等价的问题：

```text
ImageTaskService._run_task：
  submit_worker 认领 → per_user_running + image_inflight 占槽
  → 整段 requirements + SSE + poll + download 才 release
```

嵌套瓶颈（v2 的 expand=2 inside 10 lanes）在本仓表现为：

- `per_user_running=2~6` 全程占槽，而 SSE 实际可 10 并行
- `submit_start_min_interval_ms` 人为串行
- 单一 `duration_ms` / 前端排队把 **各阶段等待加总成「假 queue」**

**优化方向（与 v2 一致）**：拆掉「全程 lane」，改为 **分阶段池 + 分阶段释槽**。

### 12.3 目标架构（Python 版，吸收 v2）

```text
全局软上限 global_queue_max / QueueCapacity
  → 超限 ErrQueueFull（无深层 FIFO，避免假排队）
  → 路由：
      Lite 文生图：跳过 UQ
      edits：UQ(upload 池，= assetConcurrency 默认 8)
      → pS 等待 FIFO → pS 槽 1~10（AcquireForPS，独立选号）
      → sS 等待 FIFO → sS 槽 1~10（AcquireForSS，独立选号，ACI 排序）
      → DQ 等待 FIFO → download 池 max=8（≠10，download 非瓶颈）
      → deliver
```

**废弃/降级**：

| 旧 | 新 |
|----|-----|
| `per_user_running` 全程占槽 | 仅作 **owner 公平 WFQ** 权重，不占物理槽 |
| `PipelineSlots` / 单 lane Admit | `prompt_slots` + `sse_slots` 独立 worker |
| 嵌套 `expandConcurrency=2` | 并入 pS 池 10 |
| SSE AIMD 动态 target | 首期固定 `sse_slots=10`；压测后再议 |
| 单一 `queueMs` | `admit_queue_ms` / `upload_queue_ms` / `ps_queue_ms` / `ss_queue_ms` / `download_queue_ms` |

### 12.4 AccountProvider（分阶段选号，v2 必做）

```python
class AccountProvider(Protocol):
    def acquire_for_upload(self, ctx) -> AccountLease: ...   # edits only
    def acquire_for_ps(self, ctx, *, prefer_exclude: set[str]) -> AccountLease: ...
    def acquire_for_ss(self, ctx, *, prefer_exclude: set[str]) -> AccountLease: ...
```

- Gateway / `conversation.py` **禁止**在循环开头一次 `get_available_access_token` 贯穿全程。
- **首选异号**：`prefer_exclude={ps_account_id}`；候选 ≥3 则排除；<3 则回退（§11.3）。
- pS 用 `text_inflight`；sS 用 `image_inflight`；**poll 结束即 release sS 槽**。

### 12.5 Prompt 路由（合并 v2 启发式 + 本仓 API）

本仓 **`prompt_enhance` 默认 false**（产品已确认）；v2 的 `expand_prompt` 默认 true **不照搬**。

| `prompt_enhance` | prompt 形态 | 行为 |
|------------------|-------------|------|
| false / 未传 | 任意 | **跳过 pS** |
| true | 短（**<48 字符或 <8 词**，复用 v2 `shouldExpand`） | **进 pS** |
| true | 长（≥256 字符或 ≥80 token） | **跳过 pS**（SkipExpand 语义） |

字段对齐：`prompt_enhance` ≈ v2 `expand_prompt`；`prompt_enhance_locale` 保留。

### 12.6 n>1 多图：`multi_image_mode`（吸收 v2）

| 模式 | API 字段 | 行为 |
|------|----------|------|
| **fast**（默认） | `multi_image_mode=fast` | pS **最多 1 次**；同一 prompt 连续 n 次 sS（每次独立占 sS 槽、独立选号） |
| **diverse** | `multi_image_mode=diverse` | 每张 **完整 pS→sS** 循环 |

- `n=1` 忽略该字段。
- 与 UI「同 turn 2×2 账号轮换」兼容：fast 模式下 n 次 sS 仍按 turn_binding 轮换账号。
- 额度：fast 省 pS 次数；diverse 耗更多 text/image 机会。

### 12.7 排队时间拆分（修正「假 86s queue」）

写入 `phase_timings_ms` / task 字段：

```text
admit_queue_ms    # 全局软上限，通常 ≈0
upload_queue_ms   # edits only
ps_queue_ms       # 仅等 pS 槽
ss_queue_ms       # 仅等 sS 槽（主排队项应落这里，而非总 queue）
download_queue_ms
```

对外 API 增加 `wall_clock_ms`；**禁止**把各段 queue 加总后仍叫单一 `duration_ms` 冒充执行时间。

### 12.8 分阶段重试（按路径分叉）

**Sync Orchestrator（内存）**：

| 失败点 | 策略 | 回退 SSE？ |
|--------|------|------------|
| pS 失败 | 换号重试 pS ≤N；仍失败 skip pS | 否 |
| sS 无 sediment | 换号重试 sS ≤N | 否 |
| 有 sediment，download 失败 | 仅重试 download | **否** |
| 进程重启 | 请求失败，**客户端重试** | — |

计费：`mark_image_result` / quota **请求级一次**（sync 成功时一次）。

**Async Job（持久化）**：

| 失败点 | 策略 |
|--------|------|
| 有 `sediment_ids`（`SS_DONE`） | 仅从 download 续；probe 失效才回退 SSE ≤1 |
| 无 sediment | `timeout_pending` / resume poll（现网已有） |
| 进程重启 | `retry_phase_cursor` 入库恢复（§11.2） |

### 12.9 可观测（吸收 v2 Timeline）

- Snapshot：`prompt_active/queued`、`sse_active/queued`、`upload_active`、`download_active`
- Gantt：**20 行**（pS slot 1–10 + sS slot 1–10）；segment 按 `stage` + `slot` 定位
- 指标条：`pS 3/10 · sS 7/10 · 排队 pS:2 sS:5`

### 12.10 配置（吸收 v2）

```yaml
image_pipeline:
  prompt_slots: 10      # 改后需重启
  sse_slots: 10
  download_concurrency: 8
  global_queue_max: 200   # 软上限，超限拒单
  asset_upload_concurrency: 8
```

废弃作为**主调度**的：`per_user_running` 全程语义、`submit_workers` 冒充并发（worker 仅驱动状态机，不定义并发上限）。

### 12.11 v2 优点 → 本方案采纳矩阵

| v2 优点 | 本仓采纳 | 说明 |
|---------|----------|------|
| 双池 FIFO + 独立选号 | **是（P0）** | 核心 |
| 全局软上限拒单 | **是（P0）** | 替代深层 admit FIFO |
| 分阶段 queue_ms | **是（P0）** | 修 UI 60s vs 120s 口径 |
| 禁止全程占槽 | **是（P0）** | 拆 `_run_task` |
| 首选异号 / 小池回退 | **是（P0）** | §11.3 |
| expand 短/长启发式 | **是** | 仅 `prompt_enhance=true` 时 |
| multi_image_mode | **是** | fast / diverse |
| Lite skip upload | **是** | 文生图跳过 UQ |
| download 池 8 非 10 | **是** | |
| 请求内内存 checkpoint | **是（sync 路径）** | |
| 跨重启 cursor | **仅 async** | §11.2 |
| Trace 非任务队列 | **是** | trace 只观测 |
| ε-greedy ACI | **二期** | §11.4 |
| READY 512MB 硬水位 | **P1 async** | sync url 模式可先监控 |
| sediment 跨重启 | **async 已有** | v2 Lite 无此需求 |

### 12.12 实施顺序（修订）

1. **Domain**：`phase_timings` 五段 queue + `AccountProvider` 接口 + `retry_phase_cursor`（async）
2. **Orchestrator 双池**：`ps_pool` / `ss_pool` / `upload` / `download` 信号量；去掉全程占槽
3. **Gateway 分流**：sync 走 request-scoped；async 走 job 持久化
4. **API**：`prompt_enhance`、`prompt_enhance_locale`、`multi_image_mode`
5. **前端**：20 行 Gantt + 分 queue 展示 + `ImageJobQueue`
6. **Panda conc10 回归**：目标 P50 ~40–50s（执行段），P95 墙钟显著低于现 137s+ 假排队

### 12.13 架构结论（回应「双槽是否最优」）

在 **pS≈SSE 并列瓶颈**（bench expand~21s、sse~21s、download~1s）且要求 **分阶段限流 + 独立选号** 时，**pS×10 + sS×10 双池** 收益/复杂度比最高（与 v2 结论一致）。

首期不做：动态借槽（pS 闲时借给 sS）、固定 10/10 不随号池缩放（号池扩大后仍用配置 10+10，你已确认）。

