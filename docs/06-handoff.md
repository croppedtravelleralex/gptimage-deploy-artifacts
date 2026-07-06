# 交接摘要

日期：2026-07-04

## 当前结论

- 额度误判根因：旧逻辑把 `image_quota_unknown` 当成 `∞`；已修正为「真无限额 / 未知额度 / 数值额度」三态。
- **本地与生产均已部署**（2026-06-29，`ssh panda`）。
- 规范文档：`docs/quota-semantics.md`；生产运维：`docs/deployment.md` Panda 节。
- 账号池与生图性能升级已完成文档落地：根目录 `plan.md` 为执行主计划；`docs/07-account-pool-performance-upgrade.md`、`docs/sync-strategy.md`、`docs/performance-acceptance-test-plan.md` 分别是落地设计、同步策略和多轮验收计划。
- 账号池性能升级目前状态：**生图 P6 已部署到生产 Panda 并完成 12 任务受控验收；账号池 SQLite / 同步入口 / b64 窗口仍未完成**。
- 生图并发方案已升级为 99+ 本地落地版：异步任务 SQLite 中央队列、poll timeout 不重开图、CPU 1.5 vCPU 预算、90% deadlock_guard 熔断。

## 本轮已经确认的事实

### 代码

- `services/account_service.py`：`_is_true_unlimited_image_account()`、`_is_unknown_image_quota_account()`、统计拆分
- `services/account_refresh_all_service.py`：慢刷 `unlimited_quota` / `unknown_quota` 分开计数
- `api/system.py`：健康页「真无限额」「未知额度」
- `web/src/app/accounts/page.tsx`：三态展示与汇总卡片

### 生产验收（2026-06-29 部署后）

- 公网 `https://gptimage.relai.asia/health?format=json`：`healthy=true`
- `unknown_quota_count` 字段已出现在 JSON 响应中
- 旧误报 `unlimited_quota_count=1`（实为 1 个 unknown 非 Pro 账号）已消除
- 备份：`/root/gptimage/backups/quota-fix-20260629-235620/`

### 测试

- `test_account_image_capabilities.py`：20 passed
- `test_account_refresh_all_service.py`：10 passed
- 2026-07-04 生图 P6 本地落地受影响测试集合：71 passed
- 2026-07-04 生图 P6 Panda 生产部署：备份 `/root/gptimage/backups/p6-image-queue-20260704-175026/`，回滚脚本同目录 `ROLLBACK.sh`
- 2026-07-04 Panda 受控压测：12 个 async image task 全部成功；提交 p95 约 448ms；生成完成耗时 min 38s / p50 202.5s / p95 295s / max 304s；严格错误日志命中 0
- 2026-07-04 R5.6 Stage A：本地经公网提交 18 个混合输入 async task，18/18 success；Panda CPU p95 13.78%、内存 max 526.6MiB、总带宽 p95 14.56Mbps；但公网提交 p95 49.32s、状态查询 p95 4.88s，严格门槛下不进入 24
- 2026-07-04 R5.6 Stage B：用户确认不再用大参考图 submit p95<1s 阻断后执行 24；结果 22/24 入队，2 个 429（`image task queue is full for current user (20/20)`），入队后 21 success / 1 error（`ConfigStore.proxy_url`），Panda CPU/内存安全但带宽 p95 22.31Mbps；不进入 30

### 2026-07-04 生图 P6 本地落地

- `services/image_task_service.py`：异步图片任务主状态迁到 `data/image_tasks.db`，后台中央队列按 worker 消费，不再提交一个任务开一个线程。
- `services/protocol/conversation.py`：带 `conversation_id` 的 poll timeout 转 `image_timeout_pending`，避免重复上游提交。
- `services/image_deadlock_guard_service.py`：按 1.5 vCPU 预算采样 CPU，达到熔断条件后供队列/maintenance 降载。
- `api/image_tasks.py`：队列满或熔断返回 `429 + Retry-After`。
- `api/app.py` / `services/register_service.py`：修复注册服务 import 即 auto-start 的副作用；只有 FastAPI lifespan 进入时才按 enabled 恢复。
- `services/backup_service.py`：备份范围加入 `image_tasks.db/-wal/-shm`。

## 继续做什么

1. 账号池性能升级按 `plan.md` 从 P0 开始：先做备份、R0 基线采样，再修 Panda `dictionary changed size during iteration`。
2. 限制 `/api/accounts/import-batch` 高频同步并加入水位限流；不要依赖固定公网 IP allowlist。
3. 定位本地用户口径 `Accounts.js` 的实际读写来源，同时按当前代码事实兼容 `data/accounts.json`。
4. 本地账号池迁移 SQLite 主存储，新号必须经过 `1h/3h/6h` 探测后再上传 Panda。
5. 生图 P6 已上 Panda；下一步如要证明容量提升，执行 R5.5 真实 100 异步任务压测与 CPU deadlock_guard 人工触发验收。
6. R5.6 的 24 档已经暴露 per-user queue cap、`ConfigStore.proxy_url` 和带宽警戒；不要直接进入 30。
7. 下一步先修 `ConfigStore.proxy_url`，再决定是否提高 per-user queue cap 以完整接纳 24/30，或把 429 快拒绝定义为设计内行为。
8. 继续推进未完成的账号池 SQLite、Panda 同步入口保护、Panda 低写放大和 b64 回传窗口。

## 下一位接手先看

1. `docs/README.md`
2. `docs/02-current-state.md`
3. `plan.md`（若继续账号池 / 生图性能升级）
4. `docs/07-account-pool-performance-upgrade.md`
5. `docs/sync-strategy.md`
6. `docs/performance-acceptance-test-plan.md`
7. `docs/quota-semantics.md`（若动额度 / 健康页 / 账号展示）
8. `docs/05-ai-maintenance-playbook.md`
9. `docs/logs/2026/2026-07.md`（最近变更）

## 2026-07-04 追加交接：P6-12 已部署，24 档新瓶颈转为上传链路

### 已完成

- Panda 生产已备份并部署：`/root/gptimage/backups/p6-queue36-status-20260704-210512/`。
- 已修 `ConfigStore.proxy_url`。
- `per_user_queue_max` 已提高到 `36`，但 `submit_workers=6`、`per_user_running_max=2` 保持不变。
- 新增轻量接口：`GET /api/image-tasks/status?ids=...`，已验证不返回 `data` / `payload`。
- 本地相关回归：21 passed。

### 新验证事实

- Stage C 24 复测报告：
  - 本地：`reports/loadtest-20260704-211253-stage-24/summary-partial.json`
  - Panda：`/root/gptimage/backups/loadtest-20260704-211253-stage-24/summary-partial.json`
- 结果：24 请求中 19 入队，5 个公网大参考图上传阶段连接失败；入队任务 18 success / 1 error。
- 5 个未入队错误：`ConnectionResetError(10054)` / `RemoteDisconnected`。
- 入队错误：`/backend-api/files failed: status=500, body=`。

### 当前判断

- 旧的 `20/20` 429 和 `proxy_url` bug 已解决。
- 24 档仍不通过，原因不是 worker、CPU、内存，而是单阶段大参考图上传和上游文件上传链路。
- 不进入 30。

### 下一步优先级

1. 做 IMG-005：multipart 上传 / 两阶段 reference asset upload / 上传窗口保护。
2. 复测 24 时必须拆分三类指标：上传成功率、入队成功率、生成成功率。
3. 不要通过输入减重换数字；参考图质量保持不变。

## 2026-07-05 追加交接：3轮 24 并发压测

### 已完成

- 连续执行 3 轮 24 async image task 公网压测。
- 聚合报告：`reports/stage24-3rounds-20260705/aggregate-summary.json`。
- Panda 聚合报告：`/root/gptimage/backups/stage24-3rounds-20260705/aggregate-summary.json`。

### 关键结果

```text
72/72 submit HTTP 200 queued
66/72 final success
6/72 final error
submit p95≈21.07s
status p95≈2.76s
cpu p95 max≈13.7%
memory max≈724MiB
bandwidth_total p95 max≈9.2Mbps
```

### 新发现

- 当前 24 档上传/入队这三轮稳定，未复现上一轮 5 个入队前连接失败。
- Panda 资源不是瓶颈。
- 失败分两类：
  1. 2 个上游 HTTP/2 INTERNAL_ERROR，无 conversation_id，失败释放接近 30 分钟。
  2. 4 个 timeout_pending 续轮询实现 bug：`OpenAIBackendAPI.__init__() got an unexpected keyword argument 'proxy_url'`。

### 本地已修但未部署

- `services/image_task_service.py::_run_resume_poll()` 已改为 `OpenAIBackendAPI()`。
- 新增测试覆盖续轮询不再传 `proxy_url` kwarg。
- 验证：`test_image_task_service.py test_image_tasks_api.py` 共 `14 passed`。

### 下一位先做

1. 若继续压测/优化，先备份并部署 BUG-002 到 Panda。
2. 再做 IMG-005：reference asset 两阶段上传 + 上传窗口。
3. 同时做 IMG-006：pre-conversation HTTP/2 长尾失败快收敛。
4. 不要直接进入 30；当前 24 的最终成功率仍未稳定超过 95%。

## 2026-07-05 追加交接：IMG-005 一期已部署，24 仍卡在生成侧

### 已完成

- BUG-002 已部署 Panda：`/root/gptimage/backups/bug002-resume-poll-20260705-162726/`。
- IMG-005 一期已部署 Panda：`/root/gptimage/backups/img005-assets-phase1-20260705-163634/`。
- 新增 reference asset API：
  - `POST /api/image-assets/references`
  - `GET /api/image-assets/references/{asset_id}/status`
  - `DELETE /api/image-assets/references/{asset_id}`
  - `POST /api/image-tasks/edits` 支持 `asset_ids[]`
- 最小真实验收：公网 asset 上传成功，`asset_ids` edit task `queued -> success`，duration≈44.8s。
- 已执行 IMG-005 两阶段 3 轮 24 压测。

### 新报告

```text
本地 corrected：reports/img005-stage24-3rounds-20260705-164948/aggregate-summary-corrected.json
Panda corrected：/root/gptimage/backups/img005-stage24-3rounds-20260705-164948/aggregate-summary-corrected.json
清理残留备份：/root/gptimage/backups/img005-cleanup-unfinished-20260705-190020/
```

### 关键结论

```text
asset_upload=48/48
submit=72/72
submit p95 max≈2.31s
status p95 max≈2.35s
cpu p95 max≈8.28%
memory max≈750.6MiB
bandwidth_total p95 max≈6.30Mbps
45min cutoff：56 success / 9 error / 7 unfinished
```

- IMG-005 一期有效：上传/入队已解耦，提交体降到约 `300~418B`。
- 24 仍不合格：生成侧长尾和账号失效严重，不能进 30。
- 自然错误分布：
  - 4 个 pre-conversation `HTTP/2 INTERNAL_ERROR`，无 `conversation_id`。
  - 4 个 post-conversation `token invalidated during image poll task check`，已有 `conversation_id`。
  - 1 个 `no available image quota (tried 8 tokens)`。
- Round3 cutoff 后有压测残留任务；已备份 DB 并只把本次未完成测试任务标为 error，重启后 `active_all=[]`，生产 health 正常。

### 下一步先做

1. IMG-006：pre-conversation 硬超时/有限重试/账号短 backoff，避免失败占槽 30min。
2. IMG-007：post-conversation poll timeout 和续轮询策略，当前 120s 对 24 并发复杂图偏短。
3. IMG-005 二期：上传窗口保护、asset TTL/清理、前端上传/入队状态拆分。
4. 补 Panda 号池或等账号质量恢复后再复测 24；`102 total / 470 quota` 纸面足够 72，但实际风险是坏号、preflight、backoff、候选尝试上限和 timeout_pending 占槽。

## 2026-07-05 追加交接：IMG-006 / IMG-007 / IMG-005 二期已部署，24 失败主因转为候选面过窄

### 已完成

- 生产备份：`/root/gptimage/backups/img006-007-005p2-20260705-194447/`，含 `ROLLBACK.sh`。
- 已部署：
  - `services/image_asset_service.py`
  - `api/image_assets.py`
  - `services/config.py`
  - `services/protocol/conversation.py`
  - `services/protocol/openai_v1_image_generations.py`
  - `services/protocol/openai_v1_image_edit.py`
  - `services/openai_backend_api.py`
  - `services/image_task_service.py`
  - `api/image_tasks.py`
- 本地验证：目标测试 `34 passed`，扩展回归 `93 passed`，py_compile 通过。
- 生产验证：health 正常，配置生效，异常日志 0。

### 新报告

```text
本地：reports/img006-007-005p2-stage24-3rounds-combined-20260705/aggregate-summary.json
Panda：/root/gptimage/backups/img006-007-005p2-stage24-3rounds-combined-20260705/aggregate-summary.json
```

### 关键结果

```text
requested=72
asset_upload=48/48
submit=72/72
final=45 success / 26 error / 1 unfinished
asset_upload_p95_ms_max≈19078
submit_p95_ms_max≈2626
status_query_p95_ms_max≈3638
cpu_p95_pct_max≈15.0
memory_max≈875MiB
bandwidth_total_p95_mbps_max≈11.1
strict_bad_count=0
```

错误分布：

```text
23 x no available image quota (tried 8 tokens)
2 x image poll timeout 300s
1 x image poll timeout 360s
```

### 重要修正

- `102 total / 470 quota` 纸面上足够 72；不能简单叫“烧穿”。
- 失败主因是实际可调度面，不是总 quota：生产配置 `image_token_max_attempts=8`，每任务最多抽 8 个 token；抽到的 8 个 preflight/限流/不可调度就直接失败。
- 当前 24 的上传/入队已稳定；不要再把问题归因到上传链路。

### 下一位先做

1. 不进 30。
2. 做配置 A/B：`image_token_max_attempts 8 -> 24/32`，先只跑单轮 24。
3. 观察 `no available image quota`、preflight 请求量、账号限流增量和资源开销。
4. 只有 no-available 明显下降后，再试 `per_user_running_max 2 -> 3`；否则加 running 只会更快失败。


## 2026-07-06 追加交接：BUG-003 已修，IMG-008 第一档有效

### 已完成

- 修复 ImageTaskService import 副作用：只读导入不再恢复/改写未完成任务；runtime recovery 移到 start_background() / worker 启动前。
- 本地验证：	est_image_task_service.py test_image_tasks_api.py 共 19 passed。
- Panda 部署备份：/root/gptimage/backups/bug003-image-task-import-side-effect-20260705-235012/。
- 生产 image_token_max_attempts 已从 8 调到 24，配置备份 /root/gptimage/backups/img008-token-attempts24-20260705-223751/。
- IMG-008 单轮 24 完整报告：
eports/img005-stage24-1rounds-20260705-235210/aggregate-summary.json；Panda 同步在 /root/gptimage/backups/img005-stage24-1rounds-20260705-235210/。

### 关键结论

`	ext
asset_upload=16/16
submit=24/24
final=23 success / 1 error / 0 unfinished
no available image quota=0
CPU p95=8.69%
memory max=710.3MiB
bandwidth_total p95=18.09Mbps
strict_bad_count=0
`

唯一失败是 1 个文生图 300s 超时；不是候选 token 不足。

### 下一步

1. 不直接进 30。
2. 保持 image_token_max_attempts=24，暂不升 32。
3. 补号/水位恢复后，再单轮 A/B per_user_running_max 2 -> 3，观察 24 档完成时间、timeout_pending 和账号池消耗。
4. 继续查状态查询 p95 约 3s，以及为什么单轮 24 后 active/quota 明显下降。

## 2026-07-06 追加交接：补号后不具备继续压测条件

### 已完成

- 尝试 per_user_running_max=3 单轮 24 A/B。
- 结果 24/24 error，全部
o available image quota。
- 已回滚到默认 per_user_running_max=2，保留 image_token_max_attempts=24。
- 已部署 maintenance 生图期间 slow 配置：slow_when_image_inflight=1、slow_batch_limit=5、slow_delay_between_accounts_sec=8。

### 关键结论

- 这次失败不是 CPU/内存/带宽，也不能证明 running=3 一定不可行；压测被“补入假活号 + maintenance 正在批量清死号”污染。
- maintenance 连续批次真实可用率接近 0：80/0 available、80/1 available、slow 后 5/0 available。
- 单任务 smoke 也失败：
o available image quota (tried 24 tokens)。

### 下一步

1. 暂停大压测。
2. 先补入高置信账号，或等待 maintenance 清完并确认真实 available 明显恢复。
3. 做 IMG-009：health 增加 preflight backoff / 真实候选数，否则 health 的 schedulable 会虚高。
4. 只有单任务 smoke 成功、maintenance 最近批次 available 率恢复后，才重新跑 24。

## 2026-07-06 追加事实：IMG-009 health 真实候选指标已部署并完成 24 异步压测

状态：**Panda 已部署；health JSON/HTML 已验收；24 异步压测完成但严格 24/24 未完全通过**。

已部署：

- `services/account_service.py`：`get_stats()` 新增运行态生图候选指标：
  - `preflight_backoff_count`
  - `ready_candidate_count`
  - `available_candidate_count`
  - `dispatchable_candidate_count`
  - `image_inflight_count`
  - `image_account_concurrency_limit`
  - `image_global_concurrency_limit`
  - `image_global_queue_timeout_secs`
  - `image_global_limit_reached`
- `api/system.py`：health HTML 卡片新增“预检退避 / 退避后候选 / 当前可派发 / 生图占用/全局上限”。

本地验证：

```text
python -m py_compile services/account_service.py api/system.py
python -m pytest test/test_account_image_capabilities.py -q
# 23 passed
python -m pytest test/test_image_task_service.py test/test_image_tasks_api.py -q
# 19 passed
```

生产备份：

```text
/root/gptimage/backups/img009-health-runtime-candidates-20260706-093545/
/root/gptimage/backups/img009-health-runtime-candidates-20260706-093545/ROLLBACK.sh
```

生产验收：

```text
syntax_compile_ok
/health?format=json 新字段 missing=[]
HTML cards ok
最终 health：schedulable=142, preflight_backoff_count=0, ready_candidate_count=142, available_candidate_count=142, dispatchable_candidate_count=142, image_inflight_count=0
同步兼容入口仍为 image_global_concurrency_limit=6, image_global_queue_timeout_secs=0.0
```

24 异步两阶段真实混合输入压测：

```text
本地报告：reports/img005-stage24-1rounds-20260706-094205/aggregate-summary.json
Panda 报告：/root/gptimage/backups/img005-stage24-1rounds-20260706-094205/aggregate-summary.json
输入：8 generation + 16 edit；10 单参考 + 6 双参考；参考图总量 30.28MB；未输入减重。
asset_upload=16/16
submit=23/24
accepted final=23 success / 0 error / 0 timeout_pending / 0 unfinished
strict_bad_count_60m=0
CPU p95=9.468%, max=14.42%
内存 max=628.1MiB
总带宽 p95=10.643Mbps, max=17.575Mbps
status query p95=3499ms
```

严格判断：

- 账号候选面已不再虚高：压测后 `dispatchable_candidate_count=142`，`preflight_backoff_count=0`。
- Panda 资源不是本轮瓶颈；入队成功的 23 个任务全部成功。
- 本轮严格 24/24 未通过：1 个 generation 在公网提交阶段 TLS handshake 120s 超时，未进入 Panda 任务库。
- 同步 OpenAI/NewAPI 兼容入口仍是 `6 + 0s queue_timeout`，超过 6 会立即触发 `image service busy`；这与异步 `/api/image-tasks` 24 入队是两条链路，不应直接把同步入口拉到 24。
- 下一步若要减少截图里的同步入口 502，应单独 A/B：先把 `image_global_queue_timeout_secs` 调到 `8~10s`，再小步测试 `image_global_concurrency 6 -> 8`；不建议公共同步入口做无限队列。

## 2026-07-06 13:55 +08 IMG-011 交接

- Panda 已部署 services/image_task_service.py hard timeout，备份 /root/gptimage/backups/img011-image-task-hard-timeout-20260706-124825/。
- 本地 scripts/img005_asset_stage_loadgen.py 已加入 asset upload window/retry，submit/status 用 HTTP/2 keepalive。
- 最新有效 3×24 报告：
eports/img005-stage24-3rounds-20260706-125316/aggregate-summary.json。
- 结果：70/72 success，2 个 generation 因 510s no conversation_id hard timeout，0 unfinished。
- 压测后 Panda：healthy，active 空，image_inflight=0，dispatchable≈186。
- 下一步若继续提并发：不要先上 30；先解决 pre-conversation 无 conversation_id 的根因，推荐进程级可 kill 上游调用或降低 pre-conversation timeout A/B。

## 2026-07-06 IMG-011 详细数据补录
- 已把 3轮24详细数据和后台运行并发 4/6/8/10 预测写入 docs/02-current-state.md。
- 推荐顺序：NewAPI 兼容入口内部异步队列化后，先 running=4 A/B，再 running=6；不要直接 8/10。

## 2026-07-06 IMG-012 交接：NewAPI sync-over-async 与 6/burst8 流水线方案

状态（16:55 更新）：**已本地实现 + Panda 已部署 + 16:41 restart**；busy_6=0；24 路 5/24 成功。

### 已实现

```text
IMG-012A~C：config / wait_for_result / api/ai.py sync-over-async / image_sync_adapter
queue_coordinated → skip_global_limit：解除同步入口 global 6 快拒
Panda 备份：/root/gptimage/backups/img012-sync-over-async-20260706-*
Panda config：per_user_running_max=6, burst_enabled=false
压测脚本：scripts/img012_newapi_sync_loadgen.py（IMG012_TARGET=newapi）
```

### 压测结论（2026-07-06 16:42）

```text
报告：reports/img012-newapi-sync-stage24-1rounds-20260706-164210/
busy_6：0 ✅
成功：5/24（文生图 60–111s）
失败：19/24 — NewAPI HTTP/2 断连与空响应，非 Panda busy
```

### 未做

```text
burst 8（burst_enabled=false）
3 轮 72 路
stream=true 入口改造
NewAPI 网关稳定性
IMG-012E 完整下载/回传窗口拆分
```

### 下一位接手

```text
1. 读 docs/08-image-pipeline-newapi-async-plan.md §13
2. 排查 NewAPI 24 长连接断连（HTTP/2 / 外层超时）
3. 重跑 NewAPI 24 压测，目标 ≥23/24 + busy_6=0
4. 通过后 scripts/img012_enable_burst_deploy.py
5. 3 轮 72 路验收
```

**关键教训**：部署 Python 代码后必须 `docker compose restart`；`up -d` 不 reload 进程。16:18 部署、12:51 启动的容器在 restart 前仍报 busy_6。

---

## 2026-07-06 IMG-012 交接（方案阶段，已被上文覆盖）

状态：**仅文档化为待办**（方案制定时记录，实施见上文 16:55 更新）。

新增文档：

```text
docs/08-image-pipeline-newapi-async-plan.md
```

背景事实（方案制定时）：

- IMG-011 最新 3×24 异步压测：`72 submit / 70 success / 2 hard-timeout / 0 unfinished`。
- 当前 `/api/image-tasks` 能稳定接 24，但实际单用户 running≈2，clean 轮 24 完成约 12.4 分钟。
- NewAPI 仍走 `/v1/images/*` 同步入口，超过全局 6 并发会出现 `image service busy: global concurrency limit 6 reached`，NewAPI 侧表现为 502/503。

## 2026-07-06 追加交接：IMG-012 asset pointer 后 24 同步仍卡 NewAPI/Cloudflare

### 已完成

- Panda 已部署 asset pointer 兼容层：NewAPI 必填的 `image` 文件可用 `panda-asset://<asset_id>` text/plain 小文件替代。
- `asset_ids` 字段直传不可行：NewAPI 会先拒绝 `image is required`。
- NewAPI smoke 1/1 成功；6 路 6/6；12 路 12/12；busy_6 全为 0。
- 24 HTTP/2：客户端 17/24，Panda 入库 17 且 17/17 success。
- 24 HTTP/1.1：客户端 14/24，失败为 Cloudflare 524；Panda 入库 24，最终 23 success / 1 error。
- 已部署 `resume_polling` hard-timeout 止血，最终 health：healthy=true、image_inflight_count=0。

### 当前判断

- Panda 生成能力不是本轮 24 同步失败主因。
- 失败主因是 NewAPI/closeapi/Cloudflare 同步长连接 175~210s 超时/断流。
- `/v1/images/*` 标准同步入口不适合承载 24/30 长等待；必须改成 NewAPI 侧异步 task/callback，或同步入口做短等待和 admission 控制。

### 下一位先做

1. 不要继续盲跑 24/30 标准同步压测。
2. 若用户要 24/30 体验，优先设计 NewAPI -> Panda `/api/image-tasks` 异步适配，而不是继续拉 Panda worker。
3. 若必须走标准同步，先调整 closeapi/NewAPI/Cloudflare timeout；否则 Panda 再优化也会 524。

## 2026-07-06 IMG-013 handoff

- 当前代码：`/v1/images/generations` 支持 prompt tunnel：`panda-async:` 异步提交，`panda-task://<task_id>` 通过 NewAPI 查询结果；`/v1/images/edits` 支持 form `panda_async=true` 和 prompt tunnel。
- 生产已部署，备份 `/root/gptimage/backups/img013-newapi-async-tunnel-20260706-203319/`。
- 生产配置已开启保守 burst8：base=6、burst=8、dispatchable>=120、backoff=0、per_user_queue_max=48。
- NewAPI stage6 报告：`reports/img013-newapi-async-stage6-1rounds-20260706-205344/`；6/6 submit_ok，5/6 success，1 个 generation pre-conversation hard-timeout。
- 收尾状态：Panda healthy=true，image_inflight_count=0。
- 不要直接继续 24/30/36，先处理 IMG-014 hard-timeout/slot 自愈；否则大压测会被上游尾流污染。
