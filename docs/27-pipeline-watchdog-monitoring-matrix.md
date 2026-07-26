# 27 — 流水线看门狗与监控矩阵

最后更新：**2026-07-26**（纠偏）
状态：**部分失效** — 设计意图仍权威，但下表多处 ✅ 已被 `28` 号审计推翻
关联：`21-image-scheduling-and-pipeline.md`、`26-slot-lifecycle-rust-roadmap.md`、`image_deadlock_guard_service.py`、**`28-scheduling-queue-slot-audit-20260726.md`**

> **纠偏（2026-07-26，源：`28` 号审计，Panda 生产代码确证）**
>
> 1. 本文 ✅ 表示"代码已就位"，**不等于运行时生效**。`pipeline_watchdog.tick()` 的
>    `force_release_expired` 与 `reconcile_inflight()` 的 `force` **均硬编码 `False`**
>    （`api/system.py:390`、`pipeline_watchdog.py:51`），修正分支为死代码 → 看门狗只报不修；
>    且无后台定时器（寄生 `/health` 处理器）。见 `28` §A4。
> 2. **sS 75s 墙钟方向有害** — 比它所包裹的 120/300/360s 合法轮询短 1.6~4.8 倍，
>    会在图片已生成并下载完成后抛错丢弃。见 `28` §B1。
> 3. `ss_active` / `ss_queued` 因取错 key 恒为 0（活体 `pipeline_pools = null`），
>    §4 用它们做横评不成立。见 `28` §A5。
>
> 下表原文保留，仅加行内批注（`⚠️` 列）。

---

## 1. 阶段矩阵

| 阶段 | 入口 | 当前 deadline | 看门狗 | 死锁风险 | 建议守卫 | ⚠️ 2026-07-26 实测 |
|------|------|---------------|--------|----------|----------|--------------------|
| **task_queue admit** | `ImageTaskService.submit` | 队列满 429 | ❌ | 低 | 队列深度 + ETA 暴露 | ETA 低估 2x，见 `28` §4 |
| **worker dequeue** | `_worker_loop` | — | ❌ | 中 | worker 心跳 + stuck running 检测 | 无 try/except，异常杀线程后无 reaper（`28` B10） |
| **account_queue** | `get_available_access_token` | `image_global_queue_timeout_secs` | ❌ | 中 | phase_timings `account_queue_ms` | 该 key=0.0 → **立即拒绝**；且队列任务全部绕过（`28` §5） |
| **pS 槽** | `acquire_ps` | 无硬超时 | ❌ | 中 | 槽 holder 年龄 + pool snapshot | 确认无 timeout，可无限阻塞 |
| **sS 槽（全阶段）** | `acquire_ss` → stream 结束 | **75s 墙钟** ✅ | ✅ SlotLedger | **高** | `assert_ss_wall_ok` + forced release | **墙钟方向有害**（`28` B1）；forced release 从未执行（A4）；重试永久泄漏槽（B2） |
| **pre_conversation** | prepare + conduit | **240s** | 部分 | 高 | 独立 watchdog；失败换号 | — |
| **SSE post-ready** | `conversation_id` 后 | **75s** (`image_sse_post_ready_timeout_secs`) | ✅ | 高 | 已有 soft valve | 活体该值 = `null` → **valve 关闭**（`28` §5） |
| **poll / timeout_pending** | resume worker | `resume_deadline_ts` | 部分 | 中 | 续轮询上限 + evict | 阶梯 720s > 客户端 540s（`28` B7）；队列暂停时永不终态化 |
| **download** | `acquire_download` | 无独立硬超时 | ❌ | 低 | 带宽桶 + 并发槽 | 真实并发是 `download_concurrency=8`，`download_workers=4` 为死配置 |
| **ready_buffer** | `wait_for_ss_slot` | 背压 bytes/items | 部分 | 中 | hysteresis + snapshot | 无 TTL；恢复只看 bytes → item 级背压失效 |
| **image_inflight** | `acquire_image_slot` | 无（靠任务终态） | ✅ reconcile | **极高** | `reconcile_inflight` + SlotLedger | `force=False` → **只报不修**（`28` A4）；另有双重释放（B4） |
| **CPU / maintenance** | deadlock_guard | **90% × 60s** | ✅ | 高 | 暂停 submit + maintenance pause | 死区 bug：两样本即跳闸，65~90% 区间永不复位（`28` B8） |
| **终端内存** | `_cleanup_locked` | **90s** retention | ✅ | 低 | TERMINAL_MEMORY evict | DB 397MB / 1 行，99.48% 空洞（`28` §8） |

---

## 2. 除 sS 75s 外优先加守卫的节点（P0）

1. **`pre_conversation` 240s** — 占 sS 槽但无 conversation_id；应与 sS 75s 联动失败释槽。
2. **`image_inflight` 对账** — `pipeline_watchdog_service.tick()` + `reconcile_inflight()`；漂移 >0 告警。
3. **task hard timeout 540s** — worker 强杀路径必须释 account + sS + ledger（部分已修）。
4. **SlotPool 排队无限等待** — pS/sS `acquire` 无 timeout 时高负载可假死；建议可配置 `pool_acquire_timeout_secs`。
5. **进程守护** — Panda `docker compose` restart policy + 本地 `start_backend.ps1` watchdog（已有）。
6. **ready_buffer 背压** — bytes 满时 sS 等待；需 health 暴露 `ready_buffer` 水位。

---

## 3. 监控扩展（已实现 / 待做）

### 已实现（2026-07-25）

- `/health?format=json`：`pipeline_watchdog`、`pre_ticket_pool`、`slot_ledger` stats
- `GET /api/ops/image-pipeline/snapshot`：池 active/queued + segments
- `phase_timings_ms` 五段 + `wall_clock_ms`
- `image_deadlock_guard_service` CPU 熔断

### 待做（P1）

- Ops UI：各阶段 deadline 着色（超时红条）
- `schedule_trace` 事件：`ss_wall_timeout`、`inflight_drift`、`ledger_forced_release`
- call log 字段：`watchdog_snapshot` 摘要

---

## 4. 横评指标（前后对比）

采集脚本：`scripts/capture_performance_baseline.py`

| 指标 | 用途 |
|------|------|
| `rss_mb` | 内存回归 |
| `image_inflight_count` / drift | 泄漏检测 |
| `ready_candidate_count` / `dispatchable` | 调度面（**注**：与 `image_schedulable` 差 10，见 `28` A2） |
| ~~`ss_active` / `ss_queued`~~ | ~~sS 瓶颈~~ — **恒为 0，指标无效**，修 `28` A5 后才可用 |
| conc10 pass rate | 端到端 SLO |
| `phase_timings_ms` p50/p95 | 分段耗时 |

基线：`docs/captures/spa/BASELINE-pre-slotledger-*`  
完成后：`BASELINE-post-slotledger-*`

---

## 5. 账号/基础设施 Rust（非本矩阵范围）

号池 **N>50** 或 `global_concurrency>20` 时再启动；见 `plan.md` Phase 3、`04` backlog Layer 2。
