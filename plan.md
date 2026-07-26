# SLOT-RUST Layer 1 — 执行计划

最后更新：**2026-07-26**（纠偏）
真相源：[`docs/26-slot-lifecycle-rust-roadmap.md`](docs/26-slot-lifecycle-rust-roadmap.md)、[`docs/27-pipeline-watchdog-monitoring-matrix.md`](docs/27-pipeline-watchdog-monitoring-matrix.md)、**[`docs/28-scheduling-queue-slot-audit-20260726.md`](docs/28-scheduling-queue-slot-audit-20260726.md)**

> **纠偏（2026-07-26）** — `28` 号审计在 Panda 生产代码上确证：Phase 1 中若干 `[x]` 属
> **代码已就位但运行时未生效或方向有害**。勾选状态保留（代码确实在位），受影响项已就地批注。
> 整治批次见 `docs/04` §AUDIT-28 —— **必须先做批次 0**（打开 watchdog force 会引爆 Python
> ledger 自死锁）。

## SLO（Layer 1 完成后复测）

| 指标 | 基线（pre-slotledger） | 目标 |
|------|------------------------|------|
| conc10 成功率 | 4/10（040240Z）/ 10/10（023900Z） | ≥8/10 稳定 |
| `image_inflight` 泄漏 | 曾 10/10 堵死 | 0 漂移（watchdog 告警） |
| sS 假超时 225s | 6 路 `no conversation_id` | ~~**75s** 墙钟失败+释槽~~ → ⚠️ **目标需重定**：75s 同时落在成功路径正常区间（EWMA 初值即 60s），会连成功一起杀。改为 `> max(poll_timeout)+margin` 或仅覆盖 SSE 阶段（`28` B1） |
| conc10 后 RSS | ~259 MB（修复后） | ≤280 MB |
| account_queue 占比 | 0.1% | <5% |

基线采集：`scripts/capture_performance_baseline.py` → `docs/captures/spa/BASELINE-pre-slotledger-*`

---

## Phase 0 — 基线横评（当前）

- [x] 基线脚本 `scripts/capture_performance_baseline.py`
- [x] 文档化 conc10 时间线（`docs/26` §1.2）
- [ ] Panda 上跑 conc10 + 基线 JSON（CF 换绑后）
- [ ] Layer 1 完成后 `BASELINE-post-slotledger-*` 对比报告

---

## Phase 1 — SlotLedger + FSM + 看门狗【P0 · 进行中】

### Rust（`crates/image_schedule_core`）

- [x] `slot_ledger.rs`：account/sS 租约 FSM、`watchdog_tick` 强制过期释槽
- [x] FFI：`isc_slot_ledger_*` 导出
- [ ] Docker 构建 Linux `.so` → `native/libimage_schedule_core.so` → Panda artifact 部署
- [ ] Python 全面切到 FFI 持账（禁止直接 `_image_inflight` mutate）— **渐进**：当前 shadow + reconcile

### Python 集成

- [x] `services/image_pipeline/slot_ledger.py`（Rust + Python fallback）
  - ⚠️ Python fallback `watchdog_tick` 用非可重入 `Lock` 且锁内调 `release_*` → **自死锁**（`28` B3 / A0-1）
- [x] `services/image_pipeline/pipeline_watchdog.py`（inflight 对账 + health 打点）
  - ⚠️ **惰性**：`force_release_expired=False` 硬编码、无后台定时器（寄生 `/health`）；`ss_active/queued` 取错 key 恒为 0（`28` A4/A5 / A3-1~A3-3）
- [x] `account_service.reconcile_inflight()`
  - ⚠️ `force=False` 硬编码，修正分支 `if force and memory > expected` 为**死代码**（`28` A4 / A3-1）
- [x] orchestrator：`acquire_ss`/`release_ss` 登记 SlotLedger
  - ⚠️ `_ss_released_indices` 只增不减 → **重试永久泄漏 sS 槽**，10 次耗尽后无 timeout 挂死（`28` B2 / A1-3、A1-4）
- [x] **sS 75s** 墙钟：`config.image_ss_stage_wall_timeout_secs` + `assert_ss_wall_ok`
  - ⚠️ **方向有害**：75s 比所包裹的 120/300/360s 合法轮询短 1.6~4.8 倍，在图已生成并下载后抛错丢弃，且 `conversation_id` 被清空致续轮询短路（`28` B1 / A1-1、A1-2）
- [ ] hard timeout / 异常路径全量 `release_account_ledger`
- [ ] `failure_retry_enabled` / `failure_retry_max` API+UI

### 预开票池【P1】

- [x] `services/image_pipeline/pre_ticket_pool.py`（按账号 TTL 缓存）
- [ ] 接入 `openai_backend_api` requirements 获取路径（`pre_ticket_pool_enabled`）

### 任务内存 evict【P1】

- [x] `TERMINAL_MEMORY_RETENTION_SECS=90`（已有）
- [ ] 与 SlotLedger reconcile 联动日志

---

## Phase 2 — 监控扩大【P0】

- [x] `docs/27-pipeline-watchdog-monitoring-matrix.md`（全链路节点矩阵）
- [x] `/health?format=json` 扩展：`pipeline_watchdog`、`pre_ticket_pool`、`slot_ledger`
- [ ] Ops 甘特：各阶段 deadline 着色（超时红条）
- [ ] `schedule_trace` 事件：ss_wall_timeout、inflight_drift、forced_release

详见 `27`：除 sS 75s 外，P0 待加守卫 — `pre_conversation` 240s、`task hard` 540s、inflight reconcile、CPU deadlock_guard、ready_buffer 背压。

---

## Phase 3 — 账号/基础设施 Rust【P2 · 号池扩大后】

**触发条件**（满足任一启动）：

- 号池 **N > 50** 或 `global_concurrency > 20`
- account_queue 占比持续 **>10%**
- Python `account_service` 锁竞争成为 profiling 热点

**范围**：账号 CRUD/缓存、CF 探活编排、proxy 绑定 — **非当前 sprint**。

详见 `docs/04-improvement-backlog.md` §SLOT-RUST Layer 2。

---

## 命令

```bash
# 基线
python scripts/capture_performance_baseline.py

# Rust 本地构建（Windows DLL / WSL Linux .so）
python scripts/build_schedule_trace_linux.py

# conc10 验收
python scripts/_tmp_run_conc10_phases.py

# health 监控（含 watchdog）
curl -s "http://127.0.0.1:8000/health?format=json" | jq '.pipeline_watchdog,.pre_ticket_pool'
```

---

## 关键文件

| 文件 | 变更 |
|------|------|
| `crates/image_schedule_core/src/slot_ledger.rs` | SlotLedger FSM + watchdog |
| `services/image_pipeline/slot_ledger.py` | FFI 封装 |
| `services/image_pipeline/pipeline_watchdog.py` | inflight 对账 |
| `services/image_pipeline/pre_ticket_pool.py` | 预开票 TTL 池 |
| `services/image_pipeline/orchestrator.py` | sS 75s + ledger 登记 |
| `services/account_service.py` | `reconcile_inflight()` |
| `api/system.py` | health 扩展 |
| `scripts/capture_performance_baseline.py` | 前后横评基线 |
