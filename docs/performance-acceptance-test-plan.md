# 多轮测试与验收计划

最后更新：2026-07-04

## 1. 目标

本文件定义账号池、同步、Panda 存储、maintenance、生图和 b64 回传升级后的多轮验收流程。只有完成验收，才能重新评估并发、RPM、CPU 核心和最终瓶颈。

## 2. 统一指标

每轮至少记录：

```text
success_rate
p50 / p95 / p99 latency
Panda CPU avg / p95 / max
Panda memory
Panda BlockIO read/write delta
NewAPI CPU / memory
NewAPI 400 / 429 / 500 / 502 / 503
Panda no available image quota
Panda dictionary changed size during iteration
Panda image service busy
Panda image_poll_timeout
b64 broken pipe
import-batch 次数/小时
accounts 存储写入次数/大小
maintenance 单号耗时 p50/p95
image_task queued/running/polling/timeout_pending/success/failed
重复上游提交率 duplicate_submit_rate
CPU deadlock_guard 触发次数
worker 当前值/升降档事件
event loop 或 health check 延迟
```

## 3. R0 基线复测

执行时机：任何代码改动前。

目标：拿到对比基线。

动作：

- 采集当前 Panda/NewAPI 资源。
- 统计最近 1h/2h import-batch 次数。
- 统计 NewAPI 图片请求状态码和耗时。
- 统计 Panda BlockIO。

验收产出：

```text
baseline-YYYYMMDD-HHMM.json / md
```

## 4. R1 单元与回归测试

执行时机：每个代码切片完成后。

建议命令：

```bash
python -m pytest test/test_account_image_capabilities.py -q
python -m pytest test/test_account_refresh_all_service.py -q
python -m pytest test/test_account_maintenance_loop_service.py -q
python -m pytest test/test_json_storage.py -q
python -m pytest test/test_image_storage_service.py test/test_multi_image_results.py -q
```

前端涉及 UI / 配置时：

```bash
cd web
npm run build
```

通过标准：

- 相关测试全绿。
- 不因存储迁移破坏旧 JSON 兼容测试。

## 5. R2 本地 SQLite 迁移验收

执行时机：本地账号池 SQLite 主存储完成后。

测试点：

1. `data/accounts.json` / `Accounts.js` 导入 SQLite。
2. 账号数量一致。
3. token_hash 唯一。
4. 随机抽样 20 个账号 raw_json 字段一致。
5. 注册新号写 SQLite。
6. 三次探测状态写 SQLite。
7. 死号标记不会高频写文件。
8. 上传成功后批量更新 sync state。
9. 低频导出快照可被旧脚本读取。

通过标准：

```text
账号数一致
关键字段一致
文件写入频率下降
功能路径不丢
```

## 6. R3 Panda 同步入口保护验收

执行时机：Nginx / 应用层 import-batch 保护完成后。

测试点：

- 正常合法请求 2xx。
- 高频请求 429 + Retry-After。
- nonce 重放拒绝。
- 错误 HMAC 拒绝。
- 重复 idempotency key 不重复写。
- Panda 高水位时应用层降频。

通过标准：

```text
高频同步无法制造高频写盘
合法动态公网 IP 客户端仍可同步
```

## 7. R4 Panda SQLite / 写放大验收

执行时机：Panda 端 SQLite 增量存储完成后。

测试点：

- JSON -> SQLite 导入一致。
- import-batch batch upsert。
- 单账号 update 不全量重写。
- maintenance 批量 delete/update。
- preflight 无变化不写。
- mark_image_result write-behind。

通过标准：

```text
Panda BlockIO 比 R0 下降 80%+
账号数/状态/调度结果一致
```

## 8. R5 生图阶梯压测

执行时机：P0~P4 完成并稳定后。

阶梯：

```text
4 并发
6 并发
8 并发
12 并发（仅探索，不作为常驻目标）
```

每档至少记录：

```text
请求数 >= 20
成功率
p50/p95/p99
Panda CPU/BlockIO
NewAPI 状态码
b64 broken pipe
no available image quota
dictionary changed
```

通过标准：

- `dictionary changed size during iteration = 0`
- 8 并发不出现持续 CPU 打满，CPU p95 <= 70%。
- CPU >= 90% 持续 60s 时必须触发 deadlock_guard，而不是继续压测。
- BlockIO 明显低于基线。
- 429 no available image quota 明显下降。
- p95 不再频繁 200s+，或能解释为上游真实长尾。


## 9. R5.5 异步队列与 CPU 死锁保护验收

执行时机：`ImageTaskService` 改成真正中央队列、任务状态迁 SQLite、timeout_pending 语义完成后。

本地代码状态（2026-07-04）：

- 中央队列、SQLite 任务状态、`timeout_pending` 语义、队列满 429、maintenance deadlock_guard pause 已完成本地实现。
- 本地受影响测试集合 71 passed。
- 已部署到 Panda 并完成 12 个 async image tasks 受控压测：提交接口全 `200 queued`，最终 `12/12 success`，提交 p95 约 `448ms`，生成完成耗时 min `38s` / p50 `202.5s` / p95 近似 `295s` / max `304s`。
- 本节定义的“100 个 async image tasks、CPU p95、内存、duplicate_submit_rate、CPU>=90% 人工触发”仍未执行；不能据此宣称生产容量已经达到高并发最终目标。

测试点：

```text
一次提交 100 个 async image tasks
async_submit_workers = 6
async_submit_workers_max = 8
poll_workers = 24
b64_return_window = 4
```

通过标准：

- 提交接口 p95 < 1s。
- 状态查询 p95 < 500ms。
- 后台真实 submit 并发不超过配置。
- 任一 task 拿到 conversation_id 后，poll timeout 不产生第二个上游 conversation。
- duplicate_submit_rate < 1%。
- timeout_pending 可继续后台恢复，不因 HTTP 客户端断开丢任务。
- CPU p95 <= 70%，内存 < 1.2GB。
- 人工或压测制造 CPU >= 90% 持续 60s 时，必须触发 deadlock_guard：暂停新提交、暂停 maintenance、worker 降级、同步入口快拒绝。

失败即停止进入生产 canary。

## 10. R5.6 生产 18 / 24 / 30 受控压测与带宽监测

执行时机：P6 已部署到 Panda、12 个 async task 受控压测通过后。

本轮目的不是马上提并发参数，而是拿到真实瓶颈数据。**18 / 24 / 30 表示一次性提交的 async image task 数量，不等于把 worker 调到 18 / 24 / 30。** 当前 worker / 队列参数保持不变，除非单轮报告证明需要单独做变更实验。

### 10.1 输入边界

- 不做输入减重。
- 不压缩、缩小、删除参考图。
- 不删 prompt、mask、reference image、multi-image 输入字段。
- 允许记录 `request_payload_bytes`、`reference_image_count`、`reference_image_total_bytes`，但不能为了压测好看而降低输入质量。
- 输出侧可以分别统计 `url` 与 `b64_json`，但优化 b64 回传属于 R6，不能混入本轮压测变量。

原因：输入减重会改变真实生图难度和参考一致性，数据不可用于后续容量判断。

### 10.2 受控阶梯

```text
Stage A: 18 async tasks
冷却与复核
Stage B: 24 async tasks
冷却与复核
Stage C: 30 async tasks
```

每一档必须独立生成报告。18 不过，不进入 24；24 不过，不进入 30。

推荐执行窗口：

```text
单档最大等待：45min
任务状态轮询：15s
单档结束冷却：10min，或直到 queued/running/timeout_pending 全部归零且资源回到基线
```

### 10.3 压测前快照

每档开始前记录到独立目录：

```text
/root/gptimage/backups/loadtest-YYYYmmdd-HHMMSS-stage-{18|24|30}/
```

必须采集：

- `docker compose ps`
- `docker inspect chatgpt2api-local` 的 CPU / memory limit
- `/health?format=json`
- `/api/settings`
- `/api/accounts/maintenance-loop/status`
- `/api/image-tasks` 状态分布
- `data/image_tasks.db` 当前 status count
- 账号池：total / active / limited / abnormal / schedulable / total_quota
- 最近 15 分钟严格错误日志基线
- 宿主机磁盘空间、负载、内存
- **带宽基线**：入站 / 出站 Mbps，至少 60 秒采样

### 10.4 压测中监测

采样间隔建议 1~5 秒。报告中统一写“带宽”，不要只写“网络”。

资源指标：

```text
container_cpu_pct
container_memory_mib
container_pids
block_read_mb_s
block_write_mb_s
bandwidth_rx_mbps
bandwidth_tx_mbps
bandwidth_total_mbps
health_latency_ms
image_task_list_latency_ms
```

任务指标：

```text
submit_http_status
submit_latency_ms p50/p95/p99
status_query_latency_ms p50/p95/p99
queued_count
running_count
timeout_pending_count
success_count
error_count
queue_wait_seconds p50/p95/p99
running_seconds p50/p95/p99
total_seconds p50/p95/p99/max
conversation_created_count
duplicate_submit_count
attempt_count_distribution
```

上游 / 链路指标：

```text
429 / 502 / 503 / 524
connection reset
remote disconnected
broken pipe
poll timeout
image_timeout_pending
no available image quota
token invalid / revoked / 401 / 403
```

账号池指标：

```text
active_before / active_after
limited_before / limited_after
abnormal_before / abnormal_after
schedulable_before / schedulable_after
total_quota_before / total_quota_after
quota_consumed
maintenance_processed / maintenance_deleted
```

### 10.5 带宽判定

服务器公网带宽按 `30Mbps` 观察。判定口径：

```text
bandwidth_total_mbps_p95 < 18Mbps    正常
18~24Mbps                            警戒，需要看 broken pipe / b64 / download tail
>=24Mbps 持续 60s                    视为带宽接近饱和，停止进入下一档
>=28Mbps 任意连续 15s                立即停止本档新增提交
```

如果 CPU / 内存 / BlockIO 都低，但 `bandwidth_tx_mbps` 高且 broken pipe 增加，优先判断为输出回传拥塞，不要误判为 worker 不够。

### 10.6 进入下一档条件

18 -> 24，24 -> 30 必须同时满足：

```text
submit_p95 < 1s
status_query_p95 < 500ms
success_rate >= 95%
duplicate_submit_rate < 1%
timeout_pending_rate <= 10%
HTTP 5xx / 524 / reset / broken pipe 不高于 3%
container_cpu_p95 <= 70%
container_memory_peak < 1.2GiB
bandwidth_total_mbps_p95 < 18Mbps，且没有 >=24Mbps 持续 60s
strict_bad_logs = 0，或所有命中均可解释且不影响任务
账号池 schedulable / total_quota 未异常烧穿
```

### 10.7 停止条件

任一满足即停止当前档，不进入下一档：

```text
CPU >= 90% 持续 60s
health 连续 2 次失败或延迟 > 5s
memory > 1.2GiB 且持续上升
bandwidth_total_mbps >= 24Mbps 持续 60s
HTTP 5xx / 524 / reset / broken pipe > 5%
timeout_pending_rate > 20%
duplicate_submit_rate >= 1%
任务总失败率 > 5%
账号池 schedulable 或 total_quota 出现异常陡降
maintenance 大量删除且影响生图
```

### 10.8 本轮之后才能提优化

18 / 24 / 30 报告出来前，不做以下结论：

- 不判断 worker 应该升到多少。
- 不判断是否必须多桶 / 多出口。
- 不判断是否需要扩 CPU / 内存。
- 不通过输入减重换取表面吞吐。
- 不把账号做长期质量分层作为主方案。

压测报告必须能回答：

```text
瓶颈是上游长尾、带宽、CPU、内存、BlockIO、账号池，还是连接链路？
队列是否只是把尾流藏起来，还是实际降低了 5xx/reset？
当前 6 worker 下，18/24/30 的完成曲线是否线性可解释？
是否存在无效重试和重复上游提交？
是否需要多桶/多出口，而不是单桶继续加 worker？
```

### 10.9 Stage A：18 async tasks 结果（2026-07-04）

执行方式：

- 本地机器通过公网 `https://gptimage.relai.asia` 并发发起 18 个 async image task。
- Panda 只做资源、带宽、日志和任务状态监测，不从 Panda 本机提交任务。
- 输入未减重：6 个文生图，12 个图生图；其中 8 个单参考图、4 个双参考图。
- 参考图为 768x768 PNG，压测总 payload 约 `29.37MB`，参考图原始 PNG 总量约 `22.02MB`。

报告：

- 本地：`reports/loadtest-20260704-191600-stage-18/`
- Panda：`/root/gptimage/backups/loadtest-20260704-191600-stage-18/`
- 汇总：`combined-summary.json`

结果：

```text
submit: 18/18 HTTP 200 queued
final: 18/18 success
timeout_pending: 0
error: 0
strict_bad_logs_60m: 0
```

提交延迟（公网 HTTPS）：

```text
overall submit p50=24.55s, p95=49.32s, max=53.97s
generation submit p95=5.17s
edit submit p95=50.96s
```

公网状态查询延迟：

```text
status query p50=2.77s, p95=4.88s, max=7.16s
```

Panda 任务耗时：

```text
overall generation duration p50=65.31s, p95=109.93s, max=125.42s
generation-only p95=82.04s
edit-only p95=115.40s
```

Panda 资源：

```text
CPU p50=5.90%, p95=13.78%, max=29.52%
memory p50=501.75MiB, p95=526.5MiB, max=526.6MiB
health latency p95=13.07ms
```

Panda 带宽：

```text
rx p95=9.54Mbps, tx p95=5.34Mbps
total bandwidth p95=14.56Mbps, p99=17.30Mbps, max=25.25Mbps
>=24Mbps max consecutive=5s
>=28Mbps max consecutive=0s
```

严格进入 24 条件判断：

```text
submit_p95_lt_1s=false
status_query_p95_lt_500ms=false
success_rate_gte_95=true
timeout_pending_lte_10=true
cpu_p95_lte_70=true
memory_peak_lt_1_2gib=true
bandwidth_p95_lt_18mbps=true
no_ge24mbps_60s=true
strict_bad_logs_zero=true
```

结论：

- 18 档本身成功，Panda CPU、内存、健康页和服务器侧带宽都没有打爆。
- 严格 R5.6 门槛下 **不进入 24**，因为公网大参考图提交 p95 和公网状态查询 p95 明显超标。
- 该结果说明当前瓶颈首先暴露在“公网大输入上传 / 公网状态查询路径”，不是 Panda 生图 CPU。
- 下一步不能通过输入减重解决；应先决定是否调整 R5.6 对“大参考图异步提交”的提交延迟门槛，或优化上传/状态查询路径。

### 10.10 Stage B：24 async tasks 结果（2026-07-04）

执行方式：

- 按用户确认，真实大参考图公网上传不再要求 `submit_p95 < 1s`。
- 本地机器通过公网 `https://gptimage.relai.asia` 并发发起 24 个 async image task。
- Panda 只做监测。
- 输入未减重：8 个文生图，16 个图生图；10 个单参考图、6 个双参考图。
- 总 payload 约 `40.38MB`，参考图原始 PNG 总量约 `30.28MB`。

报告：

- 本地：`reports/loadtest-20260704-194005-stage-24/`
- Panda：`/root/gptimage/backups/loadtest-20260704-194005-stage-24/`
- 汇总：`combined-summary.json`

结果：

```text
submit requested: 24
submit HTTP 200 queued: 22
submit HTTP 429: 2
accepted final: 21 success / 1 error / 0 timeout_pending
strict_bad_logs_60m: 0
```

429 原因：

```text
image task queue is full for current user (20/20)
```

这说明当前配置下单用户同一时间无法完整接纳 24 个任务；实际接纳能力约为 `20 queued + 2 running = 22`。

唯一任务错误：

```text
task_id: r56stage24-local-20260704-194005-gen-04
error: 'ConfigStore' object has no attribute 'proxy_url'
conversation_id: 6a48f1fd-fb94-83ec-9cf7-51af51ed7b25
```

提交延迟（公网 HTTPS，仅记录，不再作为大参考图硬门槛）：

```text
overall submit p50=40.76s, p95=83.71s, max=88.01s
generation submit p95=6.18s
edit submit p95=84.97s
```

公网状态查询延迟：

```text
status query p50=3.05s, p95=6.37s, max=22.66s
```

Panda 任务耗时：

```text
accepted task duration p50=64.26s, p95=99.29s, max=116.34s
generation-only p95=90.27s
edit-only p95=103.40s
```

Panda 资源：

```text
CPU p50=0.54%, p95=7.77%, max=31.16%
memory p50=525.3MiB, p95=559.9MiB, max=594.2MiB
health latency p95=35.63ms
```

Panda 带宽：

```text
rx p95=15.10Mbps, tx p95=6.70Mbps
total bandwidth p50=3.33Mbps, p95=22.31Mbps, max=298.20Mbps
>=24Mbps max consecutive=49.14s
>=28Mbps max consecutive=49.14s
```

判断：

- Panda CPU / 内存仍然很安全。
- 24 档不能算通过：2 个任务被当前 per-user queue cap 快拒绝，1 个已接纳任务失败。
- 带宽 p95 进入 `18~24Mbps` 警戒区，并出现接近但未达到 60s 的 `>=24Mbps` 连续高带宽。
- 不进入 30；先处理：
  1. 单用户队列上限是否要从 `20` 调整到能完整接纳 24/30。
  2. `ConfigStore.proxy_url` 错误。
  3. 公网状态查询慢和高带宽突刺。

## 11. R6 b64 回传窗口验收

执行时机：b64 回传窗口完成后。

测试点：

- response_format=url 请求优先 URL 返回。
- b64 并发达到窗口上限时短等。
- 超过短等返回 429/503，不无限排队。
- broken pipe 后释放窗口。
- 大响应不会占满生成窗口。

通过标准：

```text
b64 回传和生成并发解耦
公共 API 无无限等待
```

## 12. R7 生产 canary 与 soak

执行时机：本地和 staging 验收通过后。

步骤：

1. 低流量窗口部署。
2. 先开小参数：sync/global image concurrency 6，async_submit_workers 6，b64 window 4，CPU 预算按 1.5 vCPU 观察。
3. 观察 30 分钟。
4. 观察 2 小时。
5. 观察 24 小时。

通过标准：

- NewAPI 5xx 不高于部署前。
- Panda BlockIO 维持低位。
- import-batch 次数符合水位策略。
- maintenance 不压垮生图。
- CPU p95 <= 70%；CPU >= 90% 时 deadlock_guard 生效。
- async 任务无重复上游提交。
- 账号池不烧穿。

## 13. 最终容量估算

验收后重新计算：

```text
RPM ≈ active_concurrency * 60 / avg_latency_seconds
```

按 R5/R5.6/R7 结果给出：

- 推荐公共 API active 并发。
- 推荐 b64 回传窗口。
- 推荐 Panda CPU / 内存规格。
- 是否需要多桶。
- 每桶账号池规模。

## 14. 失败处理

任一轮失败：

1. 停止进入下一轮。
2. 记录失败证据。
3. 回滚或降级参数。
4. 修复后从失败轮重新开始。
