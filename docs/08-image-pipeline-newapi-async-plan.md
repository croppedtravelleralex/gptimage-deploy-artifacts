# IMG-012 NewAPI 同步入口内部异步化与多阶段传输流水线方案

状态：**已本地实现 + Panda 已部署（2026-07-06 16:41 restart）**；busy_6 验收通过；24 路全成功未通过
记录时间：2026-07-06 15:30 +08:00；实施同步：2026-07-06 16:55 +08:00

## 1. 背景和已验证事实

当前 Panda 已具备异步图片任务队列、两阶段 reference asset 上传、轻量 status 查询和 pre-conversation hard timeout。最新有效压测基线是 `IMG-011`：

```text
报告：reports/img005-stage24-3rounds-20260706-125316/aggregate-summary.json
3 轮 24 公网混合输入：72 submit / 70 success / 2 hard-timeout / 0 unfinished
clean 轮 R2/R3：24/24 success
当前后台单用户 running≈2
clean 轮 24 入队后全完成≈12.4min，全流程含上传≈14min
Panda CPU/内存/带宽均未打满
```

仍存在的问题（方案制定时）：

```text
1. NewAPI /v1/images/* 仍走同步兼容入口，受 image_global_concurrency=6 + queue_timeout=0 影响。
2. 第 7 个同步请求会直接 502/503：image service busy: global concurrency limit 6 reached。
3. 异步 /api/image-tasks 能接 24，但 NewAPI 看不到这条异步任务链路的正常用量日志。
4. 简单把 worker 拉到 12 会放大上游长尾、524/reset/remote disconnected，体验更差。
5. 当前 2 running 的 24 总时长太长，用户侧等待体验不够。
```

**2026-07-06 实施后更新**：

```text
问题 1/2 已通过 IMG-012C + queue_coordinated/skip_global_limit 解决（需容器 restart 才生效）。
当前新瓶颈：NewAPI 网关 24 长连接并发下 HTTP/2 断连与空响应，非 Panda busy。
NewAPI 侧可见耗时 110–300s 含 per_user_running=6 排队等待，属 sync-over-async 预期行为。
```

## 2. 目标

IMG-012 的目标不是硬拉 12/24 worker，而是把 NewAPI 的同步入口改为“内部异步化 + 阶段窗口”的兼容实现：

```text
NewAPI -> Panda /v1/images/*
       -> Panda 内部创建 image task
       -> 后台按 6 常态 / 8 burst 生成
       -> 结果下载/编码/回传受独立窗口保护
       -> /v1/images/* 同步等待自己的 task 结果并返回 OpenAI 兼容响应
```

目标结果：

```text
1. NewAPI 侧仍调用标准 /v1/images/generations 和 /v1/images/edits。
2. NewAPI 日志能看到生图请求和最终成功/失败记录。
3. Panda 不再因为同步入口满 6 直接 busy 502。
4. 24 个真实混合输入请求能被接住，后台按 6/burst8 平滑生成。
5. 24 入队最后一个任务目标完成时间约 4.5~6.5min。
6. 不压缩参考图、不降低输入质量、不用输入减重换性能数字。
```

## 3. 非目标

本方案不做：

```text
1. 不把公共 NewAPI 接口改成无限队列。
2. 不默认 10/12/24 同时上游生成。
3. 不降低参考图质量。
4. 不通过跳过 Panda 账号 preflight 换速度。
5. 不承诺消除上游自身 280s+ 生成长尾。
6. 不在没有 24 三轮数据前进入 30。
```

## 4. 链路拆分

原链路：

```text
A. 用户/压测机 -> Panda：参考图上传
B. Panda -> 上游：参考图上传给 OpenAI/ChatGPT
C. 上游 -> Panda：结果图下载回来
D. Panda -> NewAPI/用户：结果回传
```

目标拆分：

```text
asset_ingress_queue       # 接收参考图，已有 IMG-005 reference assets 基础
upstream_submit_queue     # 提交上游任务，默认 6，健康时 burst 8
upstream_generating_set   # 上游生成等待期，基本不占带宽窗口
result_download_queue     # 上游结果下载窗口，建议 2~3
client_response_queue     # b64/url 组装与回传窗口，建议 2~3
```

关键原则：

```text
上游生成等待期是带宽真空期，可以继续做上传、下载和回传。
但下载和回传不能跟上游生成 worker 数量绑定，否则会把带宽尖峰重新放大。
```

## 5. 推荐初始参数

### 5.1 生成窗口

```text
upstream_generating_base = 6
upstream_generating_burst = 8
submit_workers = 8
per_user_running_base = 6
per_user_running_burst = 8
per_user_queue_max = 48~60
global_queue_max = 240~300
poll_workers = 24
```

解释：

```text
submit_workers 可以是 8，但实际认领运行任务由 base/burst 决定。
默认只释放 6 个上游生成槽；只有健康条件满足才临时 burst 到 8。
```

### 5.2 下载/回传窗口

```text
result_download_window = 2~3
client_response_window = 2~3
image_return_window_size = 2~3    # 当前默认 20 对 30Mbps 单机偏大，应在 IMG-012 中下调
image_return_window_timeout_secs = 180~240
```

### 5.3 带宽保护

```text
bandwidth_soft_limit = 20Mbps
bandwidth_hard_limit = 24Mbps
bandwidth_emergency_limit = 28Mbps
```

动作：

```text
5s EWMA >= 20Mbps：暂停新开 result download / client response
5s EWMA >= 24Mbps：只保留已在传输的下载/回传，不再放新传输
连续 10s >= 28Mbps：冻结 download/response 15~30s
```

## 6. burst 8 升降档规则

### 允许 burst 到 8

全部满足才允许：

```text
1. queue backlog >= 6
2. dispatchable_candidate_count >= 80
3. preflight_backoff_count 没有快速上升
4. image_inflight_count < 8
5. CPU p95 < 50%
6. memory < 1.2GiB
7. bandwidth_5s_EWMA < 18Mbps
8. 最近 5min hard_timeout / 524 / reset 无突增
```

### 退回 6

任一满足立即退回：

```text
1. bandwidth_5s_EWMA >= 24Mbps
2. bandwidth >= 20Mbps 持续 60s
3. task p95 > 180s 且持续恶化
4. hard_timeout_rate > 5%
5. timeout_pending_rate > 10%
6. 524/reset/remote disconnected 增加
7. dispatchable_candidate_count < 50
8. CPU p95 > 70% 或 deadlock_guard warning
```

### 紧急降档

```text
CPU >= 90% 持续 60s
或 health 延迟 > 5s 连续 3 次
或 image_inflight 残留不释放
```

动作：

```text
生成窗口降到 4
暂停新接收高并发同步等待
maintenance slow/pause
保留 status 查询
记录 deadlock_guard 事件
```

## 7. NewAPI sync-over-async 设计

### 7.1 `/v1/images/generations`

新行为：

```text
1. 校验 auth、content filter。
2. 生成内部 client_task_id。
3. 调 image_task_service.submit_generation()。
4. wait_for_task_result(task_id, timeout)。
5. success：返回 task.data + usage，保持 OpenAI 兼容响应。
6. error：按错误类型映射 4xx/5xx。
7. timeout_pending：如果外层等待超时，返回 504/503，错误信息带 task_id 便于排查；不重复提交上游。
```

### 7.2 `/v1/images/edits`

新行为：

```text
1. multipart / JSON 图片输入仍按现有 parse_image_edit_request 解析。
2. 直接上传到 /v1/images/edits 的参考图先进入任务 payload。
3. 已经使用 asset_id 的请求保持 asset_id，不重复读大图。
4. submit_edit 后 wait_for_task_result。
5. 返回 OpenAI 兼容 data。
```

### 7.3 同步等待时间

建议新增配置：

```text
newapi_image_sync_wait_timeout_secs = 540
newapi_image_sync_poll_interval_secs = 1.0~2.0
newapi_image_sync_admission_max_eta_secs = 600
```

注意：

```text
如果 NewAPI / Cloudflare 外层 HTTP 超时时间低于 540s，仍可能出现外层 524。
IMG-012 先保证 Panda 内部不 busy、不重复提交、不丢任务；外层 NewAPI 超时需要后续配合 NewAPI 自身超时配置或异步 task adapter。
```

## 8. 24 并发耗时预估

基于 IMG-011 clean 轮：

```text
当前 2 running：24 入队后全完成≈12.4min
单任务运行 p50≈54~57s，p95≈85~90s
```

IMG-012 预估：

| 档位 | 24 个请求最后一个完成时间 | 说明 |
| --- | ---: | --- |
| 固定 6 | 5~6.2min | 稳定性最好 |
| 6 + burst 8 | 4.5~6min | 推荐目标 |
| 固定 8 | 4.5~6.5min | 更快但尾流风险更高 |
| 极差上游尾流 | 8~10min+ | 上游自身慢，Panda 只能止损不能消除 |

带宽预估：

```text
平均带宽：5~8Mbps
p95 带宽：18~24Mbps
5s max：24~30Mbps
```

## 9. 实施切片

### IMG-012A：文档和配置骨架

```text
新增配置字段：
- newapi_image_sync_wait_timeout_secs
- image_task_queue.per_user_running_base
- image_task_queue.per_user_running_burst
- image_task_queue.burst_enabled
- image_task_queue.burst_bandwidth_soft_limit_mbps
- image_task_queue.download_window
- image_task_queue.response_window
```

### IMG-012B：ImageTaskService wait API

新增服务方法：

```python
wait_for_result(identity, task_id, timeout_secs) -> dict
```

要求：

```text
1. 不返回 payload / 大字段之外的历史任务。
2. success 返回完整 task.data / usage。
3. error 返回错误。
4. timeout_pending 不重复提交，保持后台续轮询。
5. 等待期间只 watch 当前 task，不扫全库。
```

### IMG-012C：NewAPI `/v1/images/*` 接入内部异步

```text
api/ai.py:
- /v1/images/generations -> submit_generation + wait_for_result
- /v1/images/edits -> submit_edit + wait_for_result
```

保留：

```text
/api/image-tasks/* 原异步入口继续给 image-manager 使用。
```

### IMG-012D：动态 6/burst8 调度

```text
ImageTaskService._next_submit_task_locked()
- running_limit 使用动态 base/burst
- burst 条件基于 health/带宽/错误/候选数
```

最低可落地版本：

```text
先用配置固定 per_user_running_max=6；
同时保留 submit_workers_max=8；
真实 burst 条件作为二期接入。
```

### IMG-012E：下载/回传窗口收紧

```text
image_return_window_size 从当前 20 下调到 2~3。
result download 和 response window 分开是最终形态；
如果当前代码仍在 conversation.py 内下载并格式化，先用 image_return_window_service 作为统一回传窗口止血。
```

### IMG-012F：生产部署和压测

压测必须通过 NewAPI：

```text
NEWAPI_BASE_URL=https://sub2api.closeapi.top
NEWAPI_API_KEY=<从环境变量读取，不写入文档、不写入日志>
```

压测输入：

```text
24 并发
8 文生图 + 16 图生图
10 单参考 + 6 双参考
参考图不压缩、不缩小、不减重
```

## 10. 验收标准

### 本地测试

```text
py_compile 通过：api/ai.py services/image_task_service.py services/config.py
相关 pytest 通过：
- test_image_task_service.py
- test_image_tasks_api.py
- test_v1_images_generations.py
- test_v1_images_edits_api.py
- test_v1_images_edits_json.py
```

### 生产 smoke

```text
1. /health?format=json healthy=true
2. image_task_queue 显示 base=6 / burst=8 或等价配置
3. /v1/images/generations 单请求成功
4. /v1/images/edits 单参考图成功
5. 超过 6 的 NewAPI 并发不再直接出现 image service busy: global concurrency limit 6 reached
```

### 24 压测

```text
24/24 HTTP 请求进入 Panda，不出现同步入口 busy 6
最终 success >= 70/72（三轮）
unfinished = 0
image_inflight_count 最终 = 0
strict_bad_count_60m = 0
bandwidth p95 < 24Mbps
5s max < 30Mbps，且 >=24Mbps 不连续超过 60s
CPU p95 < 40%，CPU max 不接近 90% deadlock_guard
NewAPI 侧能看到对应生图调用日志
```

## 11. 回滚方案

生产部署前必须备份：

```text
api/ai.py
api/image_tasks.py
services/image_task_service.py
services/config.py
services/image_return_window_service.py
config.json
```

回滚动作：

```text
1. 恢复上述文件和 config.json。
2. docker compose -f docker-compose.panda.yml restart app
3. 验证 /health?format=json。
4. 单请求 /v1/images/generations smoke。
```

SQLite `image_tasks.db` 原则上不回滚删除；如任务状态结构变更，需要先备份 DB，再执行迁移/回滚脚本。

## 12. 风险

```text
1. NewAPI / Cloudflare 外层超时可能低于 Panda 内部等待时间，仍会产生 524。
2. 固定 8 可能放大上游尾流，所以默认必须是 6 + 条件 burst 8。
3. image_return_window_size 当前默认 20 偏大，IMG-012 必须同步收紧。
4. 如果账号池真实候选数虚高，6/8 只会更快暴露 no available image quota。
5. Python 线程 hard timeout 不能真正 kill 底层阻塞 I/O，后续仍应做进程级 kill。
```

## 13. 实施状态（2026-07-06）

### 已实现（本地 + Panda）

| 切片 | 交付物 | 说明 |
| --- | --- | --- |
| IMG-012A | `services/config.py` | `newapi_image_sync_wait_timeout_secs`、`per_user_running_base/burst`、`image_return_window_size=3` |
| IMG-012B | `ImageTaskService.wait_for_result()` | 轮询单 task 至终态 |
| IMG-012C | `services/image_sync_adapter.py` | `run_generation_sync` / `run_edit_sync` |
| IMG-012C | `api/ai.py` | 非 `stream` 的 `/v1/images/*` → submit + wait |
| IMG-012C+ | `queue_coordinated` 链路 | `image_task_service` payload → `conversation.py` → `skip_global_limit=True` |
| 部署脚本 | `scripts/img012_*.py` | deploy / patch_config / verify / loadgen / enable_burst |

Panda 生产：

```text
备份：/root/gptimage/backups/img012-sync-over-async-20260706-*
config：per_user_running_max=6, per_user_running_base=6, burst_enabled=false
容器 restart：2026-07-06 16:41 +08:00（此前 12:51 启动的进程未加载新代码，导致 busy_6 仍出现）
```

### 未实现 / 未启用

```text
IMG-012D：动态 burst 8（代码骨架在 _effective_per_user_running_max_locked，生产 burst_enabled=false）
IMG-012E：result download / client response 独立窗口（仅 image_return_window_size 下调）
stream=true 的 /v1/images/* 仍走旧同步 handler，未接 queue_coordinated
NewAPI 外层超时 / HTTP/2 网关调优
3 轮 72 路正式验收
```

### 压测结果（2026-07-06 16:42 NewAPI 单轮 24）

```text
报告：reports/img012-newapi-sync-stage24-1rounds-20260706-164210/
入口：NewAPI https://sub2api.closeapi.top，标准 /v1/images/*
结果：24 请求 / 5 成功 / 19 失败 / busy_6=0
成功耗时：60–111s（均为文生图）
失败：JSONDecodeError 空响应 8 + HTTP/2 ConnectionTerminated 11（多为图生图，127–489s）
Panda 日志 busy_6：0 条（压测前后 15min）
压测结束 image_inflight_count=5（部分上游任务仍在跑）
```

验收对照：

| 门槛 | 结果 |
| --- | --- |
| 超过 6 并发不出现 busy_6 | ✅ 通过 |
| 24/24 或 ≥23/24 成功 | ❌ 5/24 |
| 三轮 70/72 | ❌ 未跑 |
| unfinished=0 | ❌ 未验证 |

### 耗时口径说明

```text
压测脚本 elapsed_ms = 客户端 HTTP 往返（含排队 + 上游 + 回传）。
成功 5 个文生图：60–111s（较早拿到 running 槽位）。
NewAPI 仪表盘 110–300s：统计全部请求；靠后排队请求 ≈ 1–3 批 × 60–110s + 执行时间。
24 路 @ per_user_running=6 预估末位完成：5–6.2min（§8），与失败请求 127–489s 一致。
```

### 当前待办结论

推荐执行顺序：

```text
1. ✅ wait_for_result + NewAPI sync-over-async（已完成）
2. ✅ 默认 running=6 + restart 后确认不 busy（已完成）
3. ✅ image_return_window_size=3（已配置）
4. ⚠️ 单轮 24 NewAPI 压测：busy_6 通过，成功率未通过 → 先修 NewAPI 传输层
5. 通过后接 burst 8 条件升档
6. 再做 3 轮 24 NewAPI 压测（70/72）
```

## 2026-07-06 IMG-012 追加：asset pointer 小请求、NewAPI 压测和 524 根因

状态：**Panda 已部署并验收；NewAPI 6/12 通过，24 同步入口仍受 NewAPI/Cloudflare 外层超时限制**。

已部署备份：

```text
/root/gptimage/backups/img012-assetids-smallreq-20260706-173824/
/root/gptimage/backups/img012-asset-pointer-20260706-174706/
/root/gptimage/backups/img012-resume-poll-hard-timeout-20260706-195215/
```

新增实现：

- `api/image_inputs.py`：支持 `panda-asset://<asset_id>` 指针；NewAPI 必填的 multipart `image` 文件可用极小 text/plain 指针文件替代，不再让大参考图二次穿过 NewAPI。
- `api/ai.py` / `api/image_tasks.py`：读取 image sources 时自动拆出 asset ids，指针文件不会作为真实参考图传给上游。
- `scripts/img012_newapi_sync_loadgen.py`：支持 `6/12/18/24/30` 小档；图生图先上传 Panda asset，再通过 NewAPI 发送 pointer file；新增 `IMG012_HTTP2=0/1` 对照。
- `services/image_task_service.py`：`resume_polling` 增加 hard-timeout 止血，避免续轮询长期停在 running。

本地验证：

```text
python -m py_compile api/ai.py api/image_inputs.py api/image_tasks.py services/image_sync_adapter.py services/image_task_service.py scripts/img012_newapi_sync_loadgen.py
python -m pytest test/test_v1_images_edits_api.py test/test_v1_images_edits_json.py test/test_v1_images_sync_async.py test/test_image_task_service.py test/test_image_tasks_api.py -q
# 36 passed（asset pointer 后）
python -m pytest test/test_image_task_service.py test/test_image_tasks_api.py test/test_v1_images_edits_api.py test/test_v1_images_sync_async.py -q
# 28 passed（resume poll hard-timeout 后）
```

生产验收：

```text
Panda health after final deploy: healthy=true, image_inflight_count=0, dispatchable_candidate_count=160, verified_total_quota=752, preflight_backoff_count=0
NewAPI asset pointer smoke: 1/1 success, elapsed≈34.15s
NewAPI stage 6: 6/6 success, busy_6=0, report=reports/img012-newapi-sync-stage6-1rounds-20260706-175102/
NewAPI stage 12: 12/12 success, busy_6=0, report=reports/img012-newapi-sync-stage12-1rounds-20260706-175304/
```

24 对照结果：

```text
HTTP/2 + pointer，24 同时提交：客户端 17/24 success，7 个 RemoteProtocolError / stream reset；Panda DB 仅入库 17 个，17/17 success。
HTTP/1.1 + pointer，24 同时提交：客户端 14/24 success，10 个 Cloudflare 524（约 175~209s）；Panda DB 入库 24 个，最终 23 success / 1 error。
窗口 12 对照：被 closeapi/NewAPI 短时拒连污染（ConnectError 10061 / server disconnected），不能作为容量结论；停止继续压测。
```

判断：

- `busy_6` 已解决；大参考图二次穿 NewAPI 也已解决。
- 24 同步入口失败主因不是 Panda 生成失败，而是 NewAPI/closeapi/Cloudflare 同步长连接约 175~210s 超时或断流。
- Panda 对“已入库任务”的完成率高；但 NewAPI 标准同步接口无法稳定承载 24 个长等待请求。
- 想要 24/30 的用户体验，不能继续强压 `/v1/images/*` 长同步；需要 NewAPI 侧接异步 task/callback，或把公共同步入口 admission 控制在能低于 Cloudflare 超时的窗口内。

## 2026-07-06 IMG-013 追加：NewAPI async prompt tunnel 已部署

状态：**已部署 Panda；NewAPI 小档验收通过入队与轮询通路，未进入 24/30/36 大档**。

背景：

- NewAPI/closeapi 对非 `/v1` 路径 `/api/image-tasks/status` 返回 `403`，不能直接把 Panda 原生异步接口暴露给 NewAPI baseurl。
- NewAPI 会剥掉 `/v1/images/generations` JSON 里的未知字段；`panda_async` / `panda_task_id` 不能可靠穿透。
- multipart edits 的额外 form 字段可穿透，但 generations/status 必须走稳定字段。

新增实现：

- `/v1/images/generations` 支持 prompt tunnel：
  - `panda-async: <真实提示词>`：提交异步任务并立即返回 `object=image.task` / `task_id`。
  - `panda-task://<task_id>`：查询任务；成功时返回标准 OpenAI image response，并附带 `panda_task`。
- `/v1/images/edits` 同时支持 form 字段 `panda_async=true` 和 prompt tunnel。
- `ImageTaskService` burst 条件配置化：默认 `burst_min_dispatchable_candidates=120`、`burst_min_queued=6`、`burst_max_preflight_backoff=0`。
- 新增 `scripts/img013_newapi_async_loadgen.py`：提交和轮询均走 NewAPI `/v1/images/*`。

生产部署：

```text
备份：/root/gptimage/backups/img013-newapi-async-tunnel-20260706-203319/
回滚：/root/gptimage/backups/img013-newapi-async-tunnel-20260706-203319/ROLLBACK.sh
部署文件：api/ai.py, api/image_inputs.py, services/config.py, services/image_task_service.py, scripts/img013_newapi_async_loadgen.py
配置：burst_enabled=true, base=6, burst=8, per_user_queue_max=48, global_queue_max=240, image_return_window_size=3
最终收尾 health：healthy=true, image_inflight_count=0, dispatchable_candidate_count=148, preflight_backoff_count=0, verified_total_quota≈692
```

本地验证：

```text
python -m py_compile api/ai.py api/image_inputs.py services/config.py services/image_task_service.py scripts/img013_newapi_async_loadgen.py
python -m pytest test/test_image_task_service.py test/test_image_tasks_api.py test/test_v1_images_generations.py test/test_v1_images_edits_api.py test/test_v1_images_edits_json.py test/test_v1_images_sync_async.py test/test_account_image_capabilities.py test/test_config.py -q
# 67 passed

git diff --check
# 通过，仅 LF/CRLF warning
```

NewAPI 验收：

```text
报告：reports/img013-newapi-async-stage6-1rounds-20260706-205344/
入口：https://sub2api.closeapi.top/v1/images/*
提交：6/6 submit_ok，submit_p95≈19.0s，无 524、无 busy_6
最终：5/6 success，1 个 generation hard-timeout
总时长：675.9s（被 1 个 hard-timeout 拖长）
成功任务轮询耗时：约 30s、44s、55s、55s、69s
失败原因：image task hard timeout before upstream completion (510.0s); no conversation_id captured
```

判断：

- IMG-013 已解决 NewAPI 无法穿透异步字段的问题；异步入队不再依赖会被剥掉的 JSON unknown fields。
- NewAPI 长同步 524 问题在提交层已绕开：stage6 提交全部成功，最长约 19s，而不是 175~210s 长等。
- 本轮未进入 24/30/36 大压测，因为 stage6 已出现 1 个上游 pre-conversation hard-timeout；继续扩大只会把异步链路验收污染成上游/账号尾流压测。
- 下一步应单独处理 pre-conversation hard-timeout 的进程级 kill / slot 自愈 / 账号失败归因，再重新跑 24/30/36。

## 2026-07-06 IMG-014：任务错误状态响应去 NewAPI 错误化

状态：**已部署 Panda，并用 NewAPI baseurl/key 验证**。

问题：

- NewAPI/CloseAPI 日志出现大量 `status_code=200, image task hard timeout before upstream completion (510.0s); no conversation_id captured`。
- 根因不是新的 524，而是 IMG-013 的任务状态查询在 task `status=error` 时返回了顶层 `error` 字段；NewAPI 即使 HTTP 200 也会把顶层 `error` 记为渠道错误。
- `panda-task://...` / `panda-task:...` 形式会触发 closeapi Cloudflare `1010`；状态查询 tunnel 改为纯文本 `panda status <task_id>`。

修复：

- `/v1/images/*` task 状态查询遇到失败任务时返回：
  - `object=image.task`
  - `status=error`
  - `panda_error={message,type,code}`
  - **不再返回顶层 `error`**
- `scripts/img013_newapi_async_loadgen.py` 轮询 prompt 改为 `panda status <task_id>`，并默认带浏览器 User-Agent，避免 closeapi 1010。

验证：

```text
本地：py_compile api/ai.py scripts/img013_newapi_async_loadgen.py
本地：pytest test/test_v1_images_sync_async.py test/test_v1_images_edits_api.py test/test_image_task_service.py test/test_config.py -q = 28 passed
生产备份：/root/gptimage/backups/img014-task-error-envelope-20260706-211820/
生产 health：healthy=true, image_inflight_count=0, dispatchable_candidate_count≈154, preflight_backoff_count=0
NewAPI 查询已知 error task：HTTP 200, object=image.task, status=error, has_top_error=false, has_panda_error=true
```

结论：

- NewAPI 不应再因为任务状态 error 被刷 `status_code=200 + hard timeout` 错误日志。
- hard-timeout 的上游根因仍未根治；后续继续处理 pre-conversation 可 kill 执行与 slot 自愈。

## 2026-07-06 IMG-016：同步入口过载自动异步 + hard-timeout 强制释放 slot

状态：本地已实现并通过受影响测试，待生产部署验收。

变更点：
- `/v1/images/generations` 与 `/v1/images/edits` 的非 stream 同步兼容入口新增 admission gate。
- 当同步等待席位已满，或账号侧 `image_inflight_count >= image_global_concurrency_limit` 时，不再继续占用公网长连接等待，而是复用现有 ImageTaskService 入队并返回 `object=image.task` / `task_id`。
- 显式 `panda-async:` / `panda status <task_id>` tunnel 保持不变。
- `conversation.py` 在成功获取图片账号 token 后通过 progress callback 回传 runtime `access_token`，仅用于内存态 slot 释放，不写入公开任务响应。
- `ImageTaskService._run_task()` 在 hard timeout 时会对已租用 token 执行 `account_service.release_image_slot()`，并记录 `force_released_inflight_count`，避免 DB 已 error 但 `image_inflight_count` 长期残留必须重启。

验证命令：
```bash
python -m py_compile api/ai.py services/image_task_service.py services/protocol/conversation.py test/test_image_task_service.py test/test_v1_images_sync_async.py
python -m pytest test/test_image_task_service.py test/test_v1_images_sync_async.py test/test_v1_images_edits_api.py test/test_v1_images_edits_json.py test/test_image_tasks_api.py -q
```

验证结果：`44 passed`。

下一步生产验收：
- 部署前备份 Panda 生产文件与必要数据。
- 部署后 health 应保持 `healthy=true`、`image_inflight_count=0`。
- NewAPI 小档验证：普通同步请求在满载时返回 `image.task`；`panda status <task_id>` 能查回终态。
- 后续 24/30/36 压测改为异步 submit + status polling，不再用 24 条同步长连接硬等。

### IMG-016 生产后 NewAPI 异步 24 压测结果（2026-07-06 23:48 +08）

入口：`https://sub2api.closeapi.top/v1/images/*`，脚本：`scripts/img013_newapi_async_loadgen.py`，参数：`IMG013_STAGE=24, IMG013_ROUNDS=1, IMG013_HTTP2=0, IMG013_USE_PANDA_ASSETS=1`。

结果：未通过，停止 30/36 升档。

```text
requested=24
submit_ok=23
submit_failed=1
final_success=14
final_failed=9
submit_p95=70.24s
final_p95=642.30s
duration=763.51s
```

关键事实：
- 提交阶段仍有 1 个 edit 请求被远端 reset，且 edit 提交 p95 仍高达 70s，说明参考图/asset pointer 穿 NewAPI 仍存在提交层长尾。
- 失败集中为 `upstream connection timed out, please retry later` 与 3 个 generation hard-timeout。
- 本轮 3 个 hard-timeout 均记录 `force_released_inflight_count=1`，IMG-016 对 hard-timeout slot 释放生效。
- 压测后曾出现 `image_inflight_count=1` 且 DB unfinished=0，60s 后自然回到 `image_inflight_count=0`；剩余问题来自非 hard-timeout 上游连接超时线程滞后，仍需二期 sweeper / 更细粒度 lease 覆盖。
- 最终收尾：`healthy=true, image_inflight_count=0, unfinished=[]`。

下一步：不要继续 30/36；先处理 edit 提交长尾、非 hard-timeout slot 滞后、上游 connection timeout 降级与重试策略。
## 2026-07-10 IMG-017：conversation-ready deadline、可取消 hard timeout 与流资源回收

状态：**已部署 Panda；直连同步 canary 与线程/连接验收通过；NewAPI 外层待正式环境变量复测**。

### 现象与生产证据

部署 IMG-016 首 payload deadline 后，仍出现 3 条新失败：

```text
sync-F-1gEBG5g3AwxN_HVu8icw  315000ms
sync-vuS7njaJVmZqprpwyp7euA 315000ms
sync-6r-fdDEN-pG3kJ8jypSNiA 315000ms
```

共同特征：

- 约 4～5 秒进入 `image_upstream_phase=generating`。
- 之后没有 `image_stream_resolve_start`，任务侧也没有 `conversation_id`。
- strace 显示 curl 线程周期性收到约 76-byte TLS 数据；不是完全无 body，而是上游持续发送小型 control/heartbeat。
- DB 在 315 秒转 error 后，底层 handler/curl 线程仍可存活十余分钟；进程线程数和代理 `ESTABLISHED/CLOSE_WAIT` 会累积。
- 三个失败账号和 Webshare 代理均不同，且历史有成功记录，排除单账号/单代理永久失效。

### 根因链

```text
非空 ping/control SSE payload
  -> 被“首个非空 data 即 ready”误判
  -> 45 秒 deadline 被提前解除
  -> 后续 response_queue.get() 无 timeout
  -> heartbeat 使 libcurl low-speed timeout 不触发
  -> Panda 315 秒安全网只改 DB/释放 slot，不能取消底层流
  -> handler/curl 线程与代理连接继续存活
  -> NewAPI 同步适配器读到 Panda error
  -> RuntimeError 经 _image_error_response() 映射为 HTTP 502
```

因此 NewAPI 不是独立超时源；`status_code=502` 是 Panda hard-timeout 终态错误的映射结果。

### 实现

1. **conversation-ready deadline**
   - pre-conversation ready predicate 必须捕获真实 `conversation_id`。
   - ping/control/heartbeat 仍可透传解析，但不能解除 45 秒 deadline。
   - 日志事件统一为 `image_pre_conversation_sse_ready[_deadline]`，带 `ready_label`。

2. **post-ready 15 秒切轮询**
   - 捕获 `conversation_id` 后，SSE 最多再保留 15 秒墙钟窗口。
   - control heartbeat 不延长该窗口。
   - 到期设置 `quit_now`、非阻塞结束 generator，保留已解析状态并转 `/backend-api/tasks` / conversation poll。

3. **取消信号贯穿**
   - `ImageTaskService` 创建 `cancel_event`。
   - generation/edit handler 传到 `ConversationRequest`。
   - backend、queue reader 与 `_poll_image_results()` 的 sleep/循环均检查同一事件。
   - hard timeout 设置事件后等待 1 秒宽限，并记录 `runner_alive_after_cancel`。

4. **已捕获会话的 hard timeout 不再终态 502**
   - progress callback 原子保存 `conversation_id` 与当前 `resume_access_token`。
   - hard timeout 若已有会话：释放 slot、任务转 `timeout_pending`、`next_resume_ts=now+1`，由 poll worker 继续原任务。
   - 无会话时才沿用 transient backoff + mark-fail。

5. **单一 slot 记账与资源回收**
   - `ImageStreamCancelledError` 不在 conversation 层重复 `mark_image_result(False)`；由 task service 统一释放。
   - `OpenAIBackendAPI.close()` 幂等关闭 session，并对 curl_cffi stream executor 调用 `shutdown(wait=False, cancel_futures=True)`。
   - image 与 text backend 均显式 close，降低 executor/连接残留风险。

### 验证

本地：

```text
相关回归：85 passed
multi-image/poll 相关：13 passed
合计：98 passed
py_compile：passed
git diff --check：passed
```

新增/强化用例覆盖：

- control payload 不满足 conversation-ready deadline。
- post-ready control heartbeat 不延长 15 秒 deadline。
- cancel 立即中止 queue-backed SSE。
- cancel 中断 image poll sleep。
- conversation metadata progress 同时携带所属 access token。
- hard timeout 已捕获会话时 runner 接收 cancel、任务进入 `timeout_pending`、slot 仅释放一次。
- backend close 幂等并 shutdown stream executor。

Panda 部署：

```text
备份：/root/gptimage/backups/img017-conversation-deadline-cancel-20260710-214942/
容器 compile：passed
容器目标测试：11 + 21 + 3 = 35 passed
health：healthy=true, total=12, schedulable=12, dispatchable=12, image_inflight_count=0
```

真实直连同步 canary：

```text
task_id=sync-zKrDisvuqd_XiyLkwLvHvA
HTTP 200
elapsed=78.91s
duration_ms=78777
status=success
conversation_id=6a50fad1-c09c-83ec-b1ff-41c90c014b59
threads=2 -> 2
after_tcp={LISTEN: 2}
unfinished={}
recent bad markers=0
```

部署脚本前两次分别因只读 bind mount 的 pycache 写入、裸 `python` 不含项目依赖而自动回滚；第三次改用临时 `PYTHONPYCACHEPREFIX` 与容器真实 `uv run python` 后完成。两次失败均未让新代码生效，旧版本健康状态保持正常。

### 剩余边界

- 当前环境没有 `NEWAPI_API_KEY/NEWAPI_BASE_URL`，未读取或搜索密钥；需后续用正式环境变量做 1～3 次 NewAPI 低并发同步复测。
- 非流式 HTTP 请求本身仍不能被 Python `Event` 强制中断；如果 hard timeout 正好落在单次 `/backend-api` 请求内，`runner_alive_after_cancel` 可能短时为 true，但请求有自身 timeout，且任务已可进入 `timeout_pending`。后续若再次出现持续残留，再评估进程级隔离/kill，而不是盲目增加线程。
- 不继续 24/30/36 压测；先观察真实低并发是否彻底消除 `315s + no conversation_id captured` 与线程/连接累积。

## 2026-07-11 IMG-018：标准同步过载 429 + ETA 准入 + 削峰

状态：**已部署 Panda**；NewAPI single/C2/C3 通过（零空 data 200）。

要点：

1. 标准同步客户端过载不再返回空 `image.task` 200，改为 429 `image_service_busy`。
2. admission 与上游 inflight 解耦；`admission_max=12` + EWMA `max_eta_secs=180`。
3. `submit_start_min_interval_ms=1500` 削开工尖峰；小池 soft burst `dispatchable>=8`。
4. 生产升档：`global=6` / `per_user=2` / `submit_workers=2`。
5. 验收脚本：`scripts/img018_sync_admission_acceptance.py`；单元：`test/test_image_sync_admission_eta.py`。

回滚：`/root/gptimage/backups/img018-sync-eta-pacing-20260711-092302/ROLLBACK.sh`
