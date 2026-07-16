# 改进池

最后校准：2026-07-16

原则：只保留当前仍有工程价值的项；已完成和历史流水不放在这里，详见 `docs/logs/2026/2026-07.md` 与 `docs/archive/`。

## 当前主线

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

- **状态**：进行中。
- **背景**：2026-07-16 已显式删除 75 条 `account_deactivated` 终态 Outlook；当前 Panda 保留 18 条非终态 Outlook，其中 5 条可调度。终态删除前有完整 SQLite 回滚点。
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

### SCHED-001 账号调度可观测性与失败归因

- **状态**：建议优先级中高；先做观测，不先重写调度。
- **当前已有能力**：
  - 候选过滤：状态、额度、失败证据、Panda 接收态、preflight backoff。
  - 并发闸门：全局并发、单账号并发、per-user running、queue cap。
  - 运行态指标：`ready_candidate_count`、`available_candidate_count`、`dispatchable_candidate_count`、`preflight_backoff_count`、`image_inflight_count`。
- **缺口**：
  - `no available image quota` 还缺少结构化 breakdown，无法一眼看出是状态异常、失败证据、接收态、backoff、并发占用还是最近额度过期。
  - preflight 失败只在内存 backoff，重启后诊断丢失。
  - 任务失败和账号失败的因果链还不够清晰，容易把上游异常误解成账号死亡。
- **建议实现**：
  1. 新增候选排除原因统计：`excluded_by_status`、`excluded_by_failure_evidence`、`excluded_by_receive_state`、`excluded_by_backoff`、`excluded_by_inflight`、`excluded_by_quota_freshness`。
  2. 在 `no available image quota` 错误里带轻量 breakdown，避免只返回总错误。
  3. 给 preflight backoff 增加可选持久化或短期日志，不直接改账号状态。
  4. 加测试覆盖候选排除原因，防止调度口径漂移。
- **验收**：
  - health 或管理接口能解释可调度数为什么小。
  - 出现 0 候选时能明确是哪类原因最多。
  - 不增加自动删号风险。

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

- **状态**：设计完成、工程待落地；详细评分口径和实施顺序见 `09-outlook-longevity-99-plan.md`。
- **节点基线**：10 个释放 Webshare 节点经三轮网络与 ChatGPT 入口复测后分为 `READY=5 / OBSERVE=3 / QUARANTINED=2`；当前只允许 READY 节点进入新账号 canary。
- **当前缺口**：缺少节点租约表、账号 cohort、成熟期事件、文本/生图独立策略域、图片容量保护、401/403/429 鉴权状态机及节点/账号/任务三层指标。
- **实施顺序**：
  1. P0：先落 `proxy_nodes`、固定绑定校验、节点分层、单 canary、`1h/6h/24h/72h/7d` 成熟期和 cohort 熔断。
  2. P1：增加 `AccountWorkloadPolicyService`，维护独立文本/生图速率域、文本 lease、图片容量保护、共享健康门禁和结构化指标。
  3. P2：管理页展示节点租约、账号成熟度、熔断原因、剩余预算和生命周期时间线；补齐 7/30 日留存报表。
- **验收**：节点绑定不可静默迁移；新批次按 `1 → 2 → 3` 扩容；任一 terminal/连续鉴权失败均触发 cohort 停止；成熟前不进入生产池；每次状态变化均有审计事件和可回滚备份。

### ACC-010 ChatGPT Web 聊天/生图逆向链路一致性审计

- **状态**：本地核心项已落地；待 GitHub→Panda。上线路径：本地验收 → push → `git pull` + recreate；**禁止** Panda build / scp 直推。
- **实时证据**（22:09 SSH）：`schedulable=0 / verified_total_quota=0`；fp/egress 0/18；12 条同代理签名；Panda 缺 `account_fingerprint.py`；详见深检报告与 `account-identity-remediation-panda18`。
- **本地已落地**：API/resource Session 隔离；`/me` fail-fast；空 conduit 失败；fp ensure/persist；去掉「最新对话」兜底；`request_shape` 脱敏 header hash；身份门禁与上传校验。
- **仍待**：poll GET 预算硬上限、单一重试协调器、阶段耗时全链路、生产代码 hash 对账（部署后）。
- **验收**：跨域敏感头 0；会话串线 0；已有 conversation 的重复生成 0；poll 不越预算；成功/异常/取消后 FD、线程、Session和slot回到基线。

### ACC-008 正常状态但 invalid 证据账号的恢复闭环

- **状态**：本地已修；待 Git 部署到 Panda 后观察。
- **问题**：4 条账号当前为 `status=正常 + quota>0 + verified_ready`，但最新 `/backend-api/me` 已返回 `token invalidated`。调度器正确按失败证据隔离；自动 Outlook 恢复却只选择 `异常/rejected`，导致 `candidate_count=0`，这些账号在二次确认前既不可调度也不进入恢复。
- **已实现**：
  1. 保留 30 秒 invalid 确认窗与 10 分钟新号宽限。
  2. 超过确认窗的「正常+invalid」纳入 `is_outlook_auto_recovery_candidate`。
  3. `test_outlook_auto_recovery_loop` 覆盖确认窗内/窗外行为。
- **验收**：部署后不再出现长期 `schedulable=0` 且恢复 `candidate_count=0`、同时仍有陈旧 invalid 的状态。

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

- **状态**：高优先级待做。
- **背景**：2026-07-15 Panda `image_tasks.db` 已约 `1.65GB`；终态任务包含 b64 结果会持续放大磁盘、重启和清理风险，历史上也曾导致 502/OOM。
- **方向**：
  - 继续保持轻量状态查询不读大 `data`。
  - 对 b64 结果做外置存储或更短保留期。
  - 增加 DB 大小、WAL 大小和终态任务数量告警。
- **验收**：
  - 重启不全量加载终态结果。
  - DB 增长可控。
  - `/api/image-tasks/status` p95 稳定。

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
