# 27 — 流水线看门狗与监控矩阵

最后更新：**2026-07-25**  
状态：**权威**（全链路超时/看门狗/死锁守卫清单）  
关联：`21-image-scheduling-and-pipeline.md`、`26-slot-lifecycle-rust-roadmap.md`、`image_deadlock_guard_service.py`

---

## 1. 阶段矩阵

| 阶段 | 入口 | 当前 deadline | 看门狗 | 死锁风险 | 建议守卫 |
|------|------|---------------|--------|----------|----------|
| **task_queue admit** | `ImageTaskService.submit` | 队列满 429 | ❌ | 低 | 队列深度 + ETA 暴露 |
| **worker dequeue** | `_worker_loop` | — | ❌ | 中 | worker 心跳 + stuck running 检测 |
| **account_queue** | `get_available_access_token` | `image_global_queue_timeout_secs` | ❌ | 中 | phase_timings `account_queue_ms` |
| **pS 槽** | `acquire_ps` | 无硬超时 | ❌ | 中 | 槽 holder 年龄 + pool snapshot |
| **sS 槽（全阶段）** | `acquire_ss` → stream 结束 | **75s 墙钟** ✅ | ✅ SlotLedger | **高** | `assert_ss_wall_ok` + forced release |
| **pre_conversation** | prepare + conduit | **240s** | 部分 | 高 | 独立 watchdog；失败换号 |
| **SSE post-ready** | `conversation_id` 后 | **75s** (`image_sse_post_ready_timeout_secs`) | ✅ | 高 | 已有 soft valve |
| **poll / timeout_pending** | resume worker | `resume_deadline_ts` | 部分 | 中 | 续轮询上限 + evict |
| **download** | `acquire_download` | 无独立硬超时 | ❌ | 低 | 带宽桶 + 并发槽 |
| **ready_buffer** | `wait_for_ss_slot` | 背压 bytes/items | 部分 | 中 | hysteresis + snapshot |
| **image_inflight** | `acquire_image_slot` | 无（靠任务终态） | ✅ reconcile | **极高** | `reconcile_inflight` + SlotLedger |
| **CPU / maintenance** | deadlock_guard | **90% × 60s** | ✅ | 高 | 暂停 submit + maintenance pause |
| **终端内存** | `_cleanup_locked` | **90s** retention | ✅ | 低 | TERMINAL_MEMORY evict |

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
| `ready_candidate_count` / `dispatchable` | 调度面 |
| `ss_active` / `ss_queued` | sS 瓶颈 |
| conc10 pass rate | 端到端 SLO |
| `phase_timings_ms` p50/p95 | 分段耗时 |

基线：`docs/captures/spa/BASELINE-pre-slotledger-*`  
完成后：`BASELINE-post-slotledger-*`

---

## 5. 账号/基础设施 Rust（非本矩阵范围）

号池 **N>50** 或 `global_concurrency>20` 时再启动；见 `plan.md` Phase 3、`04` backlog Layer 2。
