# 路线图

最后校准：2026-07-22

## Now

### 0. 严格纯 HTTP 生图（Sentinel / Turnstile）【P0】

- 目标：生产 curl_cffi 路径触发 `image_gen` 并出图；禁浏览器/FlareSolverr 作数据面。
- 当前事实：P1 finalize、P2 Turnstile VM、P3 strict auto-tool 均已打通；artifact `650e899084c3` 已正式部署 Panda。生产单账号/单 Webshare canary 已触发 `image_gen` 并成功下载 PNG `1254×1254`、`2,568,782` bytes（conversation `6a606849-e1b8-83ec-96e4-e7cfbbbf305b`）。期间 `/tasks` 出现 1 次 CF403，随后 conversation poll 恢复；未整单重试、未换号。详见 `docs/20`、`captures/spa/J-panda-production-pure-http-canary-20260722.json` / `04` **PROTO-PURE-HTTP**。
- 当前事实：同账号保持原 fp/session 已换绑到新 IP `45.39.75.27`；串行 5 续验执行 `4/5`，前 3 次成功，第 4 次在活跃 SSE 流中 45 秒内未识别到明确 `image_gen` 后止损。首页软失败 403 为 `4/4`，但 requirements/prepare/start 未传播 CF，前三轮 `/tasks` 无 CF，第四轮未进入 poll；因此不能把旧 IP 定性为唯一主因。
- 当前动作：先完成 SSE deadline/事件时间线与分阶段 CF 观测修复；45 秒保持验收线，诊断模式可在 gate fail 后继续只读监听同一流至 60 秒。完成前不补第 5 轮、不启动并发 4，也不重新提交整单。
- 完成标准：见 `20` §4（非空 Turnstile + `image_gen` + Panda 串行 5 / 并发 4 `no_image_gen=0`）。

### 1. Panda 低并发生图恢复观察

- 目标：确认恢复后真实业务不再被 `image_generation_paused` 或历史队列卡住。
- 当前事实：Panda `12/12` 可调度；refresh/maintenance 自动删 invalid 已关闭；IMG-017 conversation-ready deadline、post-ready 15 秒转轮询、hard-timeout cancel/timeout_pending 与 session executor 回收已部署。2026-07-10 直连同步 canary `78.91s` 成功，线程 `2 -> 2`，无 `CLOSE_WAIT`，`unfinished={}`。
- 完成标准：
  - 真实业务低并发连续运行稳定
  - `image_tasks.db` 无 queued/running 残留
  - 日志无 502/524、Traceback、`image service busy` 大量出现
  - NewAPI/Relai 无长期排队

### 2. 恢复 Panda clean 可调度池

- 目标：把 Panda 可调度面从当前 12 个 clean 账号继续恢复到可支撑业务的安全水位。
- 当前事实：2026-07-22 最新健康为 `total=14 / schedulable=11 / dispatchable=11`；`iv***3` 已通过 Panda 转发 Webshare + 本机 Camoufox OTP 换新 token，并在 Panda `/backend-api/me` 验证 quota 25 后安全替换旧 token。Outlook `token invalidated` 不能清证据硬恢复；其余目标仍须逐号执行同一“新 token 隔离 → Panda 实测 → 删除旧 token”流程。
- 完成标准：
  - 从本地 clean ready 池自动或手动补 Panda，成功 ACK 后本地删除
  - Outlook invalid 只允许新 token 经过 Panda 实测后再入池，UI/CLI 失败必须保留旧记录
  - `dispatchable_candidate_count` 明显提升
  - `panda_incoming_count` 不持续堆积
  - 同步失败仍留本地主池，不回到 pending

### 3. 账号调度可观测性

- 目标：让 `no available image quota` 能解释清楚具体排除原因。
- 当前事实：上传链路已先暴露 details，可解释 eligible、远端缺失重传、已在远端、水位/配置/失败证据阻断；生图调度 breakdown 仍待补。
- 完成标准：
  - health 或管理接口暴露候选排除 breakdown
  - 调度失败日志能区分状态、失败证据、接收态、backoff、并发占用、额度新鲜度
  - 不引入自动删号风险

## Next

### 1. SQLite mmap / cache / checkpoint 小步实验

- 目标：评估 SQLite 读性能优化是否对账号页、health、任务状态查询有实际收益。
- 当前事实：已有 WAL / NORMAL / busy_timeout；未启 mmap。
- 完成标准：
  - 本地和 Panda 小流量数据证明 p95 或 CPU sys 下降
  - RSS / 容器内存不接近 85% 墙
  - 无 `database locked` 增加

### 2. image_tasks.db 结果瘦身

- 目标：避免终态任务大结果继续推高 DB 体积，复发启动 OOM/502。
- 完成标准：
  - 终态任务不被启动全量加载
  - 状态查询不读 b64 大字段
  - DB/WAL 大小有监控或保留期策略

### 3. NewAPI 异步适配/回调

- 目标：解决 NewAPI/Cloudflare 同步长连接在 24 路以上断流的问题。
- 当前事实：Panda 后台队列能力不等于同步入口体验稳定。
- 完成标准：NewAPI 侧以异步 task/callback 或短等待 admission 控制承载高并发。

## Later

### 1. 调度升档策略

- 目标：在 clean 可调度池恢复后，逐步评估 `submit_workers`、`per_user_running_max`、`image_global_concurrency` 和 burst。
- 完成标准：每次只改一个变量，并有 before/after 资源、成功率、账号消耗数据。

### 2. 文档自动化

- 目标：防止当前事实再次被历史流水淹没。
- 完成标准：主状态短版、月度日志流水、archive 追溯三层持续执行。
