# 生图调度：前端 P-C、分阶段耗时与多阶段流水线

状态：**设计记录**（2026-07-23）  
关联：`08-image-pipeline-newapi-async-plan.md`（IMG-012）、`20-pure-http-image-sentinel-todo.md`

---

## 1. 背景问题（用户侧）

1. UI 一提交即「排队中」，与 bench 直连验收体验不一致。
2. 完成态显示 50–60s，用户墙钟感知约 120s（排队 + 执行口径不一致）。
3. 后端真实耗时由上行、上游 SSE、poll、下行构成，**SSE 占大头**；上下行受带宽限制。

---

## 2. 当前架构（三级生产者-消费者）

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
5. **验收**：24 路混合输入下，对比「单池 running」vs「分阶段流水线」的 p50/p95 墙钟与带宽曲线。
