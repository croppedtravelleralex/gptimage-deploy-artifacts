# PROD 延迟分解 — 取号 / sS 槽 / SSE 口径

最后更新：2026-07-24（23:04 CST）  
关联：`PROD-serial10-20260724T143921Z`、`PROD-conc10-20260724T150152Z`、历史 `pipe-conc10-20260724T120352Z`

---

## 1. 问题

- serial5 p95 **118s**、conc10 单任务 p95 **125.7s**，远高于 bench **65s gate**。
- 体感：SSE 不长，**取号慢**。

## 2. 口径（避免误判）

| 字段 | 含义 | 是否「取号」 |
|------|------|-------------|
| `task_queue_ms` | 提交 → worker 开跑 | 否（任务排队） |
| `ss_queue_ms` | 等 **sS 槽**（含 ready_buffer 背压） | 否（槽位排队） |
| **`account_queue_ms`** | `mark_account_wait_start` → `acquire_for_ss` 完成 | **是（真取号）** |
| `sse_stream_ms` | 取号后 → SSE resolve（含 requirements/prepare/ticket/**上游 image_gen 空窗**） | 否（常被误认为 SSE 本体） |
| `poll_resolve_ms` | `ss_ms - account_queue - sse_stream` | 否（轮询） |

**65s gate** 仅用于 bench 脚本判 `image_gen` 是否出现，**不约束生产 pipeline**。

## 3. conc10 实测分解（10/10 成功）

| 阶段 | p50 | p95 | 说明 |
|------|-----|-----|------|
| task_queue_ms | 4.8s | 11.8s | worker / per_user 排队 |
| **account_queue_ms** | **2.1s** | **37.2s** | 取号争用（含 binding 等槽） |
| **ss_queue_ms** | 0 | **49.1s** | 等 sS 槽（尾延迟主因之一） |
| **sse_stream_ms** | 40.3s | **63.8s** | 含上游 60–70s 空窗 + ticket |
| poll_resolve_ms | 6.7s | 8.2s | 正常 |
| wall_clock_ms | 58.0s | 125.7s | 端到端 |

### 最慢任务 `conc10-09`（wall 125.7s）

```
task_queue   8.6s
account_queue 5.1s
ss_queue     49.1s   ← 等 sS 槽
sse_stream   62.6s   ← 含上游延迟
```

### 取号最重 `conc10-00`（account_queue 37.2s）

```
account_queue 37.2s
sse_stream    36.7s
ss_queue      0
wall          83.7s
```

## 4. 根因归纳

1. **不是单一「取号 bug」**：尾延迟 = `task_queue` + **`ss_queue`** + **`account_queue`** + **`sse_stream`**（上游）+ poll 叠加。
2. **conc10 争用**（STAB 条件：`hot=5`、`binding_max=1`、10 并行）：
   - 5 热号 × binding 串行 → `account_queue` 尾部到 **37s**
   - **sS 槽已是 10**（默认/已写入 `config.image_pipeline.sse_slots=10`）。`ss_queue` 长尾来自槽被长任务占用（开票+SSE+轮询 60–120s）及 ready_buffer 背压，**不是只开 2 槽**。
3. **sse_stream p95≈64s**：与文档 **PROTO-UPSTREAM-LATENCY**（上游 60–70s 才出 `image_gen`）一致；**不是 SSE 传输慢**，是 requirements→image_gen 墙钟。
4. **serial5 同号 p95≈118s**：低负载时 `account_queue` 通常 1–3s；118s 主要来自 **sse_stream（上游）** + poll，与 65s gate 无关。

## 5. 建议动作（按优先级）

| 优先级 | 动作 | 预期 |
|--------|------|------|
| P1 | 扩容 **hot 池**（暖号 worker 保持 hot≥conc 需求） | ↓ account_queue / ss_queue 尾延迟 |
| P1 | conc 压测时评估 `image_binding_inflight_max`：同 binding 账号是否过多 | ↓ 37s 取号尾 |
| P2 | 接受上游 60–70s 为现状；SLO 用 **540s** 而非 65s | 口径对齐 |
| P2 | Ops 甘特看 `ss_queue_ms` / `account_queue_ms` 分桶（已有字段） | 复盘争用 |
| P3 | 若需压到 65s 内：需上游降延迟（STAB-D）或减 poll | 非调度能单独解决 |

## 6. 复现命令

```bash
python scripts/_tmp_run_serial10_phases.py
python scripts/_tmp_run_conc10_phases.py
python scripts/_tmp_analyze_call_log_phases.py --prefix pipe-conc10-20260724T120352Z
```

## 7. serial10 vs conc10 占墙钟比（2026-07-24 晚，同步 `/v1/images/generations`）

条件：`image_schedulable=17`、`sse_slots=10`、1IP1号、`dispatch_hot_only=false`；conc10 17 号轮询 `preferred_account_email`。

| 阶段 | serial10 mean% | conc10 mean% | Δ | 说明 |
|------|----------------|--------------|---|------|
| task_queue | **0.0** | **9.7** | +9.7 | 并发 worker 排队（p95 11.6s） |
| account_queue | 5.9 | 4.4 | -1.5 | 1IP1号后取号争用下降 |
| **ss_queue** | **0.0** | **8.6** | **+8.6** | 仅 conc10 尾部 2 任务各 ~25s |
| **sse_stream** | **79.1** | **77.5** | -1.6 | 仍为主瓶颈 |
| poll_resolve | 6.3 | 6.4 | +0.1 | 基本持平 |
| download | 1.8 | 1.4 | -0.4 | 可忽略 |

| 指标 | serial10 | conc10 |
|------|----------|--------|
| 通过率 | 10/10 | 10/10 |
| 单任务墙钟 p50 | 53.2s | 44.6s* |
| 单任务墙钟 p95 | 89.7s | 77.3s / 112.3s** |
| 批次墙钟 | ~9.8min（含 30s 间隔） | **120.5s**（10 并发） |

\*conc10 p50 受多号并行影响，快任务先完成。  
\*\*conc10 最慢 `#10`：ss_queue 25.4s + sse 96.8s → wall 112.3s。

**结论**：槽调度改「先取号后占槽」+ 1IP1号后，conc10 **全通过**且取号占比未恶化；新增尾延迟来自 **task_queue** 与 **ss_queue**（仅 2/10 任务），**SSE 上游仍占 ~78%**。

## 8. 阶段 FAQ（并发上限 / sS 排队 / poll / wall_clock）

### 8.1 生产并发参数（Panda 当前）

| 层级 | 配置项 | 当前值 | 管什么 |
|------|--------|--------|--------|
| **task_queue** | `image_task_queue.submit_workers` | **10** | 同时从 `queued` 拉任务开跑的 worker 数 |
| | `per_user_running_max` / base / burst | **10** | 单 API 用户同时 `running` 的任务上限 |
| | `newapi_image_sync_admission_max` | **12** | 同步 `/v1/images/*` 可同时挂起等待的 HTTP 连接数 |
| **account_queue** | `image_global_concurrency` | **10** | 全池同时走上游生图路的账号路数上限 |
| | `image_account_concurrency` | **2** | 单账号最多 2 路并发生图 |
| | `image_binding_inflight_max` | **1** | 同一 proxy binding 同时占用的生图路数 |
| | `proxy_binding_max_accounts` | **1** | 同一 binding 进调度池的账号数 |
| **sS 槽** | `image_pipeline.sse_slots` | **10** | sS SlotPool FIFO 槽位数 |

**要点**：`task_queue` 与 `account_queue` **都不是 10 路独立并发同一个东西**——前者是「任务 worker 开跑」，后者是「账号/出口令牌」；**sS 槽是第三层**，只管上游 SSE 对话占用。

### 8.2 `task_queue_ms` / `account_queue_ms` 量的是什么

| 字段 | 起止 | 含义 |
|------|------|------|
| `task_queue_ms` | HTTP `created_ts` → `worker_started_ts` | 任务在 **ImageTaskService 队列**里等 worker（受 submit_workers、per_user_running 限制） |
| `account_queue_ms` | `mark_account_wait_start` → `mark_account_acquired` | 等 **`get_available_access_token`**（全局 10 路、单号 2 路、binding 1 路、preflight 等） |

串行压测 `task_queue≈0` 因为一次只提交一个；conc10 均值 **5.7s** 即 10 个任务争抢 **10 个 submit/running 名额** 的排队。

### 8.3 10 任务 = 10 槽，为什么还会有 `ss_queue`？

**你的直觉是对的**：在**隔离环境**里，若严格 **1 任务 = 1 张图 = 1 次 `acquire_ss`**，且 **`sse_slots=10`、同时只跑 10 路**，则数学上 **不应出现 sS 排队**——第 10 个任务去占槽时，前序最多占 9 个槽，必有空位。  
**12 并发、10 槽 → 2 个排队**才符合 SlotPool 模型；**10 并发、10 槽 → 0 排队**才是该模型的正常结论。

先前「先到的 8 个占满 10 槽」的说法 **不成立**，此处更正。

#### `ss_queue_ms` 量的是什么

仅 **`SlotPool.acquire()` 在 FIFO 队列里干等** 的时间（`services/image_pipeline/pools.py`）。  
**不是** task_queue，**不是** account_queue；`ready_buffer` 背压在 `acquire` 之前另算，**不计入** `ss_queue_ms`。

#### 隔离 10=10 下仍看到 `ss_queue` 时，只可能是

| 原因 | 说明 |
|------|------|
| **槽被本批次以外占用** | 生产 Panda 上同时有别的生图/未释放槽；本批 10 路 + 外部 ≥1 路 → 满槽排队 |
| **单任务多占槽** | `n>1` 多图、重试路径重复 `acquire_ss` 且未 `release`（应用 `n=1` 排除；代码上 `continue` 重试会走 `finally release_ss`） |
| **槽泄漏** | 异常路径未 `release_ss`（需对照 `ss.active` 快照排查） |
| **指标/日志对不齐** | call log 按 prompt 标签捞取，**未必与 10 条 HTTP 一一对应 |

#### 本次 `PROD-conc10-20260724T150152Z` 的再核对

- HTTP 侧 10 条结果里的 `account_email` 是脚本填的 **preferred**，**不是**日志里的实际账号。
- 阶段日志 10 条里：`qaflowgq5wyuxhe9` **出现 2 次**；`enrico` / `felicity` / `ivorbrown` / `qaflow0ytb7bbp0z` **未出现在阶段行**——说明 **阶段表与 10 条 HTTP 未严格对齐**，不宜用「第 9、10 个任务」解释排队。
- 有 `ss_queue` 的两条（`gq5` 第二次、`blake`）在 **23:03:17 / 23:03:58** 才完成占槽；按时间线反推，当时池里 **已满 10/10**，但这 10 个占位者 **不能** 仅用「本批前 8 个任务」解释——更可能是 **本批多路已占槽 + 同时段其它请求**，或 **日志 cohort 混杂**。

**结论（设计 vs 实测）**：

- **设计上**：10 槽就是为 10 路并发；**任务数 = 槽数时不应排队**——你说得对，这不难，是模型直接推论。
- **实测 conc10 的 ss_queue**：不能证明「10 槽不够」，更可能是 **未隔离的生产叠加流量** 或 **日志未与 10 HTTP 对齐**；要验证「10=10 零 ss_queue」，应在跑前确认 `GET /api/ops/image-pipeline/snapshot` 里 `ss.active=0`，且按 `task_id` 精确对齐阶段。

若要进一步压实测 ss_queue：隔离压测、按 task_id 拉 phase、sediment 后尽早 `release_ss`（poll 不占槽）。

### 8.4 `poll_resolve_ms` 是什么

**派生字段**（call log / 甘特）：

```text
poll_resolve_ms = ss_ms − account_queue_ms − sse_stream_ms
```

| 子字段 | 含义 |
|--------|------|
| `sse_stream_ms` | 取号完成 → SSE 流结束（requirements / prepare / ticket / 等上游出 `image_gen`） |
| `ss_ms` | **持有 sS 槽**的总时长（acquire → release） |
| `poll_resolve_ms` | 槽内、SSE 已结束后的 **/tasks 或 conversation 轮询**，直到拿到可下载 URL |

典型 **4–6s**；对应日志里的 `urls_resolved` 阶段。不是「SSE 传输慢」，是 **等上游把图写好**。

### 8.5 `wall_clock_ms` 是总时间吗？

**是端到端墙钟，但要看层级：**

| 来源 | 口径 |
|------|------|
| `phase_timings_ms.wall_clock_ms` | Pipeline `begin_run` → `finish()`（worker 内 pipeline 生命周期） |
| `total_wall_ms` / HTTP 客户端 | 往往含 `task_queue`：**提交 → 返回** 全路径 |
| 阶段占墙钟比 | 用各阶段 **mean / wall_clock mean** 估算；`sse_stream` 与 `ss_queue` 在测量上有 **重叠**（`sse_stream` 从取号完成起算，内含等槽时间），占比相加可 **>100%**，看趋势即可 |

串行 HTTP 与 `wall_clock_ms` 差 ~200ms；conc10 若只看 pipeline wall 会 **不含** 前面数秒的 `task_queue`。

---

代码锚点：`services/image_pipeline/orchestrator.py`、`services/protocol/conversation.py`（取号计时）、`services/account_service.py`（`get_available_access_token`）。
