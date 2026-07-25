# SLOT-RUST Layer 1 — 执行计划

最后更新：**2026-07-25**  
真相源：[`docs/26-slot-lifecycle-rust-roadmap.md`](docs/26-slot-lifecycle-rust-roadmap.md)、[`docs/27-pipeline-watchdog-monitoring-matrix.md`](docs/27-pipeline-watchdog-monitoring-matrix.md)

## SLO（Layer 1 完成后复测）

| 指标 | 基线（pre-slotledger） | 目标 |
|------|------------------------|------|
| conc10 成功率 | 4/10（040240Z）/ 10/10（023900Z） | ≥8/10 稳定 |
| `image_inflight` 泄漏 | 曾 10/10 堵死 | 0 漂移（watchdog 告警） |
| sS 假超时 225s | 6 路 `no conversation_id` | **75s** 墙钟失败+释槽 |
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
- [x] `services/image_pipeline/pipeline_watchdog.py`（inflight 对账 + health 打点）
- [x] `account_service.reconcile_inflight()`
- [x] orchestrator：`acquire_ss`/`release_ss` 登记 SlotLedger
- [x] **sS 75s** 墙钟：`config.image_ss_stage_wall_timeout_secs` + `assert_ss_wall_ok`
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
