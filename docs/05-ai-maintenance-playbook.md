# AI 接手规则

## 接手阅读顺序

1. `docs/README.md`
2. `docs/02-current-state.md`
3. `docs/05-ai-maintenance-playbook.md`
4. 若要改路线图，再读 `03-roadmap.md`
5. 若要改待办，再读 `04-improvement-backlog.md`
6. 若要延续上次工作，再读 `docs/logs/YYYY/YYYY-MM.md` 和 `docs/06-handoff.md`

## 信息来源优先级

从高到低：

1. 代码、配置、测试、数据库、命令结果
2. `docs/02-current-state.md`
3. `docs/03-roadmap.md`
4. `docs/06-handoff.md`
5. `docs/logs/`
6. 根目录 `README.md`
7. 其他专题文档

## 更新纪律

- 先确认事实，再写文档。
- 如果代码和文档冲突，先以代码与验证结果为准，再修文档。
- 不把“我猜”写成“已确认”。
- 不把旧日志回改成新事实。
- 新增事实先写 `02-current-state.md`。
- 路线变化写 `03-roadmap.md`。
- 长期技术债和待办写 `04-improvement-backlog.md`。
- 交接摘要写 `06-handoff.md`。
- 每次会话结束都追加月度日志。

## 不确定项写法

- 能验证但还没验证的：标成“待确认”
- 只有推断、没有证据的：标成“推测”
- 已经被新代码覆盖的旧结论：标成“已过时”

## 常见高风险链路

这个项目里最容易出问题的链路是：

- 注册
- 刷新
- 账号失效判断
- Panda / 远端同步
- 本地删除
- 代理 / Cloudflare / 网络抖动
- 额度语义和健康页展示

改这些链路时，必须补测试或命令验证。额度相关改动必须先读 `docs/quota-semantics.md`。

### 纯 HTTP 生图 canary 规则（2026-07-22）

- 先读 `docs/20-pure-http-image-sentinel-todo.md` 与 `docs/captures/spa/H-pure-http-sentinel-fix-20260722.md`。
- conversation_id 只表示提交被接受，不能作为生图成功；必须看到 image tool/file，并完成下载验证。
- 同一账号/出口遇 CF403 后立即停止探针；不得在 requirements 内三连或并发追加请求。
- CF 必须分阶段记录：首页 `home_soft_fail status=403` 单列为软失败信号，不能与 requirements/start/tasks 抛出的业务 CF 或 `propagated_cf` 混为一个 `cf403` 总数。
- TLS/socket 瞬断只允许有限 session 重建，并保持 proxy、verify、impersonate 与 timeout，不得在重试中漂移出口/指纹。
- SSE deadline 检查必须发生在当前行解析之后，避免 45 秒边界刚到达的工具事件被丢弃；诊断证据仅保存脱敏事件元数据，不保存 token/cookie。
- 45 秒继续作为验收线；诊断模式可在 gate fail 后只读监听**同一 SSE 流**至 60 秒并标记迟到事件，这不算整单重试，但禁止重新提交 conversation。
- Panda 验收前必须先本地单次 canary；远端执行遵守容量预检、单单元、硬资源限制与失败即停。

## 会话结束必须回写什么

根据本轮有没有新事实，至少回写其中几项：

- `02-current-state.md`
- `03-roadmap.md`
- `04-improvement-backlog.md`
- `06-handoff.md`
- `docs/logs/YYYY/YYYY-MM.md`

如果只是问答、没有新事实，可以不改全部文件，但要确认现状没有被错误改写。

## 月度日志写法

每条日志统一写这几项：

- 日期
- 目标
- 观察到的事实
- 已完成
- 未完成
- 下一步
- 关联文件 / 命令

## 交接摘要写法

交接摘要只写短内容：

- 当前做到哪一步
- 哪些事实已经验证
- 哪些地方还要继续看
- 下一位 AI 先做什么



## 账号池性能升级接手规则（2026-07-03 新增）

如果任务涉及账号池、Panda 同步、SQLite 迁移、maintenance、生图并发或 b64 回传，必须先读：

1. 根目录 `../plan.md`
2. `docs/07-account-pool-performance-upgrade.md`
3. `docs/sync-strategy.md`
4. `docs/performance-acceptance-test-plan.md`

执行纪律：

- 生产改动前必须备份并写明回滚命令。
- 不因本地公网 IP 变化而使用固定 IP allowlist 作为唯一保护。
- `canvas.best` 按外部公共调用处理，不写成内部无线画布队列。
- 新号上传 Panda 前必须经过 `1h/3h/6h` 三次探测。
- SQLite 迁移必须保留 raw_json、账号数校验、随机字段抽样和 JSON 快照回滚。
- 并发/RPM/CPU 推荐必须基于优化后 R5/R7 测试数据，不沿用旧污染数据。



## 生图并发与 CPU 熔断规则（2026-07-04 新增）

涉及生图 worker、异步队列、b64 回传或 maintenance 争用时，必须遵守：

- 同步入口不做无限队列；公共 API 满载时快拒绝。
- 异步队列必须持久化任务状态，不能提交即开无限线程。
- 拿到 `conversation_id` 后，poll timeout 不允许换号重开图，必须进入 `timeout_pending` 续轮询。
- Panda 生图 CPU 预算可按 `1.5 vCPU` 设计，但 `CPU >= 90%` 是 deadlock_guard 熔断线，不能作为正常运行目标。
- 调高 worker 前必须有 R5/R5.5/R7 验收数据。
- 不允许服务模块 import 时启动注册、生图、同步等外部副作用；后台任务必须放到应用 lifespan 或显式 start 方法中。


## 18 / 24 / 30 压测与后续优化规则（2026-07-04 新增）

涉及下一轮生图容量优化时，先执行 `docs/performance-acceptance-test-plan.md` 的 R5.6：

```text
18 async tasks -> 24 async tasks -> 30 async tasks
```

执行纪律：

- 18 / 24 / 30 是任务提交数，不是 worker 数。
- 每档必须采集压测前、压测中、压测后的 CPU、内存、BlockIO、健康页延迟、任务延迟、账号池变化、错误日志。
- “网络”必须拆成明确的 **带宽** 指标：`bandwidth_rx_mbps`、`bandwidth_tx_mbps`、`bandwidth_total_mbps`。
- 30Mbps 公网带宽下，`bandwidth_total_mbps >= 24Mbps` 持续 60s 视为接近饱和，不能进入下一档。
- 不允许通过输入减重换取性能数字；prompt、参考图、mask、多图输入必须保持真实业务形态。
- 账号长期质量分层不作为主方案；当前只按短期状态和短期 backoff 调度。
- 减少无效重试可以做，但必须以 R5.6 的 duplicate submit / attempt 分布 / timeout_pending 数据为依据。
- R5.6 报告出来前，不给最终 worker、RPM、CPU、桶数、多出口建议。

## 本地 40080 注册代理排障规则（2026-07-04 新增）

涉及本地注册机、40080、WSL、FlareSolverr、WARP 时：

- 先检查 `127.0.0.1:40080`、`127.0.0.1:7897`、`127.0.0.1:8191` 监听状态，再判断注册成功率。
- `WARP Network: unstable` 不能单独作为重启条件；必须结合 40080、OpenAI、TempMail、FlareSolverr 实际探测结果。
- WSL/Docker 正常时，40080 完整链路是 Docker privoxy + host proxy bridge；FlareSolverr 容器内代理必须使用 `http://privoxy:8118`，不能传 Windows `127.0.0.1:40080`。
- WSL 进入 `CreateInstance/E_UNEXPECTED` 且当前进程无管理员权限时，只能先启用 Windows 侧 `40080 -> 7897` 兜底恢复注册主链路；这不等于 FlareSolverr 清障链已恢复。
- TempMail.lol 429 是 provider rate limit，不再归因成普通网络抖动；先看 provider 级限速和 429 backoff 是否生效。
- 若注册服务 `enabled=true` 但 `127.0.0.1:40080` 未监听，先调用 `/api/register/stop` 止损，再恢复 40080；不要让 20 线程继续把 `curl: (7)` 刷成真实 fail。
- `curl: (7) Failed to connect` 属于代理/网络瞬断类 transient；本地 loopback 代理端口不可用时，注册服务启动前应拒绝自启并保持 `enabled=false`。

## 本地 WSL 代理栈 safe mode 规则（2026-07-05 新增）

涉及 `scripts/start_proxy_stack.ps1`、`scripts/start_proxy_stack_wsl.sh`、`scripts/warp_health_monitor.ps1` 时：

- 不要把 WSL / Docker 不可用当成可以反复重启的普通瞬断。
- `start_proxy_stack.ps1` 应先做 WSL 项目路径、`docker`、`docker info`、`docker compose` preflight；不通过就快速失败。
- `start_proxy_stack_wsl.sh` 默认不自动启动 Docker/dockerd；只有明确设置 `GPTIMAGE_WSL_ALLOW_DOCKER_START=true` 才允许尝试启动 Docker。
- `warp_health_monitor.ps1` 重启代理栈前必须检查 WSL Docker 条件；不满足时记录 `proxy_stack_restart_skipped` 并 sleep，不循环打 WSL。
- WSL Docker / FlareSolverr 不可用时，优先保持 Windows 侧 `127.0.0.1:40080 -> 127.0.0.1:7897` 兜底，先保证注册主链路，不要为了清障链破坏 WSL。

## 本地 WSL Docker / WARP 代理链接手规则（2026-07-05）

当前本地主注册代理链应按下面事实判断：

- 主链路：`40080 -> Docker privoxy -> warp-proxy:1080 -> WARP`。
- FlareSolverr：`127.0.0.1:8191`，用于后续 Cloudflare clearance 基础能力。
- Windows `host_proxy_forwarder.py 40080 -> 7897` 只作为应急兜底；如果它占用 40080，会挡住 Docker privoxy，不能把它误认为旧 WARP 链已恢复。

排障顺序固定为：

1. 先看运行态 `/api/register` 的 proxy 是否为 `http://127.0.0.1:40080`，以及注册任务是否停止。
2. 再看 40080 端口归属：Docker privoxy 优先，Windows host forwarder 只能算兜底。
3. 再看 WSL `HermesUbuntu` 内 Docker daemon 是否运行。
4. 再看 compose 容器 `warp-proxy/privoxy/flaresolverr` 是否 healthy。
5. 再用 Cloudflare trace 验证 `warp=on`，用 TempMail API 验证邮箱 provider 出口。
6. 最后再考虑小规模注册复测。

Docker Hub 拉镜像超时时不要只测 WSL `curl`。普通 shell 能走代理不代表 `dockerd` 会走代理；要确认 `dockerd` 启动环境里有 HTTP/HTTPS proxy。当前 `scripts/start_proxy_stack_wsl.sh` 已在显式允许启动 dockerd 时保留代理环境。

恢复命令入口以 `scripts/start_proxy_stack.ps1 -AllowDockerStart` 为准；默认不硬启 Docker，避免 WSL 异常时反复打系统服务。

### CloudflareTempMail 配置与 secret 规则（2026-07-14）

- 当前 canonical API 为 `https://email-api.relai.asia`；`https://temp-email-api.relai.asia` 是同一 Worker 的备用入口。
- 当前注册机只启用 Worker `/` 健康接口明确返回的六个 `.asia` 域名：`relai.asia`、`edu`、`mail`、`verify`、`auth`、`account`。`signup/grok/api/img/gpt` 虽有 Email Routing 规则，但未列入 Worker domain 清单，未做创建 canary 前不要加入 provider。
- `cloudflare_temp_email` 的 `admin_password`、其他邮箱 provider 的 `api_key/ddg_token/cf_inbox_jwt/cf_api_key` 都是只写字段；`GET /api/register` 与 SSE 只能返回空值和 `*_configured=true/false`。
- 前端把脱敏空值 POST 回来时，后端必须保留同下标、同 provider type 的已存 secret；不得用空字符串覆盖。
- 配置验证顺序固定为：创建邮箱 → `/admin/get_address` 恢复 JWT → `/api/mails` 读取；先验证空收件箱，再做真实 OpenAI OTP canary。
- 注册日志不得记录 OTP 正文；只能记录验证码位数与“内容已脱敏”。排障需要证明收码时，看“发送完成 / 收到验证码 / 校验完成”阶段，不复制验证码。
- Cloudflare Global API Key 只用于当次盘点，禁止写入仓库、配置、日志或维护文档；完成迁移后应轮换。
- Worker 当前使用源码默认管理员口令且没有 secret binding。轮换前先审计其他消费者，随后改为 Worker secret，不能直接改默认值造成全局断链。


## 本地 Panda staging 水位驱动补池规则（2026-07-06）

涉及本地注册号进入 Panda 前的 staging/ready/upload 链路时：

- 不要只看 `staging` 数量；必须同时看 `ready`、Panda `schedulable/active/total` 和 `last_upload`。
- Panda 低水位但本地 `ready` 已大量积压时，优先排查上传间隔/批量/远端导入限频，不要先扩大探活。
- 当前本地策略：normal `30/120/360min`，low `10/30/90min`，emergency `5/15/45min`，仍需三次 OK 才 ready。
- 当前远端导入保护单批仍为 `20`；如果要把 `low_upload_max_batch` / `emergency_upload_max_batch` 提到 20 以上，必须同步确认生产 Panda 的 `public_import_max_batch_size`，否则会触发 `413`。
- 低水位且 ready backlog 足够时，staging 循环应先上传 ready，并跳过本轮大探活，避免探活 timeout 拖住补池。
- `/api/accounts/panda-staging/status` 不允许返回完整 token；只能返回 `anonymize_token()` 后的值。

### 本地 clean 自动上传补充规则（2026-07-08）

- 当前保号池策略下，不启用 `panda_staging_service`；本轮改为通过 `account_refresh_all_service` 的受控上传路径执行。
- 后续 clean 号自动上传走 `account_refresh_all_service` 的 `panda_sync` 路径。
- 当前策略为 `panda_sync.enabled=true`、`remove_local_on_success=true`、`queue_on_failure=false`、`staging_enabled=false`；成功 ACK 后本地删除。
- `/api/accounts/sync/panda` 也必须走 `account_refresh_all_service.queue_available_accounts_for_panda()`，不要再调用旧 `scripts/sync_accounts_delta_to_panda.ps1`。
- 如果 `remove_local_on_success=false`，同步 ACK 后本地账号应标记为 `panda_sync_state=synced`，避免重复上传把 Panda 侧已验证账号改回 `incoming`。
- 如果旧留存账号是 `synced` 但远端 token 快照不存在，该账号允许重传；这类情况要在同步 details 中看 `remote_missing_reupload`。
- 已 `synced` 且 token 未轮换的账号，后续刷新成功不应重新改为 `ready`。
- 账号页上传/接收可视化字段以 `services/account_service.py:get_stats()` 和 `/api/accounts/activity/daily` 为准：`panda_upload_eligible_count`、`panda_upload_queue_count`、`panda_upload_unsynced_eligible_count`、`panda_upload_retained_count`、`panda_upload_remote_pending_count`、`panda_upload_remote_verified_count`、`panda_upload_remote_rejected_count`、`panda_upload_blocked_count`。
- 本地节点 UI 文案使用“上传”；Panda 节点 UI 文案使用“接收”。不要在 Panda 页面显示自动上传开关。
- 开关自动上传必须走 `/api/accounts/panda-sync`，只更新 `panda_sync.enabled` 并保留 secret；不要用 `/api/settings` 原样回写。
- `/api/accounts/sync/panda` 返回 details；遇到 `synced=0 failed=0 queued=0` 时先看 `already_remote`、`blocked_by_watermark`、`blocked_by_failure_evidence`、`blocked_by_config`。
- 注意 `/api/settings` 返回的是脱敏配置，`panda_sync.auth_key` 会被置空；不要把 GET 到的完整 settings 原样 POST 回去，否则会清空本地同步 key。需要改嵌套配置时读取真实 `config.json` 或使用保留 secret 的专用更新路径。
- Panda 导入 ACK 不等于远端最终可用；Panda maintenance 可能立刻 verify 并删除失败 incoming。补池后必须同时看 Panda 的 `incoming/verified/removed/total`。

### 异常账号删除与 Outlook 恢复规则（2026-07-10 校准）

- 当前生产必须保持 `account_refresh_all.delete_invalid=false`、`account_maintenance_loop.delete_invalid=false`、`auto_remove_invalid_accounts=false`、`auto_remove_rate_limited_accounts=false`。2026-07-09 曾因前端请求覆盖配置误删 10 个 Outlook，禁止恢复旧的自动删号口径。
- 不允许仅凭旧 quota 或 `panda_rejected` 直接清失败证据入池；Outlook invalid 账号必须重新登录换新 token，并经过同一 Webshare 的 csrf 预检和 Panda `/backend-api` 实测。
- 账号页行尾 `RefreshCw`：正常账号继续刷新额度；异常/rejected Outlook 执行完整恢复链。后端入口为 `POST /api/accounts/recover-outlook`，进度入口为 `GET /api/accounts/recover-outlook/progress/{id}`。
- 页面恢复 secret 固定为 Panda 本机 `/root/gptimage/data/runlogs/panda-outlook-recovery.credentials.secret.txt`，权限 `600`；对应容器路径为 `/app/data/runlogs/...`。不得提交仓库、不得写入日志或报告。
- 同一时间只允许一个 UI 恢复任务。每次先在 `/root/gptimage/data/backups/` 建备份；新 token 先隔离写入，验证成功后才删除旧 token；失败必须回滚新 token 并保留旧记录。
- 本机直连 Webshare 若在 ChatGPT callback/session 出现 `connection reset`，先用同节点做“本机 → Panda → Webshare”A/B；2026-07-22 已实测 5 个节点直连全 reset、经 Panda 转发全为 session/CSRF 200。Camoufox 重登必须先走 ChatGPT NextAuth signin，取得匹配的 state Cookie 和 authorize URL后再进 OTP；禁止手工随机 `state` 直开 `auth.openai.com`，否则 callback 会落 `/auth/error`。OTP 后回到 `chatgpt.com/` 时应读取 `/api/auth/session`，不能继续只等 URL code/refresh token。
- 外部恢复子进程完成后调用 `account_service.reload_from_storage()`；不要为单号恢复强制重启整个 app。
- CLI 兜底仍使用 `scripts/recover_panda_outlook_accounts.ps1`；批量删除或覆盖 SQLite 前仍必须另做生产备份。
- OpenAI OTP 校验若明确返回 `code=account_deactivated`，这是上游终态，不是代理、邮箱 RT、OTP 读取或密码缺失的 transient。不得继续密码重置或周期重试，也不得因此删除账号记录。
- 终态账号必须持久化为 `status=禁用`、`outlook_recovery_state=terminal`、`outlook_recovery_terminal_reason=account_deactivated`。手动恢复校验、普通/批量 `AccountService.refresh_accounts`、`re_login_accounts`、CLI 目标选择、Outlook 自动恢复候选和 refresh-all/maintenance（包括 `token_overrides`）都必须排除它。
- 历史 `limits_progress` 可能让禁用账号仍显示旧 quota；调度口径以 `status=禁用` 和 terminal 标记为准。不要为了把 UI quota 清零而清除失败证据或恢复调度资格。
- 只有 OpenAI 官方恢复账号后，才允许重新登录取得新 token、重新导入并人工清除 terminal；不能只清 terminal 字段后继续撞同一停用账号。

### Outlook 批次风控与注册止损规则（2026-07-15）

- 判断号池时必须拆开 `terminal / limited / quota=0 / invalid evidence / schedulable / dispatchable`。`active`、账面 quota 和账号总数都不能代表真实可用面；终态记录可能保留旧 quota。
- `account_deactivated` 的决定性证据必须来自 OpenAI 明确响应；只出现 `token invalidated` 时先走现有二次确认和 Outlook OTP 恢复，不得提前写 terminal。
- 同一恢复链出现“部分成功、部分 `account_deactivated`”时，说明邮箱、OTP 和代理技术链可用，失败账号属于上游终态；不要继续把 `missing_openai_password`、Graph/IMAP、Panda 内存或 Webshare 当成根因反复排查。
- Outlook/其他真实外部注册必须先单 canary，并经过 `1h/6h/24h` 成熟探活；没有成熟证据不得直接扩大到整批，也不得注册成功后立即进入 Panda 生产调度。
- 注册 cohort 在滚动窗口内出现 2 条明确 `account_deactivated` 时，应立即停止该 cohort 的注册、上传和扩容。先保存脱敏证据、统计批次关联字段，再决定是否恢复；不得继续消耗剩余凭据验证阈值。
- `127.0.0.1:40080` 只代表共享 WARP 入口可达，不代表公网出口粘性。已实测同一容器短窗口出口哈希变化；文档和代码不得再称其为“固定 IP”。
- 不得通过批量换浏览器指纹、随机 device/IP、频繁重连或跨环境搬运来规避上游风控。合法账号应尽量保持注册、首登、首次验号和长期运行环境稳定；生产规模优先使用官方 API 的组织/项目和付费额度。
- 浏览器每号新建 profile/device ID 只能证明没有直接复用 Cookie，不等于批次没有关联性；同一 Chromium 容器、版本、窗口尺寸、无痕模式、固定节奏和共享网络入口仍属于同质信号。
- `status=正常` 但存在明确 invalid 失败证据的账号必须继续排除调度；超过确认窗口后应进入 maintenance 二次确认或串行 Outlook 恢复候选。不能因为账面 quota>0 手工清失败证据，也不能让其长期停留在“不可调度且 candidate_count=0”。
- 终态增长审计至少记录：首/末 terminal 时间、每小时数量、created→rejected/terminal 延迟、cohort 终态率、是否成功生图、恢复报告中成功与 `account_deactivated` 的唯一 token 数。没有这些证据时，不把单一 IP、指纹或生图用量写成确定根因。

### 生图 SSE conversation-ready deadline 与取消规则（2026-07-10）

- curl_cffi 0.15 的流式 `Response.iter_lines()` 内部是无 timeout 的 `queue.get()`；`Response.close()` 会同步等待 `stream_task`。禁止再用 `threading.Timer + response.close()` 作为无首包中断手段。
- 图片 pre-conversation 必须直接从 `response.queue` 使用带墙钟 deadline 的 `get(timeout=...)`，并正确处理跨 chunk 的 SSE 行、`RequestException` 与 `STREAM_END`。
- **ready 条件必须是解析到真实 `conversation_id` 元数据，不是任意非空 `data:`。** ping/control/心跳 payload 只能向上游解析器透传，不能解除 45 秒 deadline。
- 捕获 `conversation_id` 后，SSE 只再保留 15 秒 post-ready 窗口；即使 control heartbeat 持续到达，也不得延长该墙钟 deadline。到期非阻塞 abort SSE，并让现有 `/backend-api/tasks` / conversation poll 接管。
- deadline 或 cancel 到期先设置 `quit_now`，底层 Future/curl 清理放守护线程；外层 finally 依赖 `_stream_closed` 幂等返回，不得再次同步等待。
- pre-conversation metadata timeout 属于 transient：当前 Panda `timeout=45s`、`max_attempts=2`，失败账号进入 backoff，再有限换号一次；不要把总 hard timeout 当首包超时。
- `cancel_event` 必须贯穿 `ImageTaskService -> generation/edit handler -> ConversationRequest -> OpenAIBackendAPI -> SSE/poll sleep`。hard timeout 设置事件后等待约 1 秒宽限，并记录 `runner_alive_after_cancel`；不能只改 DB 状态。
- hard timeout 前已捕获 `conversation_id` 时：保存 `conversation_id + resume_access_token`，释放 slot，任务转 `timeout_pending` 并后台续轮询；不得直接 `mark_image_result(False)` 或返回终态 502。未捕获会话时才沿用 backoff/mark-fail。
- `mark_image_result()` 自带 `release_image_slot()`；不得随后无条件再强释。取消异常的 slot 记账由 `ImageTaskService` 统一负责，底层 `ImageStreamCancelledError` 不得重复 mark。
- 每个 `OpenAIBackendAPI` 必须显式 `close()`；curl_cffi session 的 stream executor 要 `shutdown(wait=False, cancel_futures=True)`，防止 hard-timeout 后线程池和代理连接累积。
- 验收至少包含：持续 control heartbeat 不解除 pre-ready/post-ready deadline、cancel 能中断 queue 与 poll sleep、捕获会话的 hard timeout 进入 `timeout_pending`、runner 在宽限内退出、每个 token 只释放一次、生产 canary 前后线程回基线且无 `ESTABLISHED/CLOSE_WAIT`。

## TempMail.lol 域名归类规则（2026-07-08）

涉及注册机 TempMail.lol 免费池、`registration_disallowed`、域名熔断或候选子域时：

- 不要再只按 root 根域粗暴黑白名单；同时看 exact 子域和 root 根域。
- `data/register_domain_candidates.json` 是本地运行候选库，来源可以是真实注册后验号 OK，也可以由 `scripts/register_domain_intel_report.py --write` 从历史诊断日志离线回放生成。
- `registration_disallowed` 必须记录 bad exact/root；注册并验号 OK 才能记录 good exact。
- root 熔断时，good exact 子域允许低频探测；未知 exact 子域仍按 root 熔断拦截，避免继续烧完整注册链路。
- 大规模真实注册前先用离线报告看候选与熔断状态；真实探测先小样本，不直接 20 线程硬冲。
- 如果用户要求“跑大量注册归类”，优先执行离线回放和候选注入；需要真实外部注册时必须明确这是会消耗请求并污染风控样本的动作，并先从小样本开始。

### TempMail.lol 大量 unknown exact 探测补充规则（2026-07-08）

- 真实 2000/20 已验证：在 TempMail.lol free tier 上取消 create 间隔和 429 退避会产生大量 `HTTP 429 Rate limited (free)`，不要把这类失败归为网络抖动。
- unknown exact 探测必须启用顺序轮转和运行期 reservation，避免同一轮 in-flight exact 重复使用。
- `data/register_domain_candidates.json` 是并发写热点；所有真实注册线程写入 exact/root 结果必须走 `domain_intel.note_domain_result()`，不能绕过锁直接读改写。
- 大量探测后的可靠入口是 `reports/register-*/good-domains.txt` 与 `bad-domains.txt`；继续注册前先看本轮报告，不要只看总成功率。
- 如果目标是“筛域名”，可以跑 unknown；如果目标是“补号成功率”，优先小批复测 good exact，并恢复 TempMail.lol create 限速。

### 本地注册 no-circuit 诊断规则（2026-07-08）

- 需要“去除域名熔断继续撞域名”时，优先设置 `domain_rejection.enforce=false`，不要直接把 `domain_rejection.enabled=false` 当常规手段。
- `enabled=true/enforce=false` 的语义是：继续记录 `registration_disallowed`，但不做本地拦截。
- no-circuit 跑完必须看分类：`domain_quarantined`、`network_proxy_transient`、`tempmail_429`、`registration_disallowed`，不要只看总成功率。
- 如果 `domain_quarantined=0` 但 `registration_disallowed` 仍占绝大多数，说明瓶颈不在本地域名熔断。
- `send_otp_http_503`、`*_http_500/502/503/504` 属于 transient；GET 可短重试，POST 不默认按状态码重试，避免重复提交副作用。
- WARP/代理波动先查 `data/runlogs/proxy-monitor.log`；如果看到 `project_path_missing`，确认 monitor 是否已加载带 Windows->WSL 路径解析的新脚本。

## 文档分层规则（2026-07-08）

- `02-current-state.md` 只保留当前事实、当前风险和下一步，不再追加长流水。
- `06-handoff.md` 只保留下一位接手必须知道的短摘要，不再复制完整历史。
- 月度流水写入 `docs/logs/YYYY/YYYY-MM.md`。
- 过长、已过时但仍需追溯的文档移动到 `docs/archive/`，不要删除事实证据。
- 如果代码和旧归档冲突，以代码、运行配置和新 `02-current-state.md` 为准。

## SQLite 与 mmap 优化规则（2026-07-08）

涉及 `accounts.db`、`image_tasks.db`、`image_reference_assets.db` 时：

- 当前代码已经启用 WAL / `synchronous=NORMAL` / `busy_timeout`；不要把 mmap 当成第一步。
- mmap、`cache_size`、checkpoint、vacuum 必须作为性能实验小步引入，先本地再 Panda 小流量。
- 开 mmap 前必须采集 DB/WAL 大小、RSS、容器内存余量、读接口 p95、`database locked` 计数。
- Panda 容器内存墙低，mmap 大小必须保守；未验证前不要写入默认配置。
- 如果收益只来自减少历史 b64 读取，应优先瘦身 `image_tasks.db` 和轻量查询，而不是盲目加 mmap。

## 账号调度优化规则（2026-07-08）

- 不要只看 `total_quota` 或 `active` 判断能否加并发；必须看 `dispatchable_candidate_count`、`preflight_backoff_count`、`image_inflight_count` 和失败证据。
- 账号调度当前已有状态过滤、失败证据过滤、Panda 接收态过滤、preflight backoff、全局并发和单账号并发；优先补可观测性，不先重写调度器。
- `no available image quota` 需要拆原因：状态异常、失败证据、接收态、backoff、并发占用、最近额度过期。
- refresh token invalidated 账号不能因为账面有 quota 就恢复进调度。
- 调高 worker / burst / per-user running 前，必须先恢复 clean ready 池并做单变量 A/B。
