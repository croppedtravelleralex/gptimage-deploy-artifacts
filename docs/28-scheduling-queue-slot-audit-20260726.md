# 28 — 调度 / 队列 / 槽位专项审计（2026-07-26）

最后更新：**2026-07-26**
状态：**权威**（本次审计的分级结论与证据；纠正 `27`、`plan.md`、`04` 中已失效的"已完成"声明）
关联：`21-image-scheduling-and-pipeline.md`、`26-slot-lifecycle-rust-roadmap.md`、`27-pipeline-watchdog-monitoring-matrix.md`

---

## 0. 审计方法与口径

| 项 | 说明 |
|----|------|
| 审计对象 | **Panda 生产运行代码**：`/root/gptimage/{services,api}`（只读挂载进容器 `chatgpt2api-local`） |
| 与本地关系 | 本地 git 工作区与 Panda 已用 md5 逐文件比对，**不一致**；本文结论一律以 Panda 为准 |
| 方法 | 6 个并行子代理分域静态审计（准入/槽位/池/调度器/超时/供给）+ 主控逐条复核 CRITICAL 项 |
| 活体证据 | `docker logs`（6h/24h 聚合）、`/health?format=json`、只读 SQLite（`?mode=ro`）、`date`、cgroup `cpu.max` |
| 变更 | 全程只读。未删除、未修改、未重启任何生产文件或服务 |
| 证据局限 | 审计窗口内**无生图流量**（`image_tasks` 仅 1 行 success），故 B 类问题为静态确证 + 配置实测，无生产实证 |

运行时事实基线（`/health?format=json`，`image_inflight_count = 0` 即完全空载时采集）：

```json
{"schedulable": 17, "image_schedulable": 17, "total_quota": 399,
 "ready_candidate_count": 7, "schedulable_candidate_count": 7,
 "available_candidate_count": 7, "dispatchable_candidate_count": 7,
 "image_account_concurrency_limit": 2, "image_global_concurrency_limit": 10,
 "pipeline_watchdog": {"pipeline_pools": null, "ss_active": 0, "ss_queued": 0,
                       "inflight_drift": {"drift_count": 0}}}
```

部署约束：容器 `cpus: "1.5"`（cgroup `cpu.max = 150000 100000` 实测）、`mem_limit: 1536m`、单 uvicorn worker。

---

## 1. 分级总表

### A 类 — 正在发生（有活体证据）

| # | 级别 | 问题 | 位置 |
|---|------|------|------|
| A1 | CRITICAL | text_nurture 周末全盲，**85% 工作项被静默销毁** | `text_nurture_service.py:378` |
| A2 | CRITICAL | 供给宣称 17 实际 7：CF 探活 2 连败封 24h 且无自愈 | `account_warmup_service.py:351` |
| A3 | CRITICAL | `status=限流` 单向门，账号耗尽一次即**永久**退池 | `account_service.py:3517` |
| A4 | HIGH | watchdog 完全惰性：两个 force 开关硬编码 `False` | `api/system.py:390` |
| A5 | HIGH | sS 池监控恒为 0（取错 key），泄漏不可见 | `pipeline_watchdog.py:53` |

### B 类 — 代码路径确证，等流量触发

| # | 级别 | 问题 | 位置 |
|---|------|------|------|
| B1 | CRITICAL | 75s sS 墙钟杀死 120/300/360s 合法轮询，**丢弃已生成的图** | `orchestrator.py:190` |
| B2 | CRITICAL | 每次重试永久泄漏一个 sS 槽，耗尽后**无超时挂死** | `orchestrator.py:354` |
| B3 | CRITICAL（潜伏） | `_PySlotLedger.watchdog_tick` 非可重入锁自死锁 | `slot_ledger.py:29,93` |
| B4 | HIGH | 账号槽双重释放，偷走同伴槽位导致超卖 | `orchestrator.py:201` |
| B5 | HIGH | 有效单账号并发是 **1** 而非配置的 2 | `account_service.py:695` |
| B6 | HIGH | 24-GET 预算使 300/360s 超时不可达，报错指向错的 key | `image_poll_budget.py:82` |
| B7 | HIGH | resume 阶梯 720s 超出客户端 540s 等待 | `image_task_service.py:1787` |
| B8 | HIGH | deadlock guard 死区：两样本即跳闸，65~90% 区间永不复位 | `image_deadlock_guard_service.py:65` |
| B9 | HIGH | `_image_inflight` / `_in_flight` 泄漏无自愈，累积即永久熔断 | `orchestrator.py:403`、`account_service.py:2893` |
| B10 | HIGH | submit worker 无 try/except，异常杀线程后任务永久 RUNNING 且无 reaper | `image_task_service.py:1217` |

---

## 2. A 类详情

### A1 — 周末全盲 + 破坏性出队

两个独立缺陷叠加。

**（a）预设只填工作日。** `business_hours` / `extended_business` 由 `_fill_weekdays(..., weekdays=(0,1,2,3,4))` 构造，周六周日保留底值：

```python
# services/ip_nurture_schedule.py:83-85
_preset("business_hours", "工作日办公时段",
        _fill_weekdays(_blank_matrix(0.05), slots=_OFFICE, weekdays=(0, 1, 2, 3, 4))),
_preset("extended_business", "工作日加长班",
        _fill_weekdays(_blank_matrix(0.08), slots=_EXTENDED, weekdays=(0, 1, 2, 3, 4))),
```

闸门是**严格大于**：

```python
# services/ip_nurture_schedule.py:20,189
SLOT_ALLOW_THRESHOLD = 0.15
return current_slot_weight(matrix, tz_name=tz_name, now_utc=now_utc) > float(threshold)
```

`0.05 > 0.15` 与 `0.08 > 0.15` 均为假 → 周末全部 binding 被拒。Panda 实测 `date` = **2026-07-26 Sunday CST**，故审计当日全天关闭。日志 165 次/6h `text_nurture slot not allowed` 即此。

同一闸门下另有两个**任何时刻都无法通过**的预设：`minimal`（全天 0.12）、`sg_remote`（底值恰为 0.15，被严格大于挡掉）。

**（b）先破坏性出队，后校验。** 这才是真正的损失来源：

```python
# services/text_nurture_service.py:378-386
item = text_task_queue.dequeue()        # popleft；text_task_queue 是裸 deque
if item is None:
    raise RuntimeError("text nurture queue empty")
data = dict(item.payload)
...
token, account = self._resolve_token(data, settings)   # :386 在此抛异常
```

异常冒泡到 `_loop` 的裸 `except`（只 log，无退避、无自禁用），**全仓无 requeue / retry / 死信路径**。24h 统计：

| 错误 | 次数 | 是否销毁 |
|------|------|----------|
| slot not allowed | 339 | 是 |
| daily account cap reached | 242 | 是 |
| 上游 503/401/500 | 35 | 是 |
| **合计** | **616** | **616 项全部丢失** |

`auto_enqueue_every_sec = 120` 的产能上限是 720/24h → **约 85% 的养号工作项凭空消失**。手动入队同样会被静默销毁。

**附带**：`workload.text_queue_mode = off` 是**死配置**（定义于 `account_workload_policy_service.py:89`，全仓无消费者），运维以为的"关掉文本队列"开关失效；养号 worker 只受 `text_nurture.enabled` + `worker_enabled` 控制。

**附带**：`resolve_binding_matrix`（`ip_nurture_schedule.py:237`）对未配置 binding 用 `hash(key)` 选预设。Python 字符串哈希受 `PYTHONHASHSEED` 随机化，**每次重启换一个预设**，"稳定 per-binding 排班"的语义不成立。

### A2 — CF 探活封禁把供给从 17 削到 7

`is_dispatch_blocked` 是取号链路根部硬过滤（`account_service.py:1998`，被 `_list_available_candidate_tokens:2037`、`_acquire_next_candidate_token:2240`、`get_image_candidate_runtime_stats:2082` 共同依赖）。

逐闸门归因（每步单独验证）：

| 阶段 | 剩余 | 闸门 | 验证 |
|------|------|------|------|
| 原始账号 | 19 | — | DB count |
| status 正常 | 18 | `_is_image_account_available:271` | DB：18 正常 / 1 限流 |
| 无失败证据 | **17** | `_has_image_account_failure_evidence:894` | DB：1 个 `invalid_count=2` |
| → 对外宣称 | **17** | `get_stats():3987` | health 实测 17 |
| 扣 interval/429/preflight/cohort | 17 | — | 实算 0 个被挡 |
| 扣 **warmup CF 封禁** | **7** | `_is_warmup_dispatch_blocked:1998` | health 实测 7 |

`17 − 10 = 7`，与 health 完全闭合。日志给出精确原因：

```json
{"event":"account_warmup_block","email":"...","reason":"cf_probe_streak","streak":2,"block_sec":86400.0}
```

三个放大因素：

1. **2 次失败即封 86400 秒（24h）** — `account_warmup_service.py:351-352`。
2. **`_cf_fail_streak` 无时间窗、无衰减** — `:336` 仅成功时清零，`:234` 仅墙钟到期清零；docstring 写"连续"但实现允许两次失败相隔任意时长。
3. **无自愈路径** — `_warmup_one:320-322` 对已封号直接早退，永远走不到 `:337` 的 `_blocked_until.pop()`。纯内存、无持久化、无解封 API（`api/ops.py:93-96` 只有只读 status）。

24h 内 **15/19（79%）** 账号被封过。按 fail 20/6h ÷ streak 2 ≈ 1.67 个/小时、每个持续 24h 计，在途封禁稳态 ≈ 40 个账号位 **> 池子总量 19** — **该反馈环的不动点是可调度归零**。当前未塌只因被封号不再被探活 + 容器重启抹掉内存态（这也解释了故障时好时坏、难以复现）。

### A3 — `status=限流` 单向门

额度归零的瞬间打 `限流`：

```python
# services/account_service.py:3517-3519
if not is_true_unlimited and not image_quota_unknown and next_item["quota"] == 0:
    next_item["status"] = "限流"
    next_item["restore_at"] = next_item.get("restore_at") or None
```

而**专门为救这类账号写的**懒刷新逃生口，第一条就要求 status 正常：

```python
# services/account_service.py:389-390
if account.get("status") != "正常":
    return False
```

逃生口被自己关死。三条恢复路径全部不可达：

| 路径 | 位置 | 为何不可达 |
|------|------|-----------|
| 软熔断自愈 | `:511-515` | 条件含 `image_soft_capped` 为真；硬额度路径从不设该标志 → 恒假 |
| 成功生图后自愈 | `:3520-3521` | 需先被派发；派发需 `status == 正常` → **循环依赖** |
| `_normalize_account` | `:1146-1147` | 窗口重置后只写回 quota/restore_at，**不碰 status** |

且 status 经 `_persist_upsert_accounts` 落库，**重启不恢复**。活体已抓到一例：

```
philliphicks336926@outlook.com
status=限流  quota=0
restore_at=2026-07-25T10:30:12Z   (已过期 21.4 小时)
image_soft_capped=None
```

漂移速率：每号一生只需耗尽一次即永久退池。当前 `total_quota = 399` → 约 **399 次成功生图**后全池沉底，且不可逆。

作者在软熔断路径已修对过同一个 bug（`:507` 注释「软熔断只用 flag，禁止改 status=限流」），此处是漏改。

### A4 / A5 — 兜底与监控双双失效

```python
# api/system.py:390 —— 全仓唯一调用方
stats_json["pipeline_watchdog"] = pipeline_watchdog_service.tick(force_release_expired=False)
# services/image_pipeline/pipeline_watchdog.py:51
inflight_drift = account_service.reconcile_inflight(expected_by_token=expected, force=False)
```

两个 force 均硬编码 `False`，而修正分支条件是 `if force and memory > expected`（`account_service.py:2893`）→ **自愈是死代码，只 log 不修**。且 `tick()` **无后台定时器**，寄生在 `/health` 处理器里（`try/except: pass`），没人抓监控就完全不跑。所有槽位注册的 75s deadline 从未被兑现。

A5 让泄漏彻底不可见 —— watchdog 取的 key 不存在：

```python
# services/image_pipeline/pipeline_watchdog.py:53
pools = pipeline.get("pools") if isinstance(pipeline, dict) else {}
```

而 `snapshot()` 把池状态**摊在顶层**（`ps`/`ss`/`upload`/`download`/`in_flight`），无 `"pools"` 层：

```python
# services/image_pipeline/orchestrator.py:80-88
base = pools.snapshot()
base["ready_buffer"] = ready_buffer_tracker.snapshot()
base["segments"] = segments
return base
```

活体实测 `pipeline_pools = null` → `ss_active` / `ss_queued` **恒为 0**。这直接作废了 `27` §4 把 `ss_active/ss_queued` 列为横评指标的前提。

---

## 3. B 类详情（择要）

### B1 — 75s sS 墙钟丢弃已生成的图

`image_ss_stage_wall_timeout_secs` 默认 75（`config.py:98`；`config.json` 未覆盖），而它包裹的合法轮询预算是 120/300/360s — **内层比外层短 1.6~4.8 倍**，严重违反嵌套纪律：

```python
# services/image_pipeline/orchestrator.py:186-192
def assert_ss_wall_ok(self, *, image_index: int) -> None:
    started = self._ss_started_at.get(image_index)
    if started is None:
        return
    limit = config.image_ss_stage_wall_timeout_secs      # 75.0
    if time.monotonic() - started > limit:
        raise TimeoutError(f"sS stage wall timeout ({limit:.0f}s)")
```

唯一检查点是 `conversation.py:1901`，位于 SSE 分片循环内；而成功结果在**图片下载完之后**才 yield（`conversation.py:1525`）。时序：

1. `acquire_ss` 置 `_ss_started_at[index]`（t=0，`orchestrator.py:349`）。
2. 计时器只由 `on_sediment_captured`（`:272`）或 `release_ss`（`:367`）清除。SSE 仅携带 file_ids 而无 sediment 时**永不清除** — `conversation.py:1368-1369` 的注释自述这正是上游常态。
3. 轮询（120/300/360s）+ URL resolve + 下载 + 回传窗口全程带着计时器跑。
4. 成功结果 yield 的那一刻检查 → 早已超 75s → 抛 `TimeoutError`。
5. 不匹配任何 transient 谓词（`conversation.py:304-320`）→ 落 `except Exception` → `conversation.py:2201` 转 `ImageGenerationError(..., conversation_id="")`，**conversation_id 被显式清空**。
6. `image_task_service.py:1668` 的 `timeout_pending` 续轮询分支要求 `conversation_id` 非空 → 短路 → 直落 `TASK_STATUS_ERROR`。

**结果：上游已生成、已计费、已下载的图被丢弃，用户拿到硬错误，账号被记失败，且无任何后台恢复。**

设计意图错位说明：`plan.md` SLO 表把「sS 假超时 225s → 75s 墙钟」当作改进项，目标是让**失败**更快失败；但 75s 同时落在**成功**路径的正常区间内（EWMA 初值即 60s），于是把成功一并杀掉。

### B2 — 重试永久泄漏 sS 槽

`_ss_released_indices` 是**只增不减**的集合（全部引用：`:156` init、`:262` 检查、`:271` add、`:354` 检查、`:366` add；无 `discard`/`clear`）。`acquire_ss` 不检查它，`release_ss` 检查并早退：

```python
# services/image_pipeline/orchestrator.py:353-354
def release_ss(self, *, image_index: int, slot: int | None = None) -> None:
    if image_index in self._ss_released_indices:
        return          # ← 第 2 次及以后的释放被吞掉
```

`conversation.py:1814` 的 `_generate_single_image` 是 `while True:` 重试循环，循环体内 7 处 `continue`（1986/2035/2084/2129/2131/2164/2191），Python 语义下 `continue` 先执行 `finally`（`:2206-2209`）。于是：

1. 第 1 次尝试 → `finally` 释放 → index 入集合。
2. 第 2 次尝试 → `acquire_ss` 再次拿槽（不查集合）。
3. 本次 `finally` → 早退 → **槽与 ledger 条目双双永久泄漏**。

`MAX_POLL_TIMEOUT_RETRIES=4`、`MAX_TEXT_REPLY_RETRIES=3`、`pre_conversation_max_attempts=4`，单任务即可泄漏多个。`sse_slots=10`，累计约 10 次重试后池枯竭；而 `orchestrator.py:338` 的 `self._pools.ss.acquire(holder)` **不传 timeout**，退化为 `Event.wait()` 无限阻塞 → 后续所有生图请求**永久挂死**。配合 A5，全过程监控不可见。

同类问题：`orchestrator.py:290/306/378` 的 upload/pS/download acquire 同样不传 timeout。`pools.py:104-119` 的 `try_acquire_immediate` 名不副实（拿不到槽时无限阻塞），当前无调用者。

### B3 — Python fallback ledger 自死锁（潜伏）

```python
# services/image_pipeline/slot_ledger.py:29
self._lock = threading.Lock()        # 非可重入
# :93-104
with self._lock:
    if force_release_expired:
        ...
        if self.release_account(key):    # :60 内部再 `with self._lock` → 自死锁
        ...
        if self.release_ss(key):         # :82 同上
```

死在持锁状态 → 之后所有 `try_acquire_*` / `release_*` / `stats` 永久阻塞 → 整条生图管线 wedge。

当前被两个条件掩盖：Rust backend 生效（Panda 上 `native/libimage_schedule_core.so` 存在，`isc_slot_ledger_*` 符号齐全）；`force_release_expired` 恒为 `False`。**但 `.so` 加载失败是静默降级（`:179-181`），且"打开 force"正是修 A4 最自然的改动 —— 两个掩盖条件会同时消失。修 A4 前必须先修本条。**

### B5 — 有效单账号并发是 1 不是 2

```python
# services/account_service.py:695-701
binding_limit = self._image_binding_inflight_max()      # 1
current = int(self._image_inflight.get(token, 0))
if current >= max_concurrency:                          # 1 >= 2 → 放行
    return False
binding = self._account_binding_hash(account)
if binding and self._binding_image_inflight_locked(binding) >= binding_limit:
    return False                                        # 1 >= 1 → 拒绝
```

`_binding_image_inflight_locked` 把账号**自己**的在途计入自己的 binding 总数，而 `image_binding_inflight_max = 1`（`config.json:354`）。实测 19 号分布在 16 个 binding 上、**无空 binding** → 闸门对所有账号生效 → **单账号有效并发恒为 1**，`image_account_concurrency = 2` 永远吃不到。

---

## 4. "4 并发显示 60s，实际 120s" 的三重成因

三个子代理各自给出不同根因，交叉核对后确认**三者都真实且叠加**：

| 成因 | 机制 | 贡献 |
|------|------|------|
| EWMA 冷启动 | `_SUCCESS_DURATION_EWMA_INITIAL_SECS = 60.0`（`image_task_service.py:33`）；4 并发时 `batches = ceil(4/10) = 1` → ETA 直接等于 60 | 报数偏低 |
| 口径不一致 | `duration_ms` 自 worker 开跑起算（`:1650`），不含 queued 空转；而 `image_next_ok_ts` 在**任务完成后**才打戳（`mark_image_result` → `_stamp_image_next_ok:518`），每号完成后停 ≈71s | 墙钟 ≈2.4x |
| 供给虚高 | 估算按 `running_slots = min(10, 10) = 10`；实际 `dispatchable = 7` 且单号并发 1（B5），可达并发仅 3.2~4.4 | 再压 2~3x |

冷却期算术：`compute_submit_gap_seconds` = `60 × U(0.65,1.45) + Exp(mean 8)`，期望 **71s**。单账号服务周期 `T_period = T_exec + 71`；7 个号时可达并发 = `7T/(T+71)`：

| T_exec | 实际并发 | 广告并发 | 倍数 |
|--------|----------|----------|------|
| 60s | 3.2 | 10 | 3.1x |
| 120s | 4.4 | 10 | 2.3x |

与 commit `7088649` 的 2x 观测吻合。`21-image-scheduling-and-pipeline.md:40` 已独立记录同一结论（「完成态显示 50–60s，用户墙钟感知约 120s（排队+执行口径不一致）」），`:945` 已标 P0。

**注**：冷却受限吞吐（18 号 / 121s ≈ 8.9 img/min）比并发上限（10 / 50s = 12 img/min）**更紧** —— 池子不是瓶颈，"完成后才计时"的冷却才是真天花板。把 stamp 移到开跑时可立即 +35%。

---

## 5. 死配置清单

运维以为在生效、实际无人读取或被静默覆盖：

| key | config.json 值 | 实际 |
|-----|----------------|------|
| `image_generation_poll_timeout_secs` | 300.0 | **被降为 120**（属性算 `max(120, queue.generation=120)`，从不读顶层键；`config.py:1467`） |
| `image_timeout_retry_secs` | 10 | 全仓零引用 |
| `image_task_queue.download_workers` | 4 | 零消费者（真实下载并发是 `image_pipeline.download_concurrency = 8`） |
| `workload.text_queue_mode` | off | 零消费者 |
| `per_user_running_max` / `_base` / `_burst` | 10 | 被 `relaxed_per_user_running` 提前 return 覆盖为 `sse_slots`（`image_task_service.py:1095`） |
| `image_global_queue_timeout_secs` | 0.0 | 语义是**立即拒绝**而非无限等待；且队列任务全部走 `skip_global_limit` 绕过 |
| `image_global_concurrency` | 10 | 是**下限**不是上限（`max(floor, ready_count)`，`account_service.py:570`）；UI 会显示 18 |
| `image_edit_poll_timeout_secs` | 300 | 顶层键不被读；与 `queue.edit` 值巧合相同故暂不可见 |
| `image_multi_reference_poll_timeout_secs` | 360 | 同上，与 `queue.multi_reference` 巧合相同 |
| `image_sse_post_ready_timeout_secs` | null | = `None` = **valve 关闭**（`config.py:1506`），与 `27` §1 标注的 ✅ 矛盾 |
| `burst_enabled` 及 `burst_min_*` | False | `burst_enabled=False` 时整条 burst 路径为死代码 |

另有未配置但实际生效的隐藏默认：

| 隐藏 key | 默认 | 影响 |
|----------|------|------|
| `image_poll_max_upstream_gets` | **24** | 24 个 conversation GET × 3s ≈ 82~120s 墙钟，使 300/360s 超时**永不可达**（B6） |
| `image_poll_early_sse_initial_wait_secs` | 25 | SSE <5s 完成时首轮等待 25s |
| `image_pipeline.ss_stage_wall_timeout_secs` | 75 | 即 B1 的元凶 |

---

## 6. 文档纠偏

本次审计推翻了以下既有声明。**原文保留，已就地加纠偏批注**。

| 文档 | 原声明 | 实际 |
|------|--------|------|
| `27` §1 矩阵 | sS 槽「**75s 墙钟** ✅」+「看门狗 ✅ SlotLedger」 | 墙钟生效但**方向有害**（B1）；forced release 从未执行（A4） |
| `27` §1 矩阵 | `image_inflight`「✅ reconcile」 | `force=False` → 只报不修（A4） |
| `27` §1 矩阵 | SSE post-ready「**75s** ✅ 已有 soft valve」 | 活体 `image_sse_post_ready_timeout_secs = null` → valve **关闭** |
| `27` §4 横评 | `ss_active` / `ss_queued` 作为 sS 瓶颈指标 | 恒为 0（A5），指标无效 |
| `plan.md` Phase 1 | `[x] sS 75s 墙钟` | 已实现但需**重定阈值**，当前会丢图（B1） |
| `plan.md` Phase 1 | `[x] pipeline_watchdog` / `[x] reconcile_inflight()` | 代码在位但**惰性**（A4） |
| `04` backlog | 「已完成：3. `pipeline_watchdog` + `reconcile_inflight()` + `/health` 扩展」 | 同上 |

---

## 7. 已排除的假设（勿重复调查）

为避免后续重复投入，以下常见怀疑已确证**不成立**：

| 假设 | 结论 |
|------|------|
| 选号存在 check-then-act 竞态 | **不成立**。`_acquire_next_candidate_token:2237-2264` 在同一次 `with self._image_slot_condition` 内完成挑号与占位，中间无 await/无释锁 |
| 单事件循环上的 await 交织点 | **不成立**。整条生图管线零 async（`grep async def\|await` 于 `image_pipeline/` 无命中）；入口经 `run_in_threadpool` + `threading.Thread` 卸载 |
| 准入热路径有阻塞 SQLite 写落在 event loop | **不成立**。`api/image_tasks.py` 与 `api/ai.py` 全部 `run_in_threadpool` |
| 额度乐观预扣后失败未归还 | **不成立**。仅 success 分支扣减（`:3512-3516`） |
| token 轮换丢失在途计数 | **不成立**。`:1430-1432` 已正确迁移 |
| `image_account_concurrency=2` 压制 global=10 | **方向相反**，真正更紧的是 `image_binding_inflight_max=1`（B5） |
| `prompt_dedup_max_parallel=4` 合并并发请求 | **不成立**，它是**拒绝**（抛 `ImageTaskDuplicatePromptError`），不折叠 |
| `auto_scale_global_concurrency` 会 ratchet 到 1 | **不成立**。`max(floor=10, ready)` 保证不低于 10；但它是下限语义（见 §5） |
| `proxy_binding_max_accounts=2` 淘汰账号 | Panda 实测淘汰 **0** 个（16 binding / 最大共享 2） |
| 时区/UTC 错配导致夜间窗口偏移 | **不成立**。全部用 `datetime.now(timezone.utc)` + 显式 `ZoneInfo`；宿主 `Asia/Shanghai` 与调度 `Asia/Singapore` 同为 UTC+8，NTP 已同步 |
| commit「串行文档」= 代码变串行 | **误读**。`git show --stat 7088649` 为 32 文件纯文档批次，指"把串行问题写成文档" |

---

## 8. 其他次级发现（MEDIUM / LOW，未展开）

| 级别 | 问题 | 位置 |
|------|------|------|
| MEDIUM | `image_tasks.db` 397MB 仅 1 行；freelist 96542/97051 页（99.48% 空洞），`auto_vacuum=NONE` 且无 VACUUM 调用 | `_init_db_locked:2204` |
| MEDIUM | 整个 task（含 base64 图）每步重写 ~2MB blob；`compact_task_heavy_fields` 只清内存不回写 | `:226,255,2351` |
| MEDIUM | `wait_for_result` 每 1.5s 持全局锁跑 DELETE+commit；`_cleanup_locked` 返回 True 时逐 task 新开连接落库 | `:760-773,2369` |
| MEDIUM | ACI 排序被 round-robin 作废：三套排序互相覆盖后由 `_index % len(tokens)` 取任意位置 | `:2015,2021,2045,2261` |
| MEDIUM | `sort_tokens_by_aci` 返回 `[]` 会静默清零非空候选（锁外 TOCTOU），伪造 `no available image quota` | `:2017-2023` |
| MEDIUM | pre-ticket 与账号资格零关联，可对已失效号兑票且**上游调用后**才失败；`evict_expired()` 无调度（活体 `total_entries:18 / cached:0` 全泄漏） | `pre_ticket_pool.py:41,81` |
| MEDIUM | ready_buffer 无 TTL；恢复判定只看 bytes 使 item 级背压失效 | `ready_buffer.py:47,62` |
| MEDIUM | 代理隔离**永久**：note 无时间戳/无 TTL，`clear_gpt_unavailable` 零调用方 | `proxy_quarantine.py:163,176` |
| MEDIUM | cohort 暂停 2 次 terminal 即 24h 且 `_cohort_terminal_hits` **永不衰减**；`cohort_id` 当前未赋值故惰性，一旦赋值即全池 kill switch | `account_service.py:456-469` |
| MEDIUM | `_warm_account_lease_pool_locked` 名带 `_locked` 却在未持锁时被调用，裸迭代 `self._tasks.values()` | 定义 `:1142`，未持锁调用 `:1255` |
| MEDIUM | `_pools_locked` 配置变更时整体替换，旧 pool 在飞 run 把槽还给废弃对象，新 pool `_in_flight` 从 0 起算 | `orchestrator.py:54-67` |
| MEDIUM | hard timeout 抛弃 daemon 线程，孤儿线程完成后无 generation guard 可覆写新尝试的终态 | `image_task_service.py:1510,1871` |
| MEDIUM | `timeout_pending` 在队列暂停时永不终态化（唯一终态化者是被暂停移除的 poll worker） | `:1052-1055` |
| MEDIUM | ACI 小样本偏置：`success=1,fail=0` 得 +10，压过 `success=100,fail=5` 的 +8.1 | `aci_ranker.py:61-66` |
| MEDIUM | `newapi_image_sync_admission_max_eta_secs=180` 与客户端 540s 等待预算 3x 错配，过度拒绝 | `api/ai.py:94` |
| LOW | `aci_score` 的 NaN 被 `max(0,min(100,NaN))` 夹成 **100（最优）** 而非 0，fail-open 方向错 | `aci_ranker.py:83` |
| LOW | `pick_token` / `apply_explore_success_bonus` / `invalidate` / `hold_ss_slot` / `_force_release_image_slots` 均为死代码 | 多处 |
| LOW | `pop_hint` 在 `prefer` 不在队列时返回**无关账号**，且 `hint_email` 优先级高于显式入参 | `account_lease_pool.py:139` |
| LOW | `text_nurture._hour_key()` 用 `gmtime`（UTC 小时）而 `_daily_key` 用 SGT 日期，预算重置边界混用时区 | `text_nurture_service.py:216` |
| LOW | `_effective_image_global_concurrency` 使有超时保护的 global-limit 分支不可达，兜底 `wait(1.0)` 无 deadline 检查 | `account_service.py:2265` |

---

## 9. 待办

见 [`04-improvement-backlog.md`](04-improvement-backlog.md) §「AUDIT-28 调度/队列/槽位审计整治」。
