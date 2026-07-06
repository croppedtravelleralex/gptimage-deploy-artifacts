# IMG-012 NewAPI 同步入口内部异步化与多阶段传输流水线方案

状态：**待处理 / 未实现 / 未部署 / 未压测**  
记录时间：2026-07-06 15:30 +08:00

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

仍存在的问题：

```text
1. NewAPI /v1/images/* 仍走同步兼容入口，受 image_global_concurrency=6 + queue_timeout=0 影响。
2. 第 7 个同步请求会直接 502/503：image service busy: global concurrency limit 6 reached。
3. 异步 /api/image-tasks 能接 24，但 NewAPI 看不到这条异步任务链路的正常用量日志。
4. 简单把 worker 拉到 12 会放大上游长尾、524/reset/remote disconnected，体验更差。
5. 当前 2 running 的 24 总时长太长，用户侧等待体验不够。
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

## 13. 当前待办结论

IMG-012 已作为下一阶段生图容量优化待办。推荐执行顺序：

```text
1. 先实现 wait_for_result 和 NewAPI sync-over-async。
2. 默认 running=6，压测确认不 busy。
3. 收紧 image_return_window_size=2~3。
4. 做单轮 24 NewAPI 压测。
5. 通过后接 burst 8 条件升档。
6. 再做 3 轮 24 NewAPI 压测。
```
