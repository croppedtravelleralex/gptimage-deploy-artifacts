# 26 — 槽位生命周期、释槽路径与 Rust 演进路线

最后更新：**2026-07-25**  
状态：**权威**（槽位语义 + 2026-07-25 conc10 事故复盘 + Rust 价值评估）  
关联：`21-image-scheduling-and-pipeline.md`、`14-rust-rewrite-plan.md`、`17-cf403-and-egress.md`、`captures/spa/PROD-conc10-20260725T*.md`

---

## 1. 2026-07-25 生产事实同步（全量）

### 1.1 号池与 1IP1号

| 项 | 值 | 说明 |
|----|-----|------|
| `total` | 19 | Panda `chatgpt2api-local` |
| `image_schedulable` | 16 | 3 个 `限流` quota=0 |
| `unique_egress` | 19 | `_tmp_verify_egress_display.py` → `ok: true` |
| 换绑脚本 | `scripts/panda_rebind_unique_proxies.py` | 按 binding∪egress 连通分量去重；`--apply` 写库 |
| CF 探测 | `scripts/_tmp_cf_probe_failed_accounts.py` | 7 账号 sticky 出口 CF 探活 |
| CF 换绑 | `scripts/_tmp_rebind_cf_bad_accounts.py` | **唯一 egress + CF 实测 + sticky 校验** 后写库（禁止 blind `swap_account_proxy_on_cf` 链式传递） |
| CF failover 修复 | `services/proxy_cf_failover.py` | `pick_swap_proxy` 排除已占用 `egress_ip`；换绑须 CF 实测（待与 `_rebind_cf_bad` 逻辑合并进 failover） |

**CF 探测结论（2026-07-25）**：conc10 失败 6 号中 5 个出口 CF 不通；`qaflowud630wbo2a` 出口 CF 通 → 225s 更宜查账号/调度侧。换绑后 **7/7 cf_ok=true**。

### 1.2 conc10 验收时间线

| Stamp | 结果 | 主因 |
|-------|------|------|
| `PROD-conc10-20260725T033622Z` | 6/10 | 7 号共 egress `92.113.246.215`；换绑后部分代理质量差 |
| `PROD-conc10-20260725T034701Z` | **0/10** | `image_inflight=10` 泄漏堵死全局闸门；非 admission |
| `PROD-conc10-20260725T040240Z` | 4/10 | 释槽修复后；6 路上游 225s `no conversation_id`（CF/代理） |
| 参考达标 | **10/10** | `PROD-conc10-20260724T150152Z`、`PROD-conc10-20260725T023900Z` |

**阶段占比（040240Z，有 trace 的样本）**：`account_queue` **0.1%**（原 22.8%）；瓶颈仍在上游 SSE/开票，非取号。

### 1.3 `dispatchable=6` 的确切原因（非猜测）

health 快照：`schedulable=16`，`ready_candidate_count=6`，`preflight_backoff=0`，`inflight=3`。

- `_list_ready_candidate_tokens` **不过滤** `image_inflight`。
- conc10 **10 路均执行** `mark_image_result` → `_stamp_image_next_ok()` → 写入 `image_next_ok_ts = now + gap`（humanlike scheduler，`image_min_interval_sec` ≈ 60s × jitter）。
- 10 个参与账号进入 **`interval_not_ready`**，排除出 ready 池。
- **16 − 10 = 6** 即为当时 `ready` / `dispatchable` 数。
- 间隔到期后复测：`ready_count=16, gap=0`（`_tmp_diag_ready_pool.py`）。

### 1.4 `image_inflight` 泄漏（3 账号）

| 账号 | conc10 结果 | 泄漏机制 |
|------|-------------|----------|
| `qaflowud630wbo2a` | 客户端 540s 放弃 | hard timeout 后 runner 未完全收尾 |
| `blakekyle5108` | HTTP 200 成功 | `release_account_after_sse` 与 `mark_image_result` 不对称 |
| `qaflowxho1z6hynk` | 225s 硬超时 | hard timeout 时 `leased_tokens` 未含已取 token（`acquire_ss` 前未回调） |

**已修（Python，已同步 Panda `services/` 挂载）**：

1. `conversation.py`：`account_acquired` 回调提前到 `acquire_ss` 之前。
2. `image_task_service.py`：hard timeout 从 `pipeline_run._account_access_token` 补释；`_log_call` 终态 evict；`TERMINAL_MEMORY_RETENTION_SECS=90`。
3. `account_service.py`：有 `preferred_email` 时等待该号 slot，不抢错号。
4. `account_lease_pool.py`：`seed_queued_preferences` + `MAX_HINTS=20`。

**Rust 未参与释槽**：`libimage_schedule_core.so` 仅 dispatch gate / sediment / lease hint 原型，**不持有** `image_inflight`。

### 1.5 内存 RSS

| 时机 | RSS |
|------|-----|
| 容器重启后 | ~104 MB |
| conc10 后（修复前） | ~443 MB |
| conc10 后（evict+compact） | ~259 MB |

### 1.6 Layer 1 落地进度（2026-07-25）

| 项 | 状态 |
|----|------|
| Rust `SlotLedger` + FFI + Python fallback | **已落地**（`slot_ledger.rs` / `slot_ledger.py`） |
| sS 阶段 **75s** 墙钟（`acquire_ss` 起算） | **已落地**（`image_ss_stage_wall_timeout_secs` + `assert_ss_wall_ok`） |
| `pipeline_watchdog` + `reconcile_inflight()` | **已落地**（health JSON 暴露） |
| 预开票池（按账号 TTL） | **骨架+requirements 缓存**（`pre_ticket_pool.py`） |
| 任务内存 evict 90s | **已有**（`TERMINAL_MEMORY_RETENTION_SECS`） |
| 基线横评 | `BASELINE-pre-slotledger-20260725T072257Z` |
| API/UI：`failure_retry_enabled` | **待开发** |
| Python 全面禁止 `_image_inflight` 直改 | **渐进**（shadow ledger + reconcile） |
| 账号/基础设施 Rust | **P2**（N>50 或 global_concurrency>20）见 `plan.md` Phase 3 |

---

## 2. 槽位设计：不是 Rust，是 Python 双轨

### 2.1 两类闸门

```text
[ImageTaskService] task_queue → admit → (pS 槽)
    → account_queue: get_available_access_token()  ← image_inflight +1  【账号在途】
    → ss_queue: acquire_ss()                        ← SlotPool ss +1     【sS 槽】
    → 上游 Sentinel / conversation / SSE / poll
    → release_ss() / release_account_slot / mark_image_result
```

| 名称 | 实现 | 文件 | 上限配置 |
|------|------|------|----------|
| **账号在途** `image_inflight` | `dict[token→count]` + `Condition` | `account_service.py` | `image_global_concurrency`（10）、`image_account_concurrency`（2）、binding 1 |
| **pS 槽** | `SlotPool` | `pools.py` / `orchestrator.py` | `image_pipeline.prompt_slots` |
| **sS 槽** | `SlotPool` | 同上 | `image_pipeline.sse_slots`（10） |
| **upload/download** | `SemaphorePool` | 同上 | `asset_upload_concurrency` / `download_concurrency` |
| **lease hint** | `AccountLeasePool`（不占 inflight） | `account_lease_pool.py` | `MAX_HINTS=20` |

**为何 account 槽在 sS 之前**：全局最多 10 个账号同时生图。若先排队 sS 再占号，10 路可在 sS 门外堆叠且各持有 token，全局闸门失效。

### 2.2 Rust 现状（`.so` 插件，非槽位所有者）

| 组件 | 路径 | 职责 |
|------|------|------|
| `libimage_schedule_trace.so` | `crates/image_schedule_trace` | 调度事件打点 → phase_timings |
| `libimage_schedule_core.so` | `crates/image_schedule_core` | `DispatchGate`（submit 间隔）、`LeasePool`（hint 深度，**未接 inflight**）、`SedimentParser` |
| Python 绑定 | `schedule_core.py` / `schedule_trace.py` | ctypes 加载；无 .so 时 Python fallback |

**结论**：槽位账本 100% 在 Python `account_service._image_inflight` 与 `SlotPool`；Rust 不负责 acquire/release，**不可能**靠现有 Rust 避免 inflight 泄漏。

---

## 3. Python 释槽路径（完整清单）

### 3.1 占用（+1）

| 路径 | 调用点 |
|------|--------|
| 取号 | `account_service.get_available_access_token()` → `_image_inflight[token]+=1` |
| PS 阶段 | 同上（`acquire_for_ps`） |

### 3.2 释放（−1）

| 路径 | 调用点 | 注意 |
|------|--------|------|
| 正常结束 | `mark_image_result()` 内 `release_image_slot()` | 成功/失败均释 |
| SSE 早释 | `orchestrator._release_account_slot_after_sse()` | `release_account_after_sse=true` 时 SSE 结束即释 |
| Lease 显式 | `account_provider._Lease.release()` | 仅 `RuntimeError`/`TimeoutError` 在取号阶段 |
| Hard timeout | `image_task_service` → `mark_image_result` / `release_slot_once` | 依赖 `leased_tokens` 完整性 |
| 取消迟到 token | `progress_callback` → `release_slot_once` | cancel 后迟到的 `get_available_access_token` |
| Poll 续轮 | `timeout_pending` 路径 `force_released` | 有 `conversation_id` 时 |

### 3.3 已知漏洞类（状态机，非 GIL）

1. **回调顺序**：取号后、`account_acquired` 回调前卡在 `acquire_ss` → timeout 时 `leased_tokens` 空。（已修）
2. **双释/漏释**：`release_account_after_sse` + `mark_image_result` 组合；cancel 路径 `ImageStreamCancelledError` 故意不 mark。
3. **无对账**：进程内 `inflight` 与 `ImageTaskService._tasks` unfinished 无周期性 reconcile。
4. **无 RAII**：Python 无析构保证；线程 cancel 后仍可能持有引用。

### 3.4 sS 槽释放

| 路径 | 调用点 |
|------|--------|
| 正常 | `orchestrator.release_ss()` / `finally` in `conversation.py` |
| sediment 快路径 | `on_sediment_captured` 可提前 release ss |
| **缺失** | 占 sS 后 75s 无结果强制失败（待做） |

---

## 4. Rust 重写价值评估

### 4.1 问题本质

| 现象 | 主因类型 | Rust 能否直接解决 |
|------|----------|-------------------|
| inflight 泄漏 | 生命周期/取消状态机 | ✅ 用 RAII guard 大幅降低 |
| 4/10 上游超时 | 账号出口 CF / 上游 | ❌ 账号侧另做 |
| dispatchable=6 | humanlike `image_next_ok_ts` | ❌ 产品策略 |
| account_queue 22.8%→0.1% | Python 调度优化 | 已解决；Rust 非必须 |
| RSS 443→259MB | Python 对象持有 | 部分（紧凑结构）；I/O 仍占大头 |

**高并发下 Python 不如 Rust** 对本栈成立处：**槽位账本 + 队列 + 取消**（CPU 轻、锁竞争、状态多线程）。**不成立处**：上游 SSE/poll（I/O  bound，curl_cffi 已 native）。

### 4.2 推荐路线（非一夜全量重写）

与 `14-rust-rewrite-plan.md` Phase C 对齐，**分三层**：

```text
Layer 1（本仓，1–2 周）— SlotLedger .so
  - Rust 持有 image_inflight + ss/ps slot 计数
  - acquire 返回 Guard；drop/timeout 强制 release
  - Python 仅 I/O；账本 FFI 单写
  - 验收：conc10 后 inflight 恒为 0；无 0/10 堵死

Layer 2（gptimage-gateway-rs Phase C）— 编排进程内
  - admission / dispatch / lease pool 迁入 gateway :8013
  - 与 curl_cffi helper 同进程，减 GIL 交叉

Layer 3（Phase E）— 生产切流
  - :8012 → gateway canary
  - 账号池/CF/换绑仍 Python 或 sidecar API
```

### 4.3 全量 Rust 重写调度/队列？

| 方案 | 价值 | 成本 | 建议 |
|------|------|------|------|
| **仅 SlotLedger + sS 超时** | 高（直击泄漏+75s） | 低 | ✅ **优先** |
| **schedule_core 扩权接管 inflight** | 高 | 中 | ✅ Layer 1 |
| **gateway-rs 接管生图编排** | 中高 | 高 | 按 `14` Phase C→E |
| **conversation/SSE 全 Rust** | 低~中 | 极高 | ❌ 非当前瓶颈 |
| **账号/CF/换绑全 Rust** | 低 | 高 | ❌ 账号问题独立治理 |

**原则**：**程序问题（调度、队列、释槽 bug）** → Rust 账本 + RAII；**账号问题（CF、额度、token）** → 探活、换绑、quarantine（现有 Python 脚本链）。

### 4.4 SlotLedger 接口草案（供实现）

```rust
// acquire_account(token, task_key) -> SlotGuard
// acquire_ss(task_key) -> SsGuard  // 内置 75s deadline
// impl Drop for SlotGuard { release_inflight() }
// reconcile() -> Vec<LeakedSlot>   // inflight>0 且无 task_key
```

Python 侧：`get_available_access_token` 改为调用 `SlotLedger::try_acquire_account`；禁止直接 mutate `_image_inflight`。

---

## 5. 运维命令

```bash
# 1IP1号核对
python scripts/_tmp_verify_egress_display.py

# ready 池 / inflight 诊断（容器内）
docker exec chatgpt2api-local env PYTHONPATH=/app /app/.venv/bin/python /app/scripts/_tmp_diag_ready_pool.py
docker exec chatgpt2api-local env PYTHONPATH=/app /app/.venv/bin/python /app/scripts/_tmp_diag_inflight_leak.py

# CF 探测 + 换绑
docker exec chatgpt2api-local env PYTHONPATH=/app /app/.venv/bin/python /app/scripts/_tmp_cf_probe_failed_accounts.py
docker exec chatgpt2api-local env PYTHONPATH=/app /app/.venv/bin/python /app/scripts/_tmp_rebind_cf_bad_accounts.py

# 全量去重换绑
docker exec chatgpt2api-local env PYTHONPATH=/app /app/.venv/bin/python /app/scripts/panda_rebind_unique_proxies.py --apply
```

---

## 6. 变更记录

| 日期 | 变更 |
|------|------|
| 2026-07-25 | 初版：conc10 事故、释槽路径、Rust 评估、Layer 1–3 路线 |
