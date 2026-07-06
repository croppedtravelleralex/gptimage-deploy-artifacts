# 长期改进池

## 账号与额度

### ACC-001 远端部署额度语义修复

- 现象/问题：本地已区分真无限额与未知额度，但远端是否已同步需要再验收
- 建议方向：部署后复查 `/health?format=json` 与账号列表
- 优先级：高
- 状态：**已完成**（2026-06-29 部署至 Panda VPS，2026-07-03 文档同步）
- 备注/证据：
  - 代码：`services/account_service.py`、`api/system.py`、`web/src/app/accounts/page.tsx`
  - 规范：`docs/quota-semantics.md`
  - 生产备份：`/root/gptimage/backups/quota-fix-20260629-235620/`
  - 验收：`curl -sS 'https://gptimage.relai.asia/health?format=json'` 含 `unknown_quota_count` 字段，`unlimited_quota_count` 不再误报 unknown 账号

### ACC-002 注册后验号失败分类再细分

- 现象/问题：注册后快速失败里，超时、空错误、invalid 目前还可以更清楚地区分
- 建议方向：把诊断日志结构化输出，按 transient / invalid / other 分类
- 优先级：高
- 状态：待处理
- 备注/证据：`data/register_post_verify_diagnostics.jsonl`

### ACC-003 刷新后增量同步失败重试

- 现象/问题：Panda 同步失败时会进入 pending，但仍需要持续确认重试和清理逻辑
- 建议方向：给 pending 增加更清楚的观测和失败统计
- 优先级：高
- 状态：待处理
- 备注/证据：`services/account_refresh_all_service.py`

### ACC-004 死号删除口径统一

- 现象/问题：死号、限流、异常在不同入口的删除策略仍需继续对齐
- 建议方向：统一删除时机、删除阈值和日志文案
- 优先级：高
- 状态：待处理
- 备注/证据：`services/account_service.py`、`services/register/openai_register.py`

## 运维与同步

### OPS-001 远端健康页监控

- 现象/问题：远端状态需要靠人工查看时，容易错过语义回退
- 建议方向：给 `/health?format=json` 加定期检查或简单告警
- 优先级：中
- 状态：待处理
- 备注/证据：`api/system.py`

### OPS-002 Panda 同步 auth key 验收

- 现象/问题：Panda 侧 auth key 或 base_url 不可用时，增量同步会落到 pending
- 建议方向：新增一条专门的远端连通性检查
- 优先级：中
- 状态：待处理
- 备注/证据：`services/account_refresh_all_service.py`

### OPS-003 注册链路瞬断趋势统计

- 现象/问题：当前日志已能看到 transient，但趋势分析还不够直接
- 建议方向：把注册失败的原因按类别聚合成月度统计
- 优先级：中
- 状态：待处理
- 备注/证据：`services/register/openai_register.py`

## 文档与交接

### DOC-001 维护入口固定化

- 现象/问题：旧专题文档和新的维护主档并存，容易让接手人绕路
- 建议方向：在对外 README 里只保留公共介绍，在 docs 里固定维护入口
- 优先级：中
- 状态：待处理
- 备注/证据：`README.md`、`docs/README.md`

### DOC-002 月度日志常态化

- 现象/问题：如果不持续追加月度日志，交接会重新丢失上下文
- 建议方向：每次大改动都追加 `docs/logs/YYYY/YYYY-MM.md`
- 优先级：中
- 状态：进行中
- 备注/证据：`docs/logs/2026/2026-06.md`

### DOC-003 旧专题文档分层说明

- 现象/问题：`feature-status.en.md`、`deployment.md` 等更像专题资料，不是维护真相源
- 建议方向：在维护 README 中明确“专题资料 / 维护主档”的边界
- 优先级：低
- 状态：待处理
- 备注/证据：`docs/README.md`



## 性能、存储与同步升级（2026-07-03 新增）

### PERF-001 修复账号池并发读写 bug

- 现象/问题：生图取号时曾出现 `dictionary changed size during iteration`，说明 `_accounts` 遍历和 maintenance/import 修改存在并发冲突。
- 建议方向：账号候选列表生成使用锁内 snapshot；所有 `_accounts.values()` 遍历明确锁边界。
- 优先级：P0
- 状态：待处理
- 备注/证据：`services/account_service.py`、NewAPI 错误日志。

### STORE-001 本地账号池 SQLite 主存储

- 现象/问题：用户口径 `Accounts.js` / 当前代码 `data/accounts.json` 高频写会伤本地磁盘，也不利于 1h/3h/6h 状态管理。
- 建议方向：新增本地 SQLite 主存储，注册、探活、删除、同步状态写 SQLite；旧文件低频导出快照。
- 优先级：P0
- 状态：待处理
- 备注/证据：`scripts/sync_accounts_delta_to_panda.ps1` 当前读取 `data/accounts.json`。

### STORE-002 Panda 账号存储低写放大 SQLite

- 现象/问题：当前 JSON 全量写和现有 database backend 的全量 delete+insert 语义都无法彻底解决写放大。
- 建议方向：实现单账号 upsert/delete/update_fields 和 batch transaction；保留 raw_json 和 JSON 快照回滚。
- 优先级：P0
- 状态：待处理
- 备注/证据：`services/storage/database_storage.py`、`services/account_service.py`。

### SYNC-001 Panda 高水位同步策略

- 现象/问题：公网同步曾接近 2.6 次/分钟，Panda 高水位时不应承担高频同步和探活。
- 建议方向：采用 `high=1500`、`low=500`、`emergency=200`、`critical=100`，按水位动态同步。
- 优先级：P0
- 状态：待处理
- 备注/证据：`docs/sync-strategy.md`。

### SYNC-002 动态公网 IP 同步入口保护

- 现象/问题：本地公网 IP 经常变化，固定 allowlist 不适合作为主保护方案。
- 建议方向：Nginx 限频 + Bearer + HMAC + nonce + idempotency + 应用层水位限流。
- 优先级：P0
- 状态：待处理
- 备注/证据：`docs/sync-strategy.md`。

### ACC-005 新号 1h/3h/6h 三次探测

- 现象/问题：新号短时间内死亡会污染 Panda 号池，增加远端 maintenance 和生图 preflight 压力。
- 建议方向：新号先入本地 staging，`T+1h/T+3h/T+6h` 探测通过后才上传 Panda。
- 优先级：P0
- 状态：待处理
- 备注/证据：`docs/07-account-pool-performance-upgrade.md`。

### IMG-001 b64 回传窗口

- 现象/问题：大量 b64_json 同时回传会造成 CPU、内存、带宽尾部拥塞，并引发 broken pipe。
- 建议方向：生成窗口、落地窗口、回传窗口分离；URL 优先；b64_active_limit / bytes_inflight_limit / queue_timeout。
- 优先级：P1
- 状态：待处理
- 备注/证据：`docs/performance-acceptance-test-plan.md`。

### OPS-004 多轮性能验收与容量重估

- 现象/问题：当前并发/RPM/CPU 估算被死号、全量写和同步噪声污染。
- 建议方向：按 R0~R7 多轮测试验收后重新估算公共 API 并发、RPM、CPU、b64 窗口和是否多桶。
- 优先级：P1
- 状态：待处理
- 备注/证据：`docs/performance-acceptance-test-plan.md`。


### IMG-002 同步/异步双通道生图队列

- 现象/问题：现有 `ImageTaskService` 提交即开线程，不是真队列；同步接口长连接在 Cloudflare/NewAPI 链路上容易 524/reset。
- 建议方向：同步入口保持 6 worker 快拒绝；异步入口使用中央调度队列、SQLite 任务状态、per-user fair queue、timeout_pending 续轮询。
- 优先级：P0
- 状态：已部署 Panda，待 R5.5 真实 100 任务压测
- 备注/证据：2026-07-04 本地已将 `services/image_task_service.py` 改为 SQLite 中央队列；受影响测试集合 71 passed；Panda 生产 12 async task 受控压测 12/12 success。

### IMG-003 poll timeout 后禁止换号重开图

- 现象/问题：`ImagePollTimeoutError` 后当前可换号重试，导致单用户任务被放大成多次上游生图，增加尾流和烧号。
- 建议方向：拿到 `conversation_id` 后，timeout 改为 `timeout_pending` 并继续 poll 原 conversation；仅 pre-submit 失败允许有限重试。
- 优先级：P0
- 状态：已部署 Panda，待长时间真实链路观察
- 备注/证据：2026-07-04 已在 `services/protocol/conversation.py` 中把带 `conversation_id` 的 `ImagePollTimeoutError` 转为 `image_timeout_pending`，不再换号重试；`test_image_task_service.py` 覆盖；生产 12 任务压测未出现 `timeout_pending`。

### OPS-005 Panda 生图 CPU 预算和 deadlock_guard

- 现象/问题：Panda CPU 仍有空闲，可以给 image 多 0.5 vCPU 预算；但 CPU 打到 90% 必须视为死锁/熔断，而不是继续压测。
- 建议方向：生图预算按 1.5 vCPU 设计；CPU p95 正常目标 <=70%，>80% 降档，>=90% 持续 60s 触发暂停新提交、暂停 maintenance、worker 降级和快拒绝。
- 优先级：P0
- 状态：已部署 Panda 基础熔断，待 CPU>=90% 人工触发验收
- 备注/证据：新增 `services/image_deadlock_guard_service.py`；maintenance 已接入 tripped pause；API 队列满/熔断返回 429；生产容器 CPU 限额已从 1.0 调整为 1.5 vCPU。尚未做生产 CPU>=90% 人工压测。

### IMG-004 24/30 异步接纳容量与轻量状态查询

- 现象/问题：R5.6 24 档中 24 个公网混合输入任务只有 22 个被接纳，2 个因 `image task queue is full for current user (20/20)` 返回 429；公网状态查询 p95 达数秒。
- 建议方向：把接纳容量和执行并发拆开；`per_user_queue_max` 从 20 提高到 36，但 `per_user_running_max` 先保持 2；新增轻量 status 查询接口，不返回 data / 图片 URL / 大字段。
- 优先级：P0
- 状态：**已部署基础修复；24/30 总体验未完成**
- 备注/证据：2026-07-04 已部署 Panda，备份 `/root/gptimage/backups/p6-queue36-status-20260704-210512/`；`per_user_queue_max=36`、`GET /api/image-tasks/status` 已生效。Stage C 24 复测未再出现 `20/20` 429，但 5 个大参考图请求在入队前连接失败，转入 IMG-005。

### IMG-005 大参考图上传与任务入队解耦

- 现象/问题：18/24 档真实参考图输入下，图生图公网 submit p95 达几十秒；不能通过输入减重解决。队列接纳上限修复后，Stage C 24 复测仍有 5 个大参考图请求在入队前 `ConnectionResetError(10054)` / `RemoteDisconnected`，另有 1 个已入队任务因 `/backend-api/files failed: status=500` 失败。
- 建议方向：保持图像质量不变，优先支持 multipart 文件上传，进一步支持两阶段提交：先上传 reference 得到 asset_id，再提交 async task；同时加上传窗口/上传并发保护。
- 优先级：P0
- 状态：**一期已部署 Panda；二期上传窗口待处理**
- 备注/证据：一期新增 `POST /api/image-assets/references`、`GET/DELETE /api/image-assets/references/{asset_id}`，`POST /api/image-tasks/edits` 支持 `asset_ids[]`；生产备份 `/root/gptimage/backups/img005-assets-phase1-20260705-163634/`；3 轮 24 两阶段压测 `asset_upload=48/48`、`submit=72/72`，submit p95 max≈2.31s，报告 `reports/img005-stage24-3rounds-20260705-164948/aggregate-summary-corrected.json`。

### BUG-001 `ConfigStore.proxy_url` 续轮询错误

- 现象/问题：R5.6 24 档中一个已接纳任务失败：`'ConfigStore' object has no attribute 'proxy_url'`，发生在带 `conversation_id` 的续轮询路径。
- 建议方向：为 `ConfigStore` 增加兼容 `proxy_url` 属性，或在 `ImageTaskService._run_resume_poll()` 中改用 `config.get_public_proxy_runtime_settings().get("proxy_url")`。
- 优先级：P0
- 状态：**已完成并部署 Panda**
- 备注/证据：本地相关测试 21 passed；Panda 备份 `/root/gptimage/backups/p6-queue36-status-20260704-210512/`；容器运行时确认 `has_proxy_url=True`。

### OPS-006 本地 40080 / WSL / FlareSolverr 代理栈稳定性

- 现象/问题：网线环境下 WARP 上游导致 40080 大量 `CONNECT tunnel failed 503` / timeout；后续 WSL `HermesUbuntu` 进入 `CreateInstance/E_UNEXPECTED`，Docker privoxy/FlareSolverr 不可用。
- 建议方向：保留 40080 入口；WSL 正常时走 Docker privoxy + host proxy bridge；WSL 异常时启用 Windows 侧 `40080 -> 7897` 兜底；监控脚本不因 `Network: unstable` 单独重启；恢复 WSL 后复测 FlareSolverr payload 代理翻译为 `http://privoxy:8118`。
- 优先级：P0
- 状态：**主注册网络面已修复；FlareSolverr 清障链待 WSL 恢复后验收**
- 备注/证据：`scripts/warp_health_monitor.ps1`、`scripts/host_proxy_forwarder.py`、`scripts/start_proxy_stack_wsl.sh`、`scripts/privoxy-warp.conf`、`services/proxy_service.py`；40080 兜底连通性 5/5 可达；补丁后注册小验收 8/8 success。

### REG-001 TempMail.lol provider 级限速与 token exchange 诊断

- 现象/问题：40080 修复后，5 线程 / 50 次注册的主要 transient 变为 `TempMail.lol HTTP 429 Rate limited (free)`；真实失败中 token exchange 只记录 `token换取失败`，缺少 HTTP 状态和响应细节。
- 建议方向：对 `tempmail_lol` 的 `/inbox/create` 做全局串行限速和 429 全局退避；token exchange 失败记录脱敏后的 HTTP 细节。
- 优先级：P0
- 状态：**已完成本地修复并验收**
- 备注/证据：`services/register/mail_provider.py`、`services/register/openai_register.py`、`test/test_register_mail_provider.py`、`test/test_register_proxy_runtime.py`；补丁后 5线程/8次注册 `8/8 success, 0 transient`；受影响测试 `30 passed`。

### BUG-002 timeout_pending 续轮询构造 OpenAIBackendAPI 参数错误

- 现象/问题：2026-07-05 三轮 24 并发压测中，Round 3 有 4 个图生图任务进入 timeout_pending 续轮询后失败：`OpenAIBackendAPI.__init__() got an unexpected keyword argument 'proxy_url'`。
- 建议方向：`ImageTaskService._run_resume_poll()` 使用 `OpenAIBackendAPI()`，代理由 `OpenAIBackendAPI` 内部 `proxy_settings` 管理；部署后复测 timeout_pending 路径。
- 优先级：P0
- 状态：**已部署 Panda**
- 备注/证据：本地 `python -m pytest test/test_image_task_service.py test/test_image_tasks_api.py -q` 为 `14 passed`；Panda 备份 `/root/gptimage/backups/bug002-resume-poll-20260705-162726/`；IMG-005 生产验收未再出现 `unexpected keyword argument 'proxy_url'`。

### IMG-006 pre-conversation 上游 HTTP/2 长尾失败快收敛

- 现象/问题：2026-07-05 三轮 24 并发压测中，Round 1 / Round 2 各 1 个文生图任务失败：`curl: (92) HTTP/2 stream 1 was not closed cleanly: INTERNAL_ERROR`，无 `conversation_id`，且失败释放接近 30 分钟。
- 建议方向：区分 pre-conversation 与 post-conversation；pre-conversation 阶段设置更短硬超时/有限重试/账号或出口短期 backoff，避免单任务失败占用生成槽 30 分钟；post-conversation 仍走 timeout_pending 续轮询，不重开图。
- 优先级：P0
- 状态：**待处理**
- 备注/证据：IMG-005 两阶段 3 轮 24 压测中新增 4 个同类自然失败，均无 `conversation_id`；报告 `reports/img005-stage24-3rounds-20260705-164948/aggregate-summary-corrected.json`。

### IMG-007 post-conversation poll 超时与 token invalidated

- 现象/问题：IMG-005 两阶段 3 轮 24 压测中出现 4 个 `token invalidated during image poll task check`，均已有 `conversation_id`；当前 `image_poll_timeout_secs=120`，复杂图在 24 压力下容易先进入 timeout_pending，再在续轮询阶段撞上账号 5 分钟失效窗口。
- 建议方向：调大首轮 poll timeout 或按任务类型动态 timeout；对已拿到 `conversation_id` 的任务绑定更稳的续轮询策略，避免使用已失效账号继续查；token invalidated 时优先标记账号异常/刷新并保留任务可恢复状态，不应直接把已生成中的图判死。
- 优先级：P0
- 状态：**待处理**
- 备注/证据：`reports/img005-stage24-3rounds-20260705-164948/aggregate-summary-corrected.json`；Panda 日志 `image_poll_timeout_pending` 后接 `image_poll_token_invalid_retry`。

## 2026-07-05 生图三项优化后续待办校准

### IMG-005 二期上传窗口

- 状态：**已部署 Panda**。
- 证据：`/root/gptimage/backups/img006-007-005p2-20260705-194447/`；三轮 24 中 reference asset 上传 `48/48` 成功，submit `72/72` 成功。
- 后续：继续观察 96MiB bytes-inflight 是否足够；当前不是主要瓶颈。

### IMG-006 pre-conversation 快收敛

- 状态：**已部署 Panda，仍需继续观察**。
- 证据：日志出现 `image_pre_conversation_transient_retry`，HTTP/2 INTERNAL_ERROR 被限制为有限 retry，不再无界 30min 占槽。
- 后续：继续统计 exhausted 次数；若仍多，应查上游出口/账号，而不是加 worker。

### IMG-007 post-conversation poll 策略

- 状态：**已部署 Panda，生效但带来更长占槽**。
- 证据：日志显示 generation=180s、edit=300s、多参考=360s；timeout_pending 能被 poll worker 接管。
- 后续：当前主要失败已转为 `no available image quota (tried 8 tokens)`，下一步应调 `image_token_max_attempts`。

### IMG-008 扩大生图候选 token 尝试面

- 现象/问题：三轮 24 复测中 `no available image quota (tried 8 tokens)` 共 23 次；生产 `image_token_max_attempts=8`，每任务最多只抽 8 个账号。
- 建议方向：先做配置 A/B：8 -> 24/32，单轮 24 验收；观察成功率、preflight 请求量、账号限流增量和 Panda CPU/带宽。
- 优先级：P0
- 状态：待处理
- 证据：`reports/img006-007-005p2-stage24-3rounds-combined-20260705/aggregate-summary.json`

### BUG-003 ImageTaskService 只读导入会恢复未完成任务

- 现象/问题：生产排查时执行 docker compose exec ... import image_task_service 会触发 ImageTaskService.__init__() 内的 _recover_unfinished_locked()，把运行中任务误判为“服务重启”并改写 DB。
- 风险：只读检查会污染正在压测/生产运行的图片任务，造成 queued/timeout_pending/error 假象。
- 修复：构造/导入只读加载；runtime recovery 只在 start_background() / worker 启动前执行一次。
- 优先级：P0
- 状态：**已部署 Panda**
- 证据：本地 19 passed；生产备份 /root/gptimage/backups/bug003-image-task-import-side-effect-20260705-235012/。

### IMG-008 扩大生图候选 token 尝试面

- 现象/问题：三轮 24 复测中 
o available image quota (tried 8 tokens) 共 23 次；8 个候选 token 抽样面过窄。
- 已执行：生产 image_token_max_attempts=24，单轮 24 A/B 完成。
- 结果：上传 16/16、入队 24/24、最终 23 success / 1 error；
o available image quota 降为 0。
- 代价：账号池 pre/post active 96 -> 88，quota 438 -> 385，schedulable 96 -> 87；扩大候选面会更快发现/标记坏号与限流号。
- 状态：**第一档已部署并验证；暂不升 32**
- 下一步：水位恢复后再 A/B per_user_running_max 2 -> 3；不直接进 30。

### IMG-009 Health schedulable 未扣除 preflight backoff / 真实失败候选

- 现象/问题：Panda health 显示 ctive/quota/schedulable 很高，但实际单任务仍可报 
o available image quota (tried 24 tokens)。
- 证据：2026-07-06 running=3 A/B 后，health 显示 160+ active，但 24 任务全部 no available；随后单任务 smoke 也 no available。
- 根因方向：health 的 schedulable 是账号静态状态口径，未暴露/扣除运行时 _image_preflight_failures backoff、刚被 preflight 判死但尚未被 maintenance 清理的候选。
- 建议方向：健康页/监控增加 preflight_backoff_count、
eady_candidate_count_after_backoff、最近 preflight 失败原因分布；压测准入以该真实候选面为准。
- 优先级：P0
- 状态：**已完成**（2026-07-06 Panda 已部署，health JSON/HTML 已验收；新增 ready/available/dispatchable/preflight/inflight/global limit 字段）

- 压测复核：2026-07-06 单轮 24 异步两阶段压测中，入队 23/24，已入队 23/23 success；唯一失败为公网 TLS handshake submit timeout，非 no available / Panda 资源瓶颈。

### MAINT-002 生图期间 maintenance 不应 normal 扫大批新号

- 现象/问题：补号后压测时，maintenance normal 模式批量验证并删除大量账号，和生图 preflight 抢同一批候选，导致 health 水位虚高但生图无号可用。
- 已执行：Panda 配置改为 inflight>=1 时进入 slow，不暂停：slow_batch_limit=5、slow_delay_between_accounts_sec=8、slow_cooldown_sec=30。
- 备份：/root/gptimage/backups/maint-slow-on-image-20260706-015303/
- 状态：**已部署配置，需继续观察**

### IMG-010 同步兼容入口超过 6 立即 502

- 现象/问题：OpenAI/NewAPI 兼容同步入口当前 `image_global_concurrency=6` 且 `image_global_queue_timeout_secs=0`，超过 6 会立即返回 `image service busy`/502。
- 证据：2026-07-06 IMG-009 部署后 health 已暴露 `image_global_concurrency_limit=6`、`image_global_queue_timeout_secs=0.0`。
- 判断：该入口与 `/api/image-tasks` 异步队列不同，不应直接拉到 24；公共同步长连接做无限队列会放大尾流。
- 建议方向：单独 A/B，先设置短等待 `8~10s`，再小步测试 `6 -> 8`；同时保留异步队列作为高并发入口。
- 优先级：P1
- 状态：待处理

## 2026-07-06 新增：IMG-012 上游调用可取消隔离

- 背景：IMG-011 hard timeout 能把 unning + conversation_id=false 任务置为 error，避免无限占槽；但底层阻塞 I/O 只能靠 daemon 线程自然返回，仍不是真正 kill。
- 建议：把 OpenAI backend 生图调用放进可终止的子进程/进程池，父进程按 hard timeout kill worker；或在 curl_cffi/requests 层找到可验证的低速/总墙钟取消机制。
- 验收：构造阻塞上游调用后，任务在阈值内 error，worker/线程/账号级 inflight 不残留；24×3 压测 inal_unfinished_total=0 且 image_inflight_count=0。

### IMG-012 NewAPI 同步入口内部异步化与多阶段传输流水线

- 现象/问题：NewAPI 仍走 Panda `/v1/images/*` 同步兼容入口；该入口受 `image_global_concurrency=6` 与 `image_global_queue_timeout_secs=0` 影响，超过 6 个并发会直接出现 `image service busy: global concurrency limit 6 reached`，导致 NewAPI 侧 502/503。另一方面，Panda 的 `/api/image-tasks` 已能稳定接 24 个异步任务，但 NewAPI 标准调用看不到这条异步链路的成功用量日志。
- 建议方向：把 `/v1/images/generations` 和 `/v1/images/edits` 改成内部 `sync-over-async`：请求进入后创建 `image_task`，后台按 `6` 常态、健康时 `burst=8` 生成；结果下载、b64/url 组装和回传使用独立窗口；同步 HTTP 请求只等待自己的 task 结果并返回 OpenAI 兼容响应。详细方案见 `docs/08-image-pipeline-newapi-async-plan.md`。
- 优先级：P0
- 状态：**待处理，方案已记录，代码未实现，生产未部署**
- 关键参数建议：
  - `upstream_generating_base=6`
  - `upstream_generating_burst=8`
  - `result_download_window=2~3`
  - `client_response_window=2~3`
  - `bandwidth_soft_limit=20Mbps`
  - `bandwidth_hard_limit=24Mbps`
  - `bandwidth_emergency_limit=28Mbps`
- 验收标准：
  - NewAPI 侧用标准 `/v1/images/*` 发起 24 并发，不能再出现同步入口 `global concurrency limit 6 reached` 直接 busy。
  - 24 混合输入三轮：`final_success >= 70/72`、`unfinished=0`、`image_inflight_count` 最终为 0。
  - Panda `bandwidth p95 < 24Mbps`，`5s max < 30Mbps`，`>=24Mbps` 不连续超过 60 秒。
  - NewAPI 侧能看到对应生图调用日志。
- 备注/证据：当前基线为 IMG-011：`reports/img005-stage24-3rounds-20260706-125316/aggregate-summary.json`，3 轮 24 共 `70/72 success`，但后台单用户 running≈2，clean 轮 24 入队后全完成约 12.4 分钟。
