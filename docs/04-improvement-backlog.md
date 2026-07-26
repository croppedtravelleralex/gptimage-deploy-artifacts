# 改进池

最后校准：2026-07-26（夜）

原则：只保留当前仍有工程价值的项；已完成和历史流水不放在这里，详见 `docs/logs/2026/2026-07.md` 与 `docs/archive/`。

## PROV-29 溯源修复【P0 · 新开 2026-07-26 夜】

真相源：[`29-prod-provenance-audit-20260726.md`](./29-prod-provenance-audit-20260726.md)

**必须在任何新部署之前做完 P29-1**，否则新一轮 `git` 操作可能静默回滚 AUDIT-28。

| ID | 事项 | 位置 | 审计项 |
|----|------|------|--------|
| P29-1 | Panda 上把 28 个 staged 文件落成 commit（纯本地 `git commit`，不推不重启） | `/root/gptimage` | `29` §2 |
| P29-2 | 本地 22 个 commit + 16 个漂移文件提交并推送到**真正的源码远端**（`deploy/main` 只有 README，不是源码仓库） | 本地仓库 | `29` §3.1 |
| P29-3 | `services/yumail_otp.py` 补发（已提交未上线，prod 缺西语 OTP 关键词） | `services/yumail_otp.py` | `29` §3.3 |
| P29-4 | `services/register/domain_intel.py` 定性：补提交或删除（505 行孤儿死代码，本地不存在） | Panda 独有 | `29` §3.2 |
| P29-5 | A1-6 补完：`resolve_binding_matrix` hash fallback 限定「含周末档」预设子集，或 `slot_allowed` 加连续全关降级放行 | `ip_nurture_schedule.py:270` | `29` §5 |
| P29-6 | `.gitignore` 补 `web_dist*.tgz` / `web_dist.backup-*` / `.artifact-*/` / `.deploy-*/` / `crates/*/target/`；清理 36 个 backup 目录 | 仓库根 | `29` §8 |
| P29-7 | 13 个 `scripts/_tmp_deploy_*.py` 的 `scp` 段改造为 git ref checkout（保留健康检查/备份/smoke 逻辑） | `scripts/` | `29` §11 |
| P29-8 | `image_tasks.db` 397MB/0 行离线 VACUUM（磁盘已 79%） | Panda `data/` | `29` §4 |
| P29-9 | 前端重构决策：提交上线 or 明确搁置（prod `web_dist` 停在 07-24） | `web/` | `29` §8 |

## AUDIT-28 调度/队列/槽位审计整治【P0 · 新开 2026-07-26】

真相源：[`28-scheduling-queue-slot-audit-20260726.md`](./28-scheduling-queue-slot-audit-20260726.md)（Panda 生产代码只读审计，6 子代理分域 + 逐条复核）

> **2026-07-26 夜补注**：批次 0–4 代码已上线 Panda 且运行时确认生效（`29` §1.1），但**零生图流量验证** —— B1/B2/B4/B9 仍只有静态确证 + 单测背书。conc10 复测是 THROUGHPUT-10 的前置。

**执行纪律**：批次内按序做，跨批次不得跳。**批次 0 必须最先** —— 打开 watchdog 的 force 会同时引爆 `_PySlotLedger` 自死锁。每批次落地后跑 conc10 + `capture_performance_baseline.py` 再进下一批。

### 批次 0 — 解除踩雷前置（必须最先）

| ID | 事项 | 位置 | 审计项 |
|----|------|------|--------|
| A0-1 | `_PySlotLedger._lock` 改 `RLock()`，或 `watchdog_tick` 内改调不加锁的私有版 | `slot_ledger.py:29,93` | B3 |
| A0-2 | `.so` 加载失败改为**显式告警**，不再静默降级到 Python | `slot_ledger.py:179-181` | B3 |

### 批次 1 — 止血：停止丢图 / 停止丢工作项

| ID | 事项 | 位置 | 审计项 |
|----|------|------|--------|
| A1-1 | sS 墙钟阈值改为 `> max(poll_timeout) + margin`，或改为**仅覆盖 SSE 阶段**（sediment 前）而非全程 | `orchestrator.py:190` | B1 |
| A1-2 | `TimeoutError` 转 `ImageGenerationError` 时**保留 `conversation_id`**，让续轮询可接管 | `conversation.py:2201` | B1 |
| A1-3 | `release_ss` 早退前先归还池槽；或 `acquire_ss` 重试时从 `_ss_released_indices` 摘除 index | `orchestrator.py:354` | B2 |
| A1-4 | 4 处 `acquire`（ss/ps/upload/download）全部传 `timeout`，消除无界 wedge | `orchestrator.py:290,306,338,378` | B2 |
| A1-5 | text_nurture 出队改**非破坏性**：校验通过再 commit，失败 requeue + 退避 + 死信 | `text_nurture_service.py:378` | A1 |
| A1-6 | 周末预设补齐：`business_hours`/`extended_business` 增加周末档位，或闸门改 `>=` | `ip_nurture_schedule.py:83-85,189` | A1 |

### 批次 2 — 恢复供给

| ID | 事项 | 位置 | 审计项 |
|----|------|------|--------|
| A2-1 | `cf_block_sec` 从 86400 降至分钟量级；`_cf_fail_streak` 加滑动窗口衰减 | `account_warmup_service.py:351,336` | A2 |
| A2-2 | 已封号加**周期性重探**（当前封后不再探活 → 永不解封） | `account_warmup_service.py:320-322` | A2 |
| A2-3 | 补人工解封 API + UI（当前只有只读 status） | `api/ops.py:93-96` | A2 |
| A2-4 | `quota==0` 不再改 `status=限流`，改设 `image_soft_capped`（与 `:507` 既有结论一致，此处是漏改） | `account_service.py:3517` | A3 |
| A2-5 | 存量已沉底账号一次性回捞（`status=限流` 且 `restore_at` 已过） | 运维脚本 | A3 |

### 批次 3 — 让兜底与监控真正生效（依赖批次 0）

| ID | 事项 | 位置 | 审计项 |
|----|------|------|--------|
| A3-1 | `tick(force_release_expired=True)`；`reconcile_inflight(force=True)` | `api/system.py:390`、`pipeline_watchdog.py:51` | A4 |
| A3-2 | watchdog 挪到**独立后台线程**，不再寄生 `/health` | `pipeline_watchdog.py` | A4 |
| A3-3 | 修 `pipeline.get("pools")` → 顶层取 `ss`，恢复 `ss_active`/`ss_queued` | `pipeline_watchdog.py:53` | A5 |
| A3-4 | `reconcile` 前排除 resume-polling 任务（当前结构性虚高 → 打开 force 会误伤） | `pipeline_watchdog.py:32` | A4 |
| A3-5 | `PipelineRun.finish()` 用 `try/finally` 包住；`begin_run` 的 `admit()` 后加异常回滚 | `orchestrator.py:100,403` | B9 |
| A3-6 | submit/poll worker 循环体加 `try/except`，异常落 ERROR 而非杀线程；补 RUNNING 超时 reaper | `image_task_service.py:1217` | B10 |
| A3-7 | `release_image_slot` 加 ownership token / 幂等 key，消除 SSE-release 与 `mark_image_result` 双释放 | `orchestrator.py:201`、`account_service.py:3502` | B4 |

### 批次 4 — 吞吐与口径【P1】

| ID | 事项 | 位置 | 审计项 |
|----|------|------|--------|
| A4-1 | `_stamp_image_next_ok` 从"完成后"移到"开跑时"（或 `T_start + gap`）— **单改此项 +35% 吞吐** | `account_service.py:3522` | §4 |
| A4-2 | ETA 改用 admission→delivery 全墙钟；`running_slots` 纳入 `binding_inflight_max` 与冷却周期 | `image_task_service.py:700-729` | §4 |
| A4-3 | `image_binding_inflight_max` 语义修正（当前把账号自身计入自身 binding → 单号并发恒为 1） | `account_service.py:695-701` | B5 |
| A4-4 | `image_poll_max_upstream_gets`（隐藏默认 24）显式化并对齐 300/360s 预算；超时报错指向正确 key | `image_poll_budget.py:82`、`openai_backend_api.py:3707` | B6 |
| A4-5 | resume 阶梯总预算收敛到 < 客户端 540s；`resume_deadline_ts` 改为**运行中**也检查 | `image_task_service.py:1787` | B7 |
| A4-6 | deadlock guard 死区修复：`65~90%` 区间也复位 `_above_since`；CPU 改读 cgroup v2 | `image_deadlock_guard_service.py:65,44` | B8 |
| A4-7 | `per_user_running` 与 `submit_workers` 解耦（当前同为 10 = 无跨用户隔离） | `image_task_service.py:1095` | §3 |

### 批次 5 — 死配置清理与可观测【P1/P2】

| ID | 事项 | 审计项 |
|----|------|--------|
| A5-1 | 死配置一次性处理：`image_timeout_retry_secs`、`download_workers`、`workload.text_queue_mode`、`burst_*`、三个顶层 `*_poll_timeout_secs`（**保留键但标注失效，或接上消费者**；不静默删除） | §5 |
| A5-2 | `image_generation_poll_timeout_secs=300` 被静默降为 120 — 修属性读取顺序或 UI 标灰 | §5 |
| A5-3 | `image_global_concurrency` 下限语义 vs UI 显示不一致（配 10 显示 18）— 统一口径 | §5 |
| A5-4 | 广告 vs 实际供给对齐：`get_stats` / `get_schedulable_breakdown` / `build_snapshot` 均需扣 warmup 封禁并加 warmup 桶 | A2 |
| A5-5 | `image_tasks.db` 397MB/1 行治理：base64 拆列或落文件；`auto_vacuum=INCREMENTAL` 或离线 VACUUM | §8 |
| A5-6 | `_cleanup_locked` 节流（当前挂在 `wait_for_result` 每 1.5s 一轮，持全局锁跑写事务） | §8 |
| A5-7 | pre_ticket / ready_buffer / proxy_quarantine / cohort 四处缺 TTL 或缺衰减 — 逐项补 | §8 |
| A5-8 | `schedule_trace` 补 `ss_wall_timeout` / `inflight_drift` / `ledger_forced_release` 事件（`27` §3 待做项） | `27` §3 |

### 不做 / 暂缓

- ACI ranker 重做（当前被 round-robin 作废，属白做功但无害）— 等 A4-3 落地后再评估是否保留 ACI。
- Layer 2 账号/基础设施 Rust — 触发条件未到，见下文 Layer 2 条目。
- **`cohort_id` 赋值** — 在 `_cohort_terminal_hits` 加衰减之前**不要赋值**，否则 2 次事件即全池 24h kill switch（`28` §8）。

## 当前主线（2026-07-25 起）

### CF 准入与隔离纪律【P0 · 2026-07-26 已修】

- **事故**：`webshare_cf_scan` 批量 `cf403_scan` 隔离整池 → 生图可用 **0**（`proxy_cf_ok` 打标仍 true）。
- **已落地**：`proxy_cf_eligibility` 缓存优先；`webshare_cf_scan` 跳过已绑定 endpoint；`_tmp_recover_cf_quarantine.py`。
- **运维**：号池看 **进调度 vs 生图可用** 用 `schedulable-breakdown`；详见 `17` §「批量 scan 隔离」。

### SLOT-RUST Layer 1 — SlotLedger + sS 75s 超时【P0 · 进行中】

- **真相源**：[`26-slot-lifecycle-rust-roadmap.md`](./26-slot-lifecycle-rust-roadmap.md) §4–§5、[`27-pipeline-watchdog-monitoring-matrix.md`](./27-pipeline-watchdog-monitoring-matrix.md)
- ⚠️ **2026-07-26 纠偏**：下列"已完成"指**代码已就位**，经 `28` 号审计确证**运行时未生效或方向有害**。整治项见本文 §AUDIT-28。
- **已完成（2026-07-25）**：
  1. Rust `SlotLedger` FSM + watchdog FFI；Python `slot_ledger.py` fallback。
     — ⚠️ Python fallback 的 `watchdog_tick` **非可重入锁自死锁**（`28` B3）；`.so` 加载失败静默降级。
  2. sS 75s 墙钟：`ss_stage_wall_timeout_secs` + `assert_ss_wall_ok`。
     — ⚠️ **方向有害**：比所包裹的 120/300/360s 合法轮询短 1.6~4.8 倍，会丢弃已生成的图（`28` B1）。
  3. `pipeline_watchdog` + `reconcile_inflight()` + `/health` 扩展。
     — ⚠️ 两个 `force` 硬编码 `False` → **只报不修**；无后台定时器（寄生 `/health`）；`ss_active/queued` 取错 key 恒为 0（`28` A4/A5）。
  4. `pre_ticket_pool` TTL 缓存 requirements。
     — ⚠️ 与账号资格零关联，可对已失效号兑票；`evict_expired()` 无调度（活体 18 条全泄漏，`28` §8）。
  5. 基线脚本 + `BASELINE-pre-slotledger-*`。
     — ⚠️ 其中 `ss_active`/`ss_queued` 两项恒为 0，横评前需先修 A3-3。
- **待办**：
  1. Linux `.so` artifact 部署 Panda；FFI 全面持账。
  2. `failure_retry_enabled` / `failure_retry_max` API+UI。
  3. Layer 1 完成后 conc10 复测 + `BASELINE-post-slotledger-*` 横评。

### SLOT-RUST Layer 2 — 账号/基础设施 Rust【P2 · 号池扩大后】

- **触发**：号池 **N>50** 或 `image_global_concurrency>20` 或 account_queue 占比持续 >10%。
- **范围**：`account_service` CRUD/缓存、CF 探活编排、proxy 绑定 — **非当前 sprint**。
- **不做**：一夜全量 Rust conversation/SSE。

### IMG-STABLE 稳住出图【P0 · 运维观察】

- **状态**：可观测性 sprint 已收尾；转入 **稳住出图** 阶段。
- **真相源**：根目录 [`plan.md`](../plan.md)（STAB 执行清单）；详情 [`archive/plans/24-image-stability-plan-detail.md`](./archive/plans/24-image-stability-plan-detail.md)
- **SLO**：单账号 serial5/conc10 历史 10/10；**2026-07-25 回归 4/10**（CF 出口，换绑后待复测）；inflight 泄漏已修。
- **本周 P0**：STAB-A1/A2（公平复测+超时）、STAB-B1/B4/B5（CF 探活、binding 并发、暖号池）。
- **已锁定不做**：真 Chrome 开票、浏览器生图数据面、FlareSolverr 生图根方案、大规模协议挖矿。
- **Camoufox**：注册/重登 + SDK 漂移补 HAR；非生产生图栈。
- **验收关闭**：见 `24` §5（IMG-STABLE v1）。

## 上一主线（2026-07-24，已收尾）

### IMG-OBS 生图可观测性与三路径对比【已关闭可观测性；对比 ~80%】

- **状态**：后端+日志 UI **已部署 Panda**；VERIFY-001 pass；pure_http 5/5 + ticket_pool 3/5 首份对比报告已出。
- **真相源**：[`docs/23-image-observability-and-benchmark-todo.md`](./23-image-observability-and-benchmark-todo.md)
- **已完成（摘要）**：
  1. call log 阶段耗时 + token/t/s + traffic；成功路径单条合并；去掉 LoggedCall 双写。
  2. 日志页 DurationCell / 去重 / 200 页 / Token·流量。
  3. VERIFY-001：`verify-001-20260724T083730Z.json`
  4. BENCH-001～003：`R-image-path-benchmark-20260724.md`
- **待办（P1）**：**BENCH-004** browser 对照；ticket_pool 公平复测（号闲后单独 serial5，超时对齐 540s）。
- **架构**：真 Chrome 出票 **不纳入主线**（见 `22` §9、`06` 架构结论表）。
- **验收**：见 `23` §验收标准。

## 当前主线（2026-07-22 起优先）

### PROTO-PURE-HTTP 严格纯 HTTP 生图（Sentinel / Turnstile）【P0 · 正式链路验收中】

- **状态**：**P4-7 已落地**；串行 5 **5/5**；单账号 conc10 **30/30**（`acceptance-90s-picture_v2-20260723`）；多账号轮询 conc10 **4/10**（`acceptance-90s-multiacct-20260723`，冷号突发 CF @ init，非链路回退）。观测/流水线 → **IMG-SCHED-021**。
- **详细清单（已做/未做勾选）**：[`docs/20-pure-http-image-sentinel-todo.md`](./20-pure-http-image-sentinel-todo.md)（**真相源**）。
- **依据**：`captures/spa/G-image-gen-not-triggered-20260721.md`、`field-diff-picture_v2-live-vs-bench.json`、现网 HAR `spa-image-20260721T144019Z.har`；`12`「生图不触发 image_gen」。
- **要做（按序）**：
  1. 保持当前生产版本与单单元证据，不自动换号或追加 canary。
  2. **P0 / deadline 边界**：bench 读取 SSE 时每行先解析，再检查 45 秒 deadline，避免临界工具事件被直接丢弃。
  3. **P0 / 脱敏时间线**：保存 `arrival_ms`、`author/name/recipient`、`content_type`、`event/type/status`；禁止保存 token、cookie 或完整敏感正文。
  4. **P0 / 结果分类**：新增 `tool_args_as_text`、`late_image_gen_after_gate`、`no_image_gen_quiet_stream`，不再把所有失败压成 `no_image_gen`。
  5. **P0 / CF 分层**：拆分 `home_403_soft_fail`、`requirements_cf403`、`start_cf403`、`tasks_cf403`、`propagated_cf`；修正文档和 M 证据中“无任何 CF403”的误导口径。
  6. **P1 / 诊断监听**：gate fail 后同一 SSE 流可继续只读监听至 **90s+**；**验收 gate 临时放宽至 90s**（2026-07-23：`qaflowakjewai6ps` 在 65s gate 下 `image_gen` 68–74s，与 `/v1/images` ~72s 成功不一致；先以 90s 过验收，**上游 SSE 触发延迟优化**见 backlog **PROTO-UPSTREAM-LATENCY**）。
  7. **P4-6 / CF 节点预扫**：5 并发 Webshare 轻量探测（home+requirements），记录各节点 CF403；编排见 `scripts/spa_image_panda_acceptance.py`。
  8. 保留新 IP 绑定；观测代码已落地，可在 Panda 重跑串行 5 / cf_scan5 / 并发 4。
  9. 长期降低 `/tasks` 轮询暴露面，保持 conversation poll 恢复与连续 2 次 CF abort 边界。
- **已完成（摘要）**：G 诊断；Turnstile VM；strict auto-tool；CF/poll abort；正式发布；生产单单元；旧 IP 串行门禁止损；同账号/同 fp/同 shape 新旧 IP A/B；新 IP 生产换绑与串行 `4/5` 止损。证据 `captures/spa/{J,K,L,M}-*.json`。
- **非目标 / 禁止**：浏览器作数据面或暖机拿 Turnstile；宣称协议绕过 CF；Panda build/scp 当正式发布；空 Turnstile 软降级回生产。
- **验收**：见 `20` §4（纯 HTTP + 非空 Turnstile + 有 `image_gen` + 出图 + Panda 串行/并发）。

### PROTO-UPSTREAM-LATENCY 上游 SSE 触发生图延迟优化【P1 · 待办】

- **状态**：**待办**（2026-07-23 验收暴露：同账号 `qaflowakjewai6ps@proton.me` / `92.113.246.176`，`spa_tool` 首次 `image_gen` ~74s、`picture_v2` ~68s，均超 65s gate；`/v1/images` 同环境 ~72s 可成功）。
- **现象**：SSE 建连后前几百 ms 仅有元数据/`tool_recipient`，随后 **60–70s 空窗**，再出现 `title_generation` + `image_gen`；非 CF403，requirements/prepare 正常。
- **目标**：将 **p50 `sse_image_gen_ms` 压到 ≤45s**（长期），验收 gate 可从 90s 收回 65s。
- **要做**：
  1. 按协议路径（`picture_v2` vs `spa_tool`）对比 `sse_event_timeline`，定位空窗发生在上游哪一阶段（排队/模型路由/工具调度）。
  2. 与生产 `/v1/images`、pipeline `phase_timings` 对齐，确认是否可跳过冗余 prepare/文本 preamble。
  3. 账号/IP 维度：慢账号是否与高 CF IP 共现；是否需要换绑 vs 纯上游抖动。
  4. 监控：在 Ops/health 增加 `image_gen_ttft_p50/p95`（从 conversation start 到首个 `image_gen`）。
- **证据**：`captures/spa/acceptance-20260723T065722Z.json`、`acceptance-picture_v2-20260723T070500Z/`。
- **非目标**：单纯放宽 gate 当作优化；用浏览器暖机换延迟。

### PROTO-REFACTOR 按逆向结果改造生产路径

- **状态**：**待办**（挖矿 Now/Next 已完成；改造未开工）。**生图触发/Sentinel 修复优先走 PROTO-PURE-HTTP**，本项不挡 P2，但上传链仍要做。
- **依据**：`docs/19-protocol-full-reverse-catalog.md` 看板 + `docs/captures/spa/` 专页 + `12`。
- **要改（按优先级）**：
  1. **上传链对齐 SPA**：`POST /files` → `PUT oaiusercontent…/raw` → `process_upload_stream`（替换/并存旧 `/files/{id}/uploaded`）；聊天附图默认 `sediment://`（对照 `D-upload-sediment-20260721.md`）。
  2. **生图**：默认采用已证严格 auto-tool（`image_spa_tool_path=true`、无 conduit）；显式 `false` 仅回退 `picture_v2` canary，禁止部署时静默反转。**正式发布与下载验收 → PROTO-PURE-HTTP**。
  3. **搜索**：生产继续 `system_hints:["search"]`（HTTP 已证）；SPA「Look something up」UI HAR 可选补证据，不挡改造。
  4. **观测**：错误分类挂钩 `F-errors-20260721.md`（CF HTML / TLS / 422 / 栈差）；不宣称绕过 CF。
- **非目标**：协议绕过 CF；买「神代理」当成功条件；冷门工具/全站埋点。
- **验收**：
  - 上传单测 + Clash/Webshare smoke：附件进 conversation 的 pointer 与 SPA 同族（sediment 或文档声明的并存策略）。
  - 文本/生图回归不退化；`CHANGELOG` + `12` 差距表更新。
  - `image_spa_tool_path` 默认/回退语义必须由单测固定，禁止部署时悄然反转。
- **禁止**：无 HAR/专页依据改请求体；Panda build/scp。

### PROTO-ALIGN 文本协议对齐 canary

- **状态**：代码已落地（2026-07-19）；**SPA HAR/HTTP 挖矿 2026-07-21 已完成**（见 `19`/`captures/spa/`）。后续改造并入 **PROTO-REFACTOR**。
- **已实现**：
  1. 鉴权文本/生图 `timezone` + `OAI-Language` 跟 sticky egress（默认 SG）；anon 仍 LA。
  2. `text_chat_persist_history` / 账号 `chat_persist_history` → `history_and_training_disabled=false`；`text_chat_reuse_conversation` + `text_conversation_id`/`text_parent_message_id` 真续聊；禁止复用生图 cid。
  3. `client_contextual_info` 按账号种子轻度抖动；文本 `/f/conversation`+prepare、Client Version/Build、Prepare-Token。
- **仍缺**：→ 见 **PROTO-REFACTOR**（上传链等）。
- **验收**：单号设 `chat_persist_history=true` + `chat_reuse_conversation=true` 后文本可检索历史；时区与 egress 一致。
- **禁止**：机械假聊；批量改现网号历史开关无 canary。

### SCHED-001 账号调度可观测性与失败归因

- **状态**：观测 API 已落地（2026-07-19）；不重写调度。
- **已实现**：
  1. `GET /api/accounts/schedulable-breakdown`：`excluded_by_*` buckets + `primary_reason_counts` + runtime。
  2. `no available image quota` 附带轻量 `schedulable_breakdown`（429 detail / openai error）。
- **仍缺**：`classify_error_blob` 统一入口；`diff_inflight_vs_tasks`。
- **验收**：health/管理接口能解释可调度数；0 候选时主因桶明确。管理 UI 图表见 RISK-VIZ。

### LLM-OPS 操作日志与 L2 只读 facade

- **状态**：已落地（2026-07-19）。
- **已实现**：`llm_ops` 日志；日志页 type=`llm_ops` + source/outcome 筛选；`GET/POST /api/ops/*` 只读 tool facade；运维页 `/ops`；确定性 RCA playbook。
- **验收**：日志可筛 L0/L2/ai_review；`/ops` 可跑空池 RCA。

### TEXT-NURTURE 真实文本队列（非假聊）

- **状态**：已落地（默认 OFF）。
- **已实现**：`text_nurture_service` + Qtext 入队/worker；拒绝生图指令；要求 `chat_persist_history`；API `/api/ops/nurture/*`；UI 开关与入队。
- **红线**：禁止「每 N 图插一句」；独立文本 cid。

### RISK-VIZ 拟人/防封/管道全量可视化

- **状态**：已落地（2026-07-19）；巡检默认 OFF。
- **已实现**：
  1. `risk_metrics` 半小时点 + 日历热力；`humanlike-dashboard` / `risk-calendar` / `risk-checks` API。
  2. `/ops`「风控拟人」Tab：曲线、日历、admission/queue/burst/poll/streak/cohort/llm_ops/health 漏斗、养号水位、巡检时间线。
  3. 半小时 DeepSeek→L0 GPT 主动巡检（`risk_audit.enabled`）；账号页 soft/maturity/cohort/流量/FP。
  4. Canvas 样式案例 `humanlike-risk-viz.canvas.tsx`。
- **仍缺**：`request_shape` 正式时序图（仅钩子）；上线后开 `risk_audit` 并配 NewAPI DeepSeek。
- **验收**：`/ops` 风控 Tab 可见补齐面板；日历/曲线有采样点；enabled 后约 30min 有报告。
- **禁止**：宣称降封百分比；巡检自动改 receive_state / 删号。

### PROTO-ALIGN HAR canary

- **状态**：真实 SPA HAR 已落盘（`docs/captures/spa/`，2026-07-21）；对照脚本仍可用。改造跟踪 **PROTO-REFACTOR**。
- **脚本**：`scripts/proto_align_har_canary.py`（可 `--enable-persist` 开单号历史）。

### STORE-004 / LOG-ROT 存储归档（优先于语言重写）

- **状态**：P0 运维已完成（2026-07-19）；S1/L1 工程仍待。方案 `15-store-archive-and-log-rotation.md`。
- **P0 结果**：`image_tasks.db` 1575MiB→~0.02MiB；`logs.jsonl`→热空+`logs.jsonl.20260719-211833.gz`(20MiB)；备份 `store-p0-20260719-211735`。
- **仍待工程**：成功任务不落 b64；清理后 auto-vacuum；`LogService` 自动轮转 API。
- **验收**：见 `15` §4。

### RUST-001 Rust 网关热路径重写（后续）

- **状态**：Phase A+ 已接线；**R0 Python 开票+生图 2026-07-23 验证通过**（`22` §6）。
- **前置**：STORE-004 / LOG-ROT（`15`）完成后再开 MVP 实打上游。
- **路线**：MVP 最小生文/生图 → Panda 隔离端口测通（`self=0`）→ 全量（选号/admission + **RCA/运维指标**）。
- **永久非目标**：注册机；FlareSolverr/全局 clearance（生产保持 `clearance.enabled=false`）。
- **收益预期**：见 `13`（含 helper 保守列）；端到端生图不以变快为 KPI。
- **红线**：禁止 Panda `cargo build`；estuary Bearer；禁生产 `8012` 未立项切流。

### GO-SPIKE-001（已搁置 → Rust）

- **状态**：**搁置**（2026-07-23 用户确认用 Rust）；见 `22` §4x 归档。

---

## 既有主线

### PANDA-001 低并发生图恢复观察

- **状态**：进行中；2026-07-16 已修复 hard-timeout 迟到账号分配导致的槽位泄漏。
- **背景**：修复前 `image_inflight_count=26`、任务库真实未完成数明显更低，导致 `dispatchable=0`；热更新并重启后 `image_inflight_count=0 / schedulable=5 / dispatchable=5`。
- **当前参数**：`image_global_concurrency=6`、`image_account_concurrency=1`、`submit_workers=2`、`poll_workers=1`、`per_user_running_base/max=2`、`burst_enabled=true`（soft，`dispatchable>=8`）、`newapi_image_sync_admission_max=12`、`admission_max_eta_secs=180`。
- **验收**：
  - 真实业务不再报 `image_generation_paused`
  - 标准同步路径 **零** 空 `data` / `object=image.task` 假成功 200
  - 过载表现为 429 `image_service_busy`，而不是 canvas「接口没有返回图片」
  - `image_tasks.db` 无 queued/running 残留
  - 最近日志无 Traceback、`timeout_pending` 异常堆积
  - hard timeout 后 `runner_alive_after_cancel=false`；线程数回到基线，代理连接无 `ESTABLISHED/CLOSE_WAIT` 残留
- **禁止项**：观察期不做 24 同步硬压，不因生图失败直接删号。
- **当前删号边界**：refresh-all / maintenance / 生图链路自动删 invalid 均关闭；异常号只做隔离、人工恢复或显式删除。

### ACC-006 Panda 可调度池恢复

- **状态**：进行中；2026-07-19 小号池 `total=4 / schedulable=4`（2 Outlook + 2 Proton）。
- **背景**：2026-07-16 已显式删除 75 条 `account_deactivated` 终态 Outlook；后经 sticky/观察/Proton 补号收口到当前 4。
- **方向**：
  - 从本地 clean ready 池补 Panda。
  - 不允许清失败证据直接恢复 `panda_rejected` / invalid 账号；Outlook 必须换新 token 并通过 Webshare + Panda `/backend-api` 验证后再入池。
  - 补池前检查本地 ready 质量、Panda 水位、最近同步来源和批量上限。
- **当前进展**：
  - 已上传本地 clean ready 账号到 Panda，失败 `0`，pending `0`；最近一轮又补入 `23 + 7` 个可上传号，成功 ACK 后本地删除。
  - 2026-07-08 已备份并删除存量异常号：本地 `4664` 个，Panda `519` 个。
  - Panda 自动删 invalid 已关闭；失败号保留为 rejected，账号页 `RefreshCw` 或 CLI 可执行单号 OTP 恢复。
  - 本地保持 `panda_sync.enabled=true`、`staging_enabled=false`、`remove_local_on_success=true`、`queue_on_failure=false`。
  - 同步 ACK 后本地账号标记 `panda_sync_state=synced`，避免后续重复上传把 Panda 已验证账号打回 `incoming`。
  - 账号页已增加上传/接收状态可视化、手动上传、自动上传开关和注册/上传或接收/删除三色折线图。
  - 最近采样（2026-07-10 18:35）：Panda `total=12`、`schedulable=12`、`dispatchable_candidate_count=12`、`panda_rejected_count=0`、`verified_total_quota=219`。
  - 累计三条 Outlook invalid 已通过独立 OTP 恢复；UI 一键入口、进度轮询、单任务互斥、SQLite 备份和进程内 reload 已部署。
  - `/api/accounts/sync/panda` 已切到受控 `queue_available_accounts_for_panda()`，不再调用旧同步脚本，并返回 details 解释 `synced=0 failed=0 queued=0` 的具体原因。
  - 2026-07-15 深度审计确认：64 个唯一 token 的恢复证据为 `account_deactivated`，同链路另有 11 个唯一 token 成功恢复；终态不是 Outlook/代理/资源故障，不能继续重试或清证据入池。
  - 2026-07-15 20:42 快照：`total=93 / disabled=64 / limited=18 / active=11 / schedulable=0 / dispatchable=0`；18 条限流有未来恢复时间，不能按死号处理。
  - 2026-07-16 清理结果：`75` 条终态 Outlook 删除，`total=18 / disabled=0 / active=18 / schedulable=5 / dispatchable=5`；释放 `10` 个终态账号独占 Webshare 节点，另 `1` 个节点仍被存活账号引用。
- **验收**：
  - Panda `dispatchable_candidate_count` 明显提升
  - `panda_incoming_count` 不持续堆积
  - `panda_rejected_count` 不被误恢复
  - 同步失败仍留本地主池，不进入 pending

### SYNC-004 上传/接收可视化与控制

- **状态**：已完成第一版，继续观察。
- **已完成**：
  - 本地节点口径为“上传”，Panda 节点口径为“接收”。
  - 本地提供手动上传一批和自动上传开关；Panda 节点不显示自动上传开关，只刷新接收状态。
  - `/api/accounts/activity/daily` 返回最近 N 天注册/入库、上传、接收、删除序列。
  - 账号页三色折线图：绿色注册/入库、蓝色上传或接收、红色删除。
  - `/api/accounts/panda-sync` 专用开关只更新 `panda_sync.enabled`，保留密钥，避免 `/api/settings` 脱敏回写风险。
- **后续观察**：
  - 历史删除日志会让 Panda 当日红线很高，这是历史接收后筛号，不等于当前实时异常。
  - 后续如需更精确历史，应引入独立 account_events 表，而不是只从 JSONL 日志回放。

### ACC-007 Outlook 批次注册风险熔断与成熟期

- **状态**：高优先级待实现；下一批 Outlook 到位后先执行单 canary，不直接全量启动。
- **证据**：`shared_stable_warp`  cohort 68 条中 51 条终态，终态率 75%；7 月 14 日约 4 小时内创建 66 条。注册/首登使用共享且非粘性的 `40080`，随后切独享 WARP 验号，再上传 Panda 改走全局 `single_proxy`；51 条终态中 50 条从未成功生图。
- **目标**：把上游批次风险变成系统可观测、可自动止损的边界，不通过换指纹或换出口规避风控。
- **建议实现**：
  1. 新增注册 cohort/job 级统计；滚动窗口内出现 2 条明确 `account_deactivated` 时，自动停止注册、Panda 上传和后续扩容，并要求人工复核。
  2. 新账号执行 `1 个 canary → 1h/6h/24h` 成熟探活；通过后才进入 `2～3` 条的小批次，批次间继续保留成熟窗口，未完成成熟期前不进入 Panda 生产调度。
  3. 记录注册出口哈希与运行环境迁移；发现同一账号在注册/首登/首次验号期间出口漂移时中止，不做自动随机化补偿。
  4. 禁止注册成功后立即跨本地 WARP、独享 WARP、Panda 全局代理多次搬运；合法账号应固定在一个稳定、受支持的运行环境。
  5. UI 显示 cohort 成功、失效、终态、成熟中和熔断原因，不再只显示总成功数。
  6. 为每个账号保存固定 Webshare 节点标识、首次注册时间、首次 token 刷新、首次真实请求和首次 invalid 时间；代理凭据只保存在 secret 存储。
  7. 真实文本与真实生图使用独立速率域，共享账号健康、节点绑定、会话连续性和总 in-flight；真实队列为空时保持空闲。
  8. 逆向链路聚焦 ChatGPT Web 聊天/生图的 canonical request、API/resource Session 隔离、SSE/poll、连接回收、幂等、请求形状遥测和失败归因。
- **验收**：终态达到阈值后不再继续消耗剩余凭据；单 canary 失败不会扩成百号批次；所有扩容都有成熟期证据和明确人工放行。

### ACC-009 Outlook 99+ 长期运行方案落地

- **状态**：方案文档已按 2026-07-17 刷新；工程部分落地（一对一 sticky、fp/egress、成熟度字段透传、观察脚本 `--write-maturity`）。`proxy_nodes` 表与自动成熟期状态机仍待。详见 `09-outlook-longevity-99-plan.md` §2。
- **节点基线**：10 个释放 Webshare 节点经三轮网络与 ChatGPT 入口复测后分为 `READY=5 / OBSERVE=3 / QUARANTINED=2`；当前只允许 READY 节点进入新账号 canary。
- **当前缺口**：节点租约表、cohort 熔断状态机、WorkloadPolicy live、per-account clearance、图片容量保护与三层指标。
- **实施顺序**：
  1. P0：先落 `proxy_nodes`、固定绑定校验、节点分层、单 canary、`1h/6h/24h/72h/7d` 成熟期和 cohort 熔断。
  2. P1：增加 `AccountWorkloadPolicyService`，维护独立文本/生图速率域、文本 lease、图片容量保护、共享健康门禁和结构化指标。
  3. P2：管理页展示节点租约、账号成熟度、熔断原因、剩余预算和生命周期时间线；补齐 7/30 日留存报表。
- **验收**：节点绑定不可静默迁移；新批次按 `1 → 2 → 3` 扩容；任一 terminal/连续鉴权失败均触发 cohort 停止；成熟前不进入生产池；每次状态变化均有审计事件和可回滚备份。

### ACC-010 ChatGPT Web 聊天/生图逆向链路一致性审计

- **状态**：本地核心项已落地；Panda 已具备 fingerprint / workload / builder / poll budget / CF soft-fail；`/me` 轻量头回退与指纹分化已合入本地（待 artifacts 部署）。上线路径：本地验收 → artifacts → Panda overlay；**禁止** Panda build / scp 直推。
- **2026-07-17 证据**：7 号池 fp/egress/binding 齐全一对一；观察号 iv***3 隔离静置；反 bot 深检 `data/runlogs/antibot-deep-check-20260717/`。2026-07-16 22:09「缺 fingerprint / fp0/18」快照已过时。
- **本地已落地**：API/resource Session 隔离；`/me` fail-fast + CF 重试 + 轻头回退；空 conduit 失败；fp ensure/persist + seed 分化；去掉「最新对话」兜底；`request_shape`；身份门禁；`ImagePollBudget`。
- **仍待**：完整阶段耗时链统一字段、生产 live canary/72h、节点一对一租约表、per-account clearance。
- **验收**：跨域敏感头 0；会话串线 0；已有 conversation 的重复生成 0；poll 不越预算；成功/异常/取消后 FD、线程、Session和slot回到基线。

### ACC-008 正常状态但 invalid 证据账号的恢复闭环

- **状态**：本地已修；待 Git 部署到 Panda 后观察。
- **问题**：4 条账号当前为 `status=正常 + quota>0 + verified_ready`，但最新 `/backend-api/me` 已返回 `token invalidated`。调度器正确按失败证据隔离；自动 Outlook 恢复却只选择 `异常/rejected`，导致 `candidate_count=0`，这些账号在二次确认前既不可调度也不进入恢复。
- **已实现**：
  1. 保留 30 秒 invalid 确认窗与 10 分钟新号宽限。
  2. 超过确认窗的「正常+invalid」纳入 `is_outlook_auto_recovery_candidate`。
  3. `test_outlook_auto_recovery_loop` 覆盖确认窗内/窗外行为。
- **验收**：部署后不再出现长期 `schedulable=0` 且恢复 `candidate_count=0`、同时仍有陈旧 invalid 的状态。

### IMG-SCHED-022 双槽流水线 pS(10) + sS(10)【P1 · 设计 v2.3】

- **状态**：**待办**（设计见 `21` §8、§11、**§12 吸收 Orchestrator v2**）。
- **双轨**：Sync Orchestrator（request-scoped，`/v1/images`）；Async Job（持久化，`/api/image-tasks` + `retry_phase_cursor`）。
- **核心改造**：废除「全程 per_user_running 占槽」；分阶段 `AccountProvider`；五段 `*_queue_ms`。
- **API**：`prompt_enhance`（默认 false）、`prompt_enhance_locale`、`multi_image_mode`（fast/diverse）。
- **异号**：首选异号 + 小池回退；**禁止**任务级永久 exclude（§12.4）。
- **验收**：§8.8 + §11.8 + conc10 P50/P95；假 queue 消除（`ss_queue_ms` 与总墙钟可解释）。

### IMG-SCHED-021 生图多阶段流水线与前端提交 P-C【P1 · 设计已记录】

- **状态**：**待办**（设计见 [`docs/21-image-scheduling-and-pipeline.md`](./21-image-scheduling-and-pipeline.md)；现象：UI 4 并发完成态各显示 ~60s，用户墙钟约 120s；一提交即「排队中」）。
- **根因（已确认）**：
  1. 前端 `Promise.all` 洪峰提交 + `concurrencyLimit` 仅展示不限制。
  2. 完成态 `duration_ms` 只计 worker 执行段，不含队列等待。
  3. 后端 `RequestPhaseTracker` 只打日志，未写入 `ImageTask` API。
  4. IMG-012 多阶段队列（SSE / download / return）未完全落地；带宽 EWMA 调速无代码。
- **要做（按优先级）**：
  1. **P0 后端**：`phase_timings_ms` 写入 `ImageTask`（对齐 bench `timings_ms`）；`progress_callback` 可带分段耗时。
  2. **P0 前端**：`ImageJobQueue` 有界 submit pool，替换多图瞬间 POST；完成态展示墙钟总耗时。
  3. **P1 后端**：拆分 `result_download_queue` 与 `image_return_window`；任务状态机细化为 `SSE_STREAMING` / `READY` / `DOWNLOADING` 等。
  4. **P1 后端**：IMG-012 §5.3 带宽令牌桶（Mbps EWMA）准入 download/return。
  5. **P1 前端**：接 `/api/image-tasks/status` 的 `queue_position`；阶段瀑布图（消费 `phase_timings_ms`）。
  6. **P2**：分段 EWMA 估 ETA（SSE / download 分开）；ops 甘特图（可视化，非调度算法）。
- **调度原则（不复述实现细节）**：SSE 占账号槽不占带宽；下载/回传占带宽窗口；文生图跳过 upload 槽；多账号 SSE 并行打关键路径。
- **非目标**：React 侧计量 SSE/上下行；用甘特图当调度器；盲目加 `submit_workers`。
- **验收**：
  - 4 张批量：UI 总墙钟与分段瀑布可解释「~120s」；单张 `duration_ms` 与排队等待可拆分展示。
  - 24 路混合输入：对比单池 `per_user_running` vs 分阶段流水线 p50/p95 与带宽曲线。
  - 文档 `21` 与 `08`（IMG-012）缺口清单一致。

### SCHED-002 调度升档策略

- **状态**：暂缓到 Panda 可调度池恢复后。
- **方向**：
  - 当前低并发稳定后，再讨论 `submit_workers`、`per_user_running_max`、`image_global_concurrency` 和 burst。
  - 升档依据必须是 `dispatchable_candidate_count`、任务成功率、上游 429/timeout、CPU/内存/带宽，而不是账面 `total_quota`。
- **验收**：
  - 单轮升档只改一个变量。
  - 每轮有 before/after health、任务库状态、日志错误、资源采样。
  - 回滚路径明确。

### SEC-003 Cloudflare 邮件 Worker 管理口令轮换

- **状态**：待审计后执行；当前链路已可用，不在本轮直接轮换。
- **当前事实**：`cloudflare_temp_email` Worker 没有 `ADMIN_PASSWORD` secret binding，当前使用源码中的默认管理员口令；本地注册配置已按只写 secret 处理，管理 API 不再回显该值。
- **风险**：源码默认口令可能被历史项目或备份复用；直接轮换可能同时打断其他注册机消费者。
- **建议步骤**：
  1. 全量盘点 `email-api.relai.asia` / `temp-email-api.relai.asia` 的现有消费者。
  2. 在 Worker 中新增 `ADMIN_PASSWORD` secret，而不是继续改源码默认值。
  3. 逐个更新消费者并验证创建邮箱、恢复 JWT、读取邮件。
  4. 确认无旧消费者后撤销旧口令，并轮换本轮使用过的 Cloudflare Global API Key。
- **验收**：Worker 源码不再承载真实口令；所有客户端只写保存；旧口令与 Global API Key 均失效。

## SQLite 后续优化

### STORE-003 SQLite mmap / cache / checkpoint 评估

- **状态**：后续优化方向；当前不启用。
- **当前事实**：
  - 账号库、任务库、参考资源库已启用 WAL / NORMAL / busy_timeout。
  - 未启用 mmap。
  - 未统一配置 `cache_size`、`wal_autocheckpoint`、手动 checkpoint 或 vacuum 策略。
- **可能收益**：
  - mmap：减少大库随机读和 JSON 行读取时的系统调用与拷贝，适合大量读、少量写的 SQLite 场景。
  - `cache_size`：减少重复扫描账号表和任务状态时的磁盘读。
  - checkpoint：控制 WAL 膨胀，避免长时间运行后读写尾延迟增加。
- **可能成本**：
  - mmap 会占用进程虚拟地址空间，并可能增加 page cache 压力；容器内存墙较低时必须小步设置。
  - SQLite mmap 对写入吞吐帮助有限；写瓶颈仍主要受事务、锁、JSON 序列化和索引影响。
  - 配置不当可能把内存压力转化为 OOM 或宿主机页缓存抖动。
- **建议实验**：
  1. 先只读采样：库大小、WAL 大小、top query、状态接口延迟、容器 RSS/page cache。
  2. 本地启用小 mmap，例如 `64MiB` 或 `128MiB`，只跑账号列表、health、任务状态查询压测。
  3. Panda 小流量灰度，观察 RSS、major fault、接口 p95、WAL 大小。
  4. 有收益再固化为配置项；无收益则不启用。
- **验收**：
  - 读接口 p95 下降或 CPU sys 占比下降。
  - RSS 和容器内存没有接近 85% 墙。
  - 写入错误、database locked、WAL 膨胀没有增加。

### STORE-004 image_tasks.db 结果瘦身

- **状态**：高优先级待做；方案并入 `15-store-archive-and-log-rotation.md`（含 VACUUM / 停 b64 / 归档）。
- **背景**：2026-07-19 仍见 ~1.57GB 且 **0 行**（空洞）；终态 b64 历史与未 VACUUM 叠加。历史上全量加载曾 OOM/502。
- **方向**：见 `15` Phase S0–S2。
- **验收**：见 `15` §4；`/api/image-tasks/status` p95 稳定。

### LOG-ROT logs.jsonl 轮转

- **状态**：待做；方案见 `15` Phase L0–L2。
- **方向**：热文件大小/日期切割、gzip 归档、admin rotate API；可选按 type 分文件。
- **验收**：热文件 ≤ 配置上限；查询不随总历史线性变慢。

## 已完成，保留索引

- 额度三态修复：见 `docs/quota-semantics.md`。
- SQLite 主存储与行级 upsert/delete：见 `services/storage/database_storage.py`。
- Panda 同步失败不入 pending：`panda_sync.queue_on_failure=false`。
- 图片任务 SQLite 中央队列、`timeout_pending`、resume poll、hard timeout：见 `services/image_task_service.py`。
- health 运行态候选指标：见 `services/account_service.py` 与 `api/system.py`。
- TempMail.lol exact/root 域名归类器：见 `services/register/domain_intel.py`。
- 2026-07-08 文档主入口清理：`02-current-state.md` 与 `06-handoff.md` 已短版化，旧长文档归档到 `docs/archive/`。
- 2026-07-08 Panda 上传状态可视化：后端 stats 和账号页卡片/表格已落地；同步按钮已改为受控上传入口。
- 2026-07-15 IMG-019：恢复任务不再抬高同步 admission ETA；单次上游会话只回传 1 张图；`skipped_mainline` 支持有限换号重试。生产 Panda/NewAPI 单请求均返回 200、`data_count=1`。

## 暂缓或不做

| 项 | 当前判断 |
| --- | --- |
| 直接恢复高并发生图 | 可调度账号过少，先低并发观察 |
| 清失败证据直接恢复 `panda_rejected` | 不安全；只允许重新登录换 token 并通过 Panda 实测后再入池 |
| 生图链路直接删号 | 仍不做；当前只允许 refresh-all / maintenance 删除 invalid |
| 同步失败进入 pending | 已废弃；失败留本地主池 |
| 直接开启 mmap | 当前不是瓶颈，先写入后续实验 |
| 继续盲跑 TempMail.lol 2000/20 | 429 和 `registration_disallowed` 会吞掉大部分请求 |
