# 当前状态

最后更新：2026-07-16 22:20（Asia/Shanghai）

## 摘要

项目当前处于「本地修复与只读审计进行中；Panda 生图调度面耗尽；根分区已回落到门禁内，生产码仍落后本地」状态。

### 2026-07-16 22:09 Panda SSH 只读深检

- 首次在本会话对 `ssh panda` 做实时深检（非仅读本地旧报告）。脱敏报告：`data/runlogs/panda-deep-audit-readonly-20260716-220921.md`；复现脚本：`scripts/panda_readonly_deep_audit.py`。
- 根分区 **49%**（Avail 29G），不再处于 ≥85% 硬门禁；20:34 报告中的 86.1% 已过时。
- Health：`total=18` / 正常5 / 限流1 / 异常7 / 禁用5 / `schedulable=0` / `dispatchable=0` / `verified_total_quota=0` / `image_inflight_count=0`。
- 账面 quota 合计 271 全部在异常/禁用号；5 个「正常」均为 `q=0`，不能当可调度额度。
- 代理：12/18 有账号级 proxy 且 **同一 host 签名**；egress hash / 完整 fp / node_lease / cohort / maturity 均为 0/18。
- 任务库：success15 / error401 / timeout_pending21 / running1；`image_tasks.db` ≈ 1.6G。
- 近 24h 日志关键词：`429×2024`、`quota×664`、`timeout×661`、`account_deactivated×16`。
- 代码漂移：Panda 缺 `account_fingerprint.py` / `account_workload_policy.py`；`openai_backend_api.py` 远端仍为 07-10 版本（`1fa10b…`），本地 `5f4c52…`。部署 ACC-010 必须成对带上 fingerprint 模块。
- 发布约定（2026-07-16）：**只在本地 build/验收**，通过后经 **GitHub → Panda `git pull`**；禁止 Panda 上 build，禁止脚本 scp 直推业务码。误触的 ACC-010 scp 热更已回滚，生产哈希恢复为 `openai=1fa10b…`，fingerprint 已删除，`healthy=true`。
- 本地验收入口：`scripts/panda_acc010_local_build.ps1`。
- `plan.md` 本地进度（2026-07-16 夜）：P0/P2/P3/P4 本地完成；P1 源码+manifest 完成、公网验收待 Git 部署；P5–P8 未开始（须 GitHub→Panda）。证据：`data/runlogs/account-identity-remediation-panda18/`（18 闭合，E12/B6，H1 真共享）。
- 容器 RSS ≈ 445MiB / 1.5GiB；`image_generation_paused=false`；删号开关仍关闭。

### 2026-07-16 Panda 槽位泄漏修复与终态 Outlook 清理

- Panda 修复前 health 为 `total=93 / disabled=75 / active=18 / schedulable=5 / dispatchable=0`，运行态 `image_inflight_count=26`，但 `image_tasks.db` 只有少量真实未完成任务；全局并发上限 10 被失真的内存槽位持续占满。
- 根因是 hard-timeout 与迟到账号分配的竞态：任务先设置 `cancelled`，`get_available_access_token()` 随后才返回；迟到的 `account_acquired` 回调直接退出，未记录 token、未归还 `_image_inflight` 槽位。现已增加迟到 token 的一次性释放保护，并覆盖与 hard-timeout 清理并发发生时的重复释放竞态。
- 本地验证：`test_image_task_service.py + test_image_sync_admission_eta.py + test_account_image_capabilities.py` 共 `62 passed`，目标文件 compile 通过。Panda 受控暂停新生图、等待既有任务归零后热更新并重启；重启后 `image_inflight_count 26 → 0`、`dispatchable 0 → 5`。
- 清理前确认 75 条禁用记录均为 Outlook 且有明确 `account_deactivated` 终态证据。SQLite 在线备份及旧代码位于 `/root/gptimage/backups/slot-leak-outlook-cleanup-20260716-164033/`。
- 删除按 `1 条 canary → 74 条剩余` 执行；最终 Panda `total=18 / active=18 / disabled=0 / schedulable=5 / image_inflight_count=0 / dispatchable=5`，数据库仍有 18 条 Outlook，均为非终态记录。
- 75 条终态记录关联 11 个唯一代理节点：其中 1 个仍被存活账号使用，释放 10 个仅由已删终态账号占用的 Webshare 节点。节点统计只保留 hash，不记录代理凭据。
- 生图入口已恢复为 `image_generation_paused=false`。后续扩池采用固定 Webshare 全生命周期绑定、单 canary 成熟期、少量分批和 cohort 熔断；不再迁移注册/首登/请求出口。

### 2026-07-16 对话穿插实现审计与释放节点复测

- 文本请求底座已存在：项目提供 `/v1/chat/completions`、`/v1/responses`、`/v1/messages`，文本请求也会读取账号级代理；但文本账号选择目前只过滤禁用/异常并做基础轮转，使用后仅更新 `last_used_at`。
- 真实队列编排尚未落地：没有后台工作器、账号生命周期、文本/生图独立策略域、图片容量保护、成熟期状态机及相应测试。`humanize=True` 只用于注册/人工探测时的浏览器交互；真实浏览器注册每号使用临时 profile，流程结束后会删除，未形成持久会话环境。
- 对已释放的 10 个 Webshare 节点复测：Cloudflare trace 三轮均在线、出口哈希三轮一致，地区全部为 `SG`、colo 为 `SIN`，中位延迟约 `62～89ms`。
- 使用生产同类客户端 `curl_cffi + Chrome impersonation` 对 ChatGPT CSRF 连续三轮验证后，节点分层为 `READY=5 / OBSERVE=3 / QUARANTINED=2`；两条隔离节点三轮均为 HTTP 403，观察节点分别通过 `2/3、1/3、2/3`。
- 远端报告位于 `/root/gptimage/backups/slot-leak-outlook-cleanup-20260716-164033/webshare-node-validation.json` 与 `webshare-chatgpt-validation.json`；完整落地方案见 `09-outlook-longevity-99-plan.md`。

### 2026-07-16 账号页可观测性、Panda 最新证据与逆向链路纠偏

- 账号页已增加“代理 / 出口”和“累计流量”列并部署 Panda：代理凭据不显示，出口优先使用已记录 egress IP/hash；累计流量为应用层载荷，历史未采集记录显示“待统计”。后端新增账号上传/下载/总字节累计，覆盖文本完成和生图结果路径。
- 完整 Chromium Profile 的 CPU、内存和磁盘成本发生在 Windows 注册机；Panda 默认只保存轻量 runtime profile、会话元数据和节点租约。少量上传通常只是数 KiB 元数据与一次 SQLite upsert。
- 20:34 只读证据（已被 22:09 深检覆盖磁盘结论）：当时 `total=18 / schedulable=0 / rejected=12`，报告 `data/runlogs/panda-account-evidence-readonly-20260716-203416.md`。22:09 确认根分区已回落至 49%，调度面与 fp/egress 缺口仍在。
- 12/18 有账号代理但代理安全签名完全相同，6/18 缺账号代理；注册/运行出口 hash 与持久 fp 均为 0/18。最近日志样本还显示 timeout/quota 失败占主导、提示文本高度重复，当前根因优先级为共同出口/绑定缺失、失败重试放大、token/批次关联，再到固定请求形状待验证。
- chatgpt2api 文本模型继续维持聊天能力；逆向工程重点回到 ChatGPT Web 聊天/生图的 API/resource Session 隔离、sentinel 头、会话恢复、poll 预算、幂等和失败归因。
- 文本与生图不共享额度数值；采用独立速率域，共享账号健康、固定节点和总 in-flight 门禁。小号池全部图片候选保留给生图；完整方案见 `09-outlook-longevity-99-plan.md`。
- 已创建 `panda-72h-shadow` 自动观察：每 6 小时一次、共 13 轮，只读采集并计算真实队列 shadow 准入；不发送额外文本/生图请求。
- 新增 `AccountWorkloadPolicy` 纯策略与离线 shadow CLI，10 个单测通过；小于10个图片候选时全量保留给生图，文本专用账号仍可处理真实文本队列。

### 2026-07-15 Outlook 号池集中停用深度审计

- 2026-07-15 20:42（Asia/Shanghai）只读快照：Panda `total=93`、`disabled=64`、`limited=18`、`active=11`、`abnormal=0`。18 条限流均有未来 `restore_at`，大部分预计在北京时间 7 月 15 日 22:09～7 月 16 日 01:03 恢复，另 1 条约在 7 月 16 日 11:10 恢复；这些不是死号。
- 11 条 `status=正常` 中，7 条 `quota=0`；另 4 条账面合计 `quota=61`，但 2026-07-15 14:57 左右再次出现 `token invalidated (/backend-api/me)`，因 `invalid_count>0` 被失败证据过滤。当前 `schedulable=0`、`dispatchable=0`、`verified_total_quota=0`，不能用 `active` 或账面 quota 代替真实调度面。
- 64 条终态账号最后一次新增停用证据发生在 2026-07-15 09:06，此后终态数保持 64，没有证据表明 maintenance 或自动恢复仍在持续制造新死号。自动恢复运行态已扫描 5 轮、`attempted=0`、`candidate_count=0`、`terminal_count=64`；最近一轮 maintenance 刷新 17/17、删除 0，`delete_invalid=false` 仍生效。
- 聚合 2026-07-14～15 恢复报告得到 93 条恢复记录：64 个唯一旧 token 被明确归为 `terminal_reason=account_deactivated`，11 个唯一旧 token 使用同一 Outlook OTP 链成功恢复。决定性失败位于 OpenAI `email-otp/validate` HTTP 403 / `account_deactivated`；`missing_openai_password` 只是无 OpenAI 密码时转入邮箱 OTP fallback 的路径前缀，不是根因。
- 批次关联证据：Panda 中 `registration_proxy_scope=shared_stable_warp` 的 68 条账号有 51 条终态、17 条存活，终态率 75%；7 月 14 日约 4 小时内共创建 66 条账号。68 条均从 `127.0.0.1:40080` 注册/首登，但该共享 WARP 已实测不具备账号级粘性出口；随后账号又切本地独享 WARP 验号，上传 Panda 时回环代理被清空并改走 Panda 全局 `single_proxy`，形成短时间多次网络身份迁移。
- 浏览器链每号会新建临时 profile 和 UUID device ID，不是直接复用 Cookie/设备 ID；但所有账号仍使用同一 Chromium 容器、版本、1920×1080、无痕环境和高度规律的连续注册节奏。新批次 51 条终态账号中有 50 条从未成功生图，排除“高强度生图把账号打死”作为主因。
- 当前归因：高置信度为 OpenAI 上游对关联批次的账号信任/反滥用处置；高注册密度、共享或漂移的注册出口、同质浏览器环境、注册后立即跨出口迁移是主要贡献因素。现有证据不能确定 OpenAI 内部命中的单一规则，也不支持把问题归咎于 Outlook 收信、Webshare、Panda 资源、自动删号或生图用量。
- 本地仍有一个恢复闭环缺口：4 条“正常 + 正额度 + 明确 invalid 证据”会被调度器隔离，但自动恢复候选目前只接受 `status=异常` 或 `panda_receive_state=rejected`，因此会经历确认/maintenance 延迟，暂时表现为“不可调度且不自动恢复”。后续应在保留 invalid 二次确认窗口的前提下，把陈旧明确 invalid 证据纳入串行恢复候选。
- 操作边界：保持批量 Outlook 注册停止；终态账号只保留证据与官方申诉，不自动重登；限流账号等待 `restore_at`；不得通过批量换指纹/换出口继续撞上游风控。长期生产生图应优先迁移到官方 API 的组织/项目与付费额度。

### 2026-07-14 Outlook 100 号池批量注册与 Panda 终态对账

- 源凭据 `100/100` 已全部消费：本地 Outlook 池 `unused=0 / in_use=0 / used=100 / failed=0`，没有未处理候选。
- 方案 A 批处理链路已固化为：稳定 `40080` 注册与首次登录 → 独享 WARP 验号 → 成功后移除本地独享容器 → 上传 Panda。批次结束后本地独享容器数为 `0`。
- 最终源池口径：
  - `38` 个源账号在 Panda 通过自身出口验证。
  - `54` 个源账号在 Panda 用 Outlook OTP + 固定 Webshare 重登时被 OpenAI 明确返回 `account_deactivated`，已持久化为终态。
  - `2` 个源账号保留在本地禁用记录，同样为 `account_deactivated`。
  - `6` 个源账号在注册/首登阶段已确认 `account_deactivated`，无可上传 token。
- Panda 总池为 `93`（其中源池账号 `92`）。2026-07-14～15 对所有非终态 invalid Outlook 完成逐号 OTP 恢复；收口完成时快照为非禁用 `29`、`abnormal=0`、终态/禁用 `64`、`verified=29`、`schedulable=25`、空闲时 `dispatchable=25`，另有 `3` 个非终态 `quota=0`。该数字是恢复完成时的阶段快照，当前实时号池已变化，必须以上方 2026-07-15 深度审计和实时 health 为准。自动保活与自动 Outlook 恢复已恢复原开关，删号仍保持关闭。
- 修复 `recover_panda_outlook_accounts.py` CLI 在识别 `account_deactivated` 后不回写终态的缺陷；现会写入 `禁用 + rejected + outlook_recovery_state=terminal`，防止 maintenance 重复 OTP/验号。
- WSL `HermesUbuntu` 已恢复 `systemd + Docker active`，`chatgpt2api-warp-proxy / privoxy / flaresolverr` 均 healthy。
- 域名邮箱已配置：用户输入的 `relai.aisa` 为 NXDOMAIN；实际使用 `relai.asia`。本地注册机已新增 `cloudflare_temp_email` provider，`api_base=https://email-api.relai.asia`，管理员口令只写保存且 `/api/register` / SSE 不回显；首批启用 `relai.asia`、`edu`、`mail`、`verify`、`auth`、`account` 六个域名。六域均完成“创建邮箱 → 管理员恢复 JWT → 读取空收件箱”canary；真实 OpenAI canary 也已成功发码、收码并校验，最终在 `create_account` 被上游以 `registration_disallowed` 拒绝，证明验证码读取问题已解决，剩余瓶颈是域名/注册风控。
- 本轮本地回滚点：`data/backups/before-outlook-100-bulk-20260714-141244/`；Panda 每个恢复批次均生成独立 SQLite 备份，脚本部署前也保留了 `recover_panda_outlook_accounts.py.before-terminal-persist-*`。

### 2026-07-15 单账号申诉前置环境与出口复核

- `scripts/start_proxy_stack.ps1` 新增隐藏、幂等的 Windows 侧 WSL keepalive；`scripts/stop_all.ps1` 会同步终止该进程。实测代理栈启动后 `40080` 与 `8191` 可从 Windows 持续访问，避免 WSL 命令结束后发行版退出。
- `40080` 只确认“可持续访问”，不能视为账号级粘性出口：同一 WARP 容器在 20 秒窗口内出现不同出口哈希。因此后续单账号恢复/申诉验证不再把共享 WARP 描述为固定公网 IP。
- Wesley 终态账号的固定 Webshare canary 已通过：ChatGPT CSRF HTTP 200，两次公网出口哈希一致，Outlook 邮箱通过同一代理完成可读性预检；账号仍保持 `禁用 + terminal + account_deactivated`，未重新登录、未解除熔断、未上传 Panda。
- OpenAI 官方停用说明确认申诉入口为 `https://openai.com/form/appeal/`。2026-07-15 已为 Wesley 账号提交一次正式申诉：选择“我的使用未违反使用政策/服务条款”，正文仅包含邮箱归属、用户 ID、停用时间、`account_deactivated`、人工复核请求和合规承诺。页面明确返回 `Thank you for submitting your appeal`；未显示工单号。官方提示多数申诉在几个工作日内审核，复杂案例可能更久，等待 Outlook 邮件期间不要重复提交。

### 2026-07-14 本机 Outlook 真实浏览器注册接管进展

- 本机 `3000`、`8000`、`40080`、`8191` 已恢复；前端注册页、后端版本、FlareSolverr health 均为 HTTP 200，40080 实测 `warp=on`。
- 真实 Chromium/CDP 注册已适配当前 OpenAI 页面：
  - `/create-account/password` 只填写 `new-password`，不再误操作预填邮箱框。
  - `/about-you` 同时兼容旧 `name=age` 和新版 Birthday 月/日/年分段控件。
  - 注册网络诊断按 request-id 关联 `Network.loadingFailed`，只记录路径、状态和脱敏错误。
- 独享 WARP 管理器会记录出口哈希；若与现有账号出口碰撞，最多重连三次，仍重复则拒绝注册。
- 本机 `127.0.0.1:41xxx` 独享代理被标记为 `panda_sync_state=local_proxy_only`，注册服务不会把不可从 Panda 访问的本地代理账号上传远端。
- 真实 canary 结果：
  - 新建独享 WARP 节点可完成密码和 Outlook OTP，`email-otp/validate` 返回 200，但创建阶段的 `/api/accounts/authorize` 多次无响应并在 `/about-you` 显示 `Operation timed out`。
  - 同一候选改走历史稳定入口 `127.0.0.1:40080` 后，真实浏览器注册、同 IP 邮箱 OTP 登录、Token 导入和后端验号成功。
  - 成功账号随后切换到独享 `127.0.0.1:41003`，再次额度刷新通过：`status=正常`、`quota=25`；本地 `panda_sync_state=local_proxy_only`，Panda 账号总数保持 `24`，未发生误上传。
- 用户已确认采用方案 A，默认流程现为：稳定 `40080` 完成注册与首次登录，随后立即写入账号独享 `41xxx` WARP 并执行 `/backend-api` 验号；账号记录明确写入 `lifecycle_ip_mode=split_registration_dedicated_runtime`，不宣称严格全生命周期同 IP。
- 浏览器若被 OpenAI 导向 `/log-in/password`，会识别为上游已有账号并退出注册页面阶段，随后使用 Outlook OTP 登录导入，不再等待 45 秒后误报页面超时。
- Outlook 计划器会优先处理未记录的新凭据，把 `failed` 重试排在后面；历史无状态池可先执行“清理未使用”再重新导入，避免旧残留号阻塞 canary。
- 方案 A 真实验收：
  - `we***1` 与 `ri***4` 均完成稳定入口登录、独享节点切换和首次验号；本地账号总数增至 `29`，两条账号均保持 `local_proxy_only`，注册日志确认未上传 Panda。
  - `ri***4` 在独享 `127.0.0.1:41007` 上再次刷新成功，`status=正常`、`quota=16`；固定该账号执行 `gpt-image-2` 生图成功，得到 1 张图片，b64 长度 `1,168,812`。
  - `we***1` 后续再次刷新出现 `token invalidated (/backend-api/me)` 并被标记异常，未自动删除；说明方案 A 链路可用，但 Outlook Web token 仍需按既有 OTP 恢复机制持续维护。
- 本轮相关注册回归：`85 passed`；Python compile 与目标 `git diff --check` 通过。

- 本地和 Panda 默认账号存储均为 SQLite。
- SQLite 当前已启用 WAL、`synchronous=NORMAL`、`busy_timeout`；未启用 mmap。
- Panda 生图已从保护暂停恢复，当前低并发运行。
- 2026-07-10 Panda 累计三条 `token invalidated` Outlook 已通过独立 OTP 恢复链恢复；22:08 health 为 `total=12`、`schedulable=12`、`dispatchable=12`、`panda_rejected=0`、`verified_total_quota=211`。
- 账号页每行 `RefreshCw` 已接入异常 Outlook 一键恢复；正常账号仍执行额度刷新。恢复凭据只保存在 Panda 本机 `600` 权限 secret，不进入仓库。
- 2026-07-14 Panda `vi***35@outlook.com` 的邮箱预检、OTP 收信均成功，失败点是 OpenAI `email-otp/validate` 明确返回 HTTP 403 / `account_deactivated`；因此根因是上游账号已删除或停用，不是 Outlook、Graph/IMAP、Webshare 或本地 token 自然过期。
- Panda 已部署上游终态熔断：该账号记录保留为 `禁用 + outlook_recovery_state=terminal + account_deactivated`，自动恢复运行态为 `candidate_count=0 / terminal_count=1`；UI 后端入口、普通/批量账号刷新、手动密码重登录、CLI 显式目标、自动恢复和 maintenance token override 都会拒绝/跳过，不再每 5/30 分钟重复 OTP 或重打 `/backend-api/me`。生产备份位于 `/root/gptimage/backups/outlook-terminal-before-20260714-1255/` 和 `/root/gptimage/backups/outlook-terminal-refresh-guard-before-20260714-134906/`。
- 生图 SSE ready 条件已从“任意非空 `data:`”收紧为“捕获 `conversation_id` 元数据”；ping/control/心跳不能解除 45 秒 deadline。捕获后 SSE 最多再等 15 秒即转 `/backend-api/tasks` 轮询。
- 2026-07-10 IMG-017 已部署 Panda；真实同步 canary 78.91 秒成功，前后 Python 线程均为 2，无 `ESTABLISHED/CLOSE_WAIT` 残留，`unfinished={}`。
- 2026-07-09 用户要求关闭“刷到死号自动删除”：本地和 Panda 的 refresh-all / maintenance 已改为 `delete_invalid=false`；`auto_remove_invalid_accounts=false`、`auto_remove_rate_limited_accounts=false` 继续保持关闭。
- staging 仍保持关闭；账号页已增加上传/接收状态可视化、手动上传控制、自动上传开关和注册/上传或接收/删除三色折线图。本地 `panda_sync.enabled=true`，成功 ACK 后删除本地账号。
- 注册机 TempMail.lol exact 子域归类器已落地；当前真实注册成功率受 OpenAI `registration_disallowed` 与 TempMail.lol free 429 限制。

历史长流水已归档到 `docs/archive/02-current-state-history-20260708.md`。近期详细执行记录继续看 `docs/logs/2026/2026-07.md`。

## 生产环境

| 项 | 值 |
| --- | --- |
| SSH 别名 | `panda` |
| 项目路径 | `/root/gptimage` |
| 容器名 | `chatgpt2api-local` |
| Compose 文件 | `/root/gptimage/docker-compose.panda.yml` |
| 公网域名 | `https://gptimage.relai.asia` |
| 对外端口 | `8012 -> container 80` |
| 容器资源 | 约 `1.5 vCPU / 1.5GiB` |
| 生产热更新 | bind-mount 为主；Python 代码更新后必须 restart 才会加载 |

## 当前代码事实

### 存储

- 默认存储后端：`STORAGE_BACKEND=sqlite`。
- 账号库：`data/accounts.db`，表结构为 `accounts(access_token, data)` 与 `auth_keys(key_id, data)`。
- 账号运行时写入已是行级 `upsert_accounts()` / `delete_accounts()`；`save_accounts()` 仍保留全量替换语义，但主要用于初始化或显式迁移。
- 账号 SQLite 当前 PRAGMA：
  - `journal_mode=WAL`
  - `synchronous=NORMAL`
  - `temp_store=MEMORY`
  - `busy_timeout=5000`
- 生图任务库：`data/image_tasks.db`，已使用 WAL / NORMAL / busy_timeout。
- 图片参考资源库：`data/image_reference_assets.db`，已使用 WAL / NORMAL / busy_timeout。
- 当前未配置 SQLite mmap，也没有统一的 `cache_size` / checkpoint 策略。

### Panda 同步

- 代码默认：`panda_sync.queue_on_failure=false`。
- 同步失败不再进入 pending；失败账号应留在本地主池，便于本地刷新和探活。
- Panda 水位同步、staging、探活成熟度、导入批量限制均已有代码支撑。
- 本地 Windows 计划任务 `gptimage-panda-account-sync` 已禁用，避免绕过配置继续向 Panda 增长。
- 本地当前保持 `panda_sync.enabled=true`、`staging_enabled=false`、`remove_local_on_success=true`、`queue_on_failure=false`；成功 ACK 后本地账号立即删除，不再留 pending。
- 已 `synced` 但远端实际缺失的本地留存账号，受控上传会通过远端 token 快照识别并允许重传。
- `/api/accounts/sync/panda` 已改为调用受控 `account_refresh_all_service.queue_available_accounts_for_panda()`；不再调用旧 `scripts/sync_accounts_delta_to_panda.ps1`。
- `/api/accounts/sync/panda` 返回 `details`，可解释 eligible、远端缺失重传、已在远端、水位/配置/失败证据/探活阻断、本地删除数。
- 账号页本地节点显示“上传”，Panda 节点显示“接收”；折线图三色为绿色注册/入库、蓝色上传或接收、红色删除。

### 账号调度

- 生图候选要求：
  - `status` 不能是 `禁用` / `限流` / `异常`
  - 有可用额度或真无限/未知额度
  - 无 `invalid_count`、token refresh 错误、quota refresh 错误等失败证据
  - Panda 接收态必须是 `verified_ready` / `verified` / `local_verified` 或为空
  - 不处于内存态 preflight backoff
- 调度按最近额度刷新时间、额度、最近使用时间排序。
- 全局并发由 `image_global_concurrency` 控制。
- 单账号并发由 `image_account_concurrency` 控制。
- 单任务最多尝试账号数由 `image_token_max_attempts` 控制。
- health 已暴露运行态候选指标：
  - `schedulable`
  - `preflight_backoff_count`
  - `ready_candidate_count`
  - `available_candidate_count`
  - `dispatchable_candidate_count`
  - `image_inflight_count`

### 生图队列

- 异步生图主入口：`/api/image-tasks/*`。
- NewAPI/OpenAI 兼容同步入口已经支持 sync-over-async 路径，但外层 NewAPI/Cloudflare 长连接仍是容量瓶颈。
- `timeout_pending` 续轮询存在，拿到 `conversation_id` 后不再换号重开图；hard timeout 前已捕获会话时会保存恢复 token、设置取消信号并转 `timeout_pending`，不再直接生成终态 502。
- pre-conversation 使用 queue `get(timeout=remaining)` 等待 `conversation_id` 元数据，而不是等待任意非空 payload；当前 `timeout=45s`、`pre_conversation_max_attempts=2`，失败账号进入 transient backoff 后最多有限换号一次。
- 捕获 `conversation_id` 后，SSE 采用 15 秒 post-ready deadline；即使上游持续发送 control heartbeat，也会按墙钟切到现有 `/backend-api/tasks` 轮询。
- `cancel_event` 已贯穿 task service、generation/edit handler、`ConversationRequest`、`OpenAIBackendAPI`、SSE 与 image poll sleep；hard timeout 另有 1 秒取消宽限并记录 `runner_alive_after_cancel`。
- backend/session 会显式关闭，curl_cffi stream executor 使用 `shutdown(wait=False, cancel_futures=True)`；重启已清除历史僵尸线程。
- `image_task_queue` 支持 per-user queue、running 限制、burst 骨架和回传窗口。
- 未捕获会话的 hard timeout 仍执行 backoff/mark-fail；`mark_image_result()` 自带 slot 释放，只有标记异常的 token 才走 `_force_release_image_slots()` 兜底，避免双减。

### 注册机

- TempMail.lol exact/root 两层域名归类器已落地。
- 真实 `2000/20` unknown exact 探测已完成；成功率约 `0.9%`。
- 当前限制主要是 OpenAI `registration_disallowed` 与 TempMail.lol free 429，不应继续盲目 20 线程硬冲。
- 2026-07-09 小样本复测：TempMail.lol 能创建邮箱、发送验证码、收码并完成验证码校验；unknown exact `3/1` 与历史 good exact `2/1` 均 `0` 成功，失败主因仍是 OpenAI `create_account_http_400 / registration_disallowed`，不是邮箱收码失败。
- 2026-07-09 OutlookToken first10 注册：从 `0708-3000_010.txt` 前 10 个邮箱注册，首轮成功 `8/10`，成功率 `80%`；原 2 个在注册后验号阶段 `/backend-api/* HTTP 403` 被注册流程删除。后续已从本地 SQLite/WAL 残留恢复这 2 个的随机 OpenAI 密码，用 Outlook OTP 登录重新提取 Web token，2/2 本地刷新验证通过并上传 Panda。
- 2026-07-09 Panda Outlook 池曾通过清理旧失败证据显示 `12` 个 `schedulable`，但 12:57 NewAPI 5 次 smoke 后做只读直连验证，真实只有找回的 2 个可通过 ChatGPT Web backend；原 10 个仍返回 `/backend-api/* HTTP 403`。当前正确结论：NewAPI 通道可用，5 次生图成功；Outlook 池真实有效面只有 2 个，原 10 个不应长期强行参与调度。为避免高频 maintenance 继续放大 403，Panda `account_maintenance_loop` 已改为 `stale_after_hours=12`、`include_recent=false`、`cooldown_sec=300`、`batch_limit=20`、`concurrency=1`；`account_refresh_all` 同步改为 `stale_after_hours=12`、`include_recent=false`。
- 2026-07-09 16:50 事故复盘：Panda 配置确实是 `delete_invalid=false`，但账号页“全量慢刷额度”前端硬编码传了 `delete_invalid=true`，覆盖了全局关闭配置，导致 refresh-all 删除 10 个 403 Outlook 账号。已从 `/root/gptimage/backups/outlook-schedulable-20260709-120053/accounts.db` 非覆盖式恢复缺失 10 个，并标记为 `panda_receive_state=rejected` / 失败证据隔离，避免污染调度。已修复：前端慢刷和保活均发送 `delete_invalid=false`；后端 `AccountRefreshAllOptions.from_mapping()` 增加保险，配置关闭删除时请求传 true 也会被强制降为 false；maintenance 设置接口同样不能在配置关闭时被请求重新打开删除。
- 注意：`delete_invalid=false` 只关闭 refresh-all / maintenance 的 invalid 自动删除；注册后验号失败时删除本地临时账号属于注册流程防污染逻辑。若本地 SQLite/WAL 仍保留完整行，可用随机 OpenAI 密码 + Outlook OTP 重新登录提取 Web token。
- 2026-07-09 TempMail.lol 死号恢复试水：本地备份中最近 200 个 TempMail.lol 死号经 Panda+Webshare 只读 RT refresh 测试，`0/200` 可刷新，统一为 `refresh_token_invalidated`；10 个密码登录试水均进入邮箱 OTP；3 个旧地址按 prefix/domain 重建 inbox 均返回新地址。结论：TempMail.lol 死号在未保存原 inbox token 时不可批量重登恢复；后续若继续使用，注册时必须持久化并保护 inbox token，Outlook 恢复链路独立保留。
- 2026-07-09 Panda Outlook 恢复链曾完成 `10/10` 写回；当时暴露的无 `conversation_id` 长尾已在 2026-07-10 定位为 curl_cffi `Response.iter_lines()` 无超时 queue wait。当前已部署 queue 首包 deadline 与两次有限换号；最新真实 canary `39.152s` 成功，不再沿用旧 `schedulable=10 / quota=249` 口径。

### Outlook 已注册账号导入诊断

- Outlook 凭据文件格式仍为 `email----password----client_id----refresh_token`；恢复时使用邮箱 RT 读取 OpenAI OTP。
- 2026-07-10 发现 Platform 密码登录会对两条失效号返回 `invalid_username_or_password`，但 ChatGPT 当前默认邮箱 OTP 登录可正常返回 `accessToken` 与 `sessionToken`。
- 正式手动链已固化为 `scripts/recover_panda_outlook_accounts.ps1` + `scripts/recover_panda_outlook_accounts.py`：先尝试密码链（可得到 OpenAI RT），失败后自动走 ChatGPT 邮箱 OTP（access/session token），再由 Panda Webshare 验证 `/backend-api`、清旧 fp/失败证据、成功后删旧 token。
- ChatGPT 邮箱 OTP fallback 当前不会返回 OpenAI `refresh_token`；脚本会保留 `chatgpt_session_token`，账号过期或再次失效时重新执行手动恢复链。

## Panda 当前运行状态

最近一次复核时间：2026-07-10 22:08（Asia/Shanghai）。

```text
/version: 200 {"version":"1.5.0"}
/health?format=json: healthy=true
proxy_runtime.enabled=true
accounts.total=12
accounts.schedulable=12
accounts.dispatchable_candidate_count=12
accounts.panda_rejected_count=0
accounts.verified_total_quota=211
maintenance-loop: enabled=true, delete_invalid=false
refresh-all: delete_invalid=false
local scheduled task: gptimage-panda-account-sync Disabled
```

Panda 生图恢复配置：

```text
image_generation_paused=false
image_task_queue.enabled=true
submit_workers=10
poll_workers=1
download_workers=2
per_user_running_max=10
per_user_running_base=10
per_user_running_burst=10
burst_enabled=false
submit_start_min_interval_ms=1500
image_global_concurrency=10
image_account_concurrency=1
newapi_image_sync_admission_max=12
newapi_image_sync_admission_max_eta_secs=180
```

仍保持关闭：

```text
auto_remove_invalid_accounts=false
auto_remove_rate_limited_accounts=false
panda_sync.staging_enabled=false
panda_sync.queue_on_failure=false
```

最近 canary（2026-07-10 22:01，IMG-017 部署后）：

```text
task_id=sync-zKrDisvuqd_XiyLkwLvHvA
HTTP 200
final=success
result_count=1
elapsed_secs=78.91
duration_ms=78777
conversation_id=6a50fad1-c09c-83ec-b1ff-41c90c014b59
threads=2 -> 2
tcp_after={LISTEN: 2}
unfinished={}
```

## Panda 账号池事实

最近一次复核（2026-07-10 22:08）：

```text
Panda:
total=12
status.正常=12
panda_receive_state.verified_ready=12
schedulable=12
dispatchable_candidate_count=12
panda_rejected_count=0
verified_total_quota=211
```

2026-07-10 三条恢复结果：

- `be***6@outlook.com`：旧 quota `9`，新 token 经 Panda Webshare `/backend-api` 验证后 quota `9`，已入调度。
- `da***0@outlook.com`：旧 quota `0`，新 token 验证后 quota `25`，已入调度。
- `ev***7@outlook.com`：旧 quota `20`，新 token 经 ChatGPT 邮箱 OTP 与 Panda Webshare 验证后 quota `20`，已入调度。
- 三条均满足：旧 token 已删除、`fp` 未继承、`invalid_count=0`、无 refresh/quota 失败证据、`panda_receive_state=verified_ready`、每号独立 Webshare proxy 已配置。
- 三条 fallback token 当前没有 OpenAI `refresh_token`，但保留了 ChatGPT session token；不要依赖 RT keepalive，失效时走账号页刷新图标或 CLI 恢复入口。

账号页一键恢复入口：

- 地址：`https://gptimage.relai.asia/accounts/`。
- 正常账号点击行尾刷新图标：刷新账号信息和额度。
- 异常 / rejected Outlook 点击同一图标：执行 `RT 读 OTP → ChatGPT 邮箱 OTP 登录 → 新 token 隔离写入 → Panda Webshare 验证 → 去旧 fp/旧 token → reload_from_storage`。
- 服务端凭据：`/root/gptimage/data/runlogs/panda-outlook-recovery.credentials.secret.txt`，权限必须为 `600`；不得提交仓库、不得输出内容。
- 同一时间只允许一条 UI 恢复任务；每次任务自动在 `data/backups/` 建 SQLite/config 备份，并在 `data/runlogs/` 写脱敏报告。

CLI 兜底入口（Windows）：

```powershell
# 1. 只看将恢复哪些异常 Outlook，不改 Panda
powershell -ExecutionPolicy Bypass -File .\scripts\recover_panda_outlook_accounts.ps1 `
  -CredentialsPath "$HOME\Downloads\tokens_2026-07-09.txt" -DryRun

# 2. 恢复当前全部异常/rejected Outlook
powershell -ExecutionPolicy Bypass -File .\scripts\recover_panda_outlook_accounts.ps1 `
  -CredentialsPath "$HOME\Downloads\tokens_2026-07-09.txt"

# 3. 只恢复指定邮箱
powershell -ExecutionPolicy Bypass -File .\scripts\recover_panda_outlook_accounts.ps1 `
  -CredentialsPath "$HOME\Downloads\tokens_2026-07-09.txt" `
  -Email "name@outlook.com"
```

保护规则：

- 默认只选 `异常` / `rejected` / 明确 invalid 证据的 Outlook；不会把单纯 quota=0 的正常限流号拿去重登。
- 每次实写前用 SQLite backup API 备份 `accounts.db`；新 token 先以 `incoming + quota=0` 隔离。
- 必须先过同一条 Webshare 的 csrf 预检与 Panda `/backend-api` 刷新；成功后才清失败证据、删除旧 token 并重启 app。
- 失败时回滚新 token、保留旧记录；上传的 Outlook/目标 secret 会自动清理，报告只含脱敏邮箱和 token hash。
- 当前没有独立“生图 ban 黑名单”字段；`mark_image_result(False)` 只增加 `fail`，不会自动禁用账号。
- Panda 侧继续保持 `delete_invalid=false`，invalid 账号由人工确认后处理。

## 2026-07-15 IMG-019 生图回传与假 429 修复

- 2026-07-13 的“四张图只显示两张”已按 NewAPI、Panda 调用日志和图片时间线还原：前三个请求成功，其中第三个 `n=1` 请求被上游意外返回 2 张图；第四个请求进入 `timeout_pending`，同步入口等满 540 秒后返回 504，并没有第四个成功结果等待回传。
- 当前 429 发生时实际 `image_inflight_count=0`、可调度账号 23；任务库中的约 20 个 `timeout_pending/resume_polling` 恢复任务被错误计入同步 admission ETA，估算达到 181～187 秒并越过 180 秒门槛，形成假拥堵。
- `estimate_sync_eta_secs()` 和当前用户队列门禁现在只计算真正占用提交容量的 `queued/running`；恢复任务仍计入全局队列总量保护，但不再阻塞新同步请求。
- 每次单图上游会话最多下载、保存和回传 1 张图片；恢复轮询同样只接收第一张，防止 `n=1` 被放大成双图大响应。
- 上游 `{"skipped_mainline":true}` 改为可重试的“主链路未启动”结果，最多串行换号 3 次，不增加单请求并发。
- Panda 直连 canary：HTTP 200，59.3 秒，`data_count=1`；NewAPI 全链路 canary：HTTP 200，37.0 秒，`data_count=1`。
- 生产回滚点：`/root/gptimage/backups/hotfix-image-return-20260715-172125/`（完整回滚本轮两文件）和 `/root/gptimage/backups/hotfix-skipped-mainline-20260715-173655/`（仅回滚第二阶段）。

## 当前风险

- Panda 账号可调度面仍然偏小；任何高并发恢复都会快速放大 token 失效、preflight backoff 和上游长尾。
- 本地 clean 在本地 egress 刷新通过，不等于 Panda egress verify 通过；Panda 已出现导入后验证失败并删除的情况。
- NewAPI/Cloudflare 同步长连接仍可能在 24 路以上断流；本轮只完成 Panda 直连与 NewAPI 单请求 canary，不能据此恢复 24 路同步硬压。
- SQLite 未启 mmap；当前不是瓶颈，但在大账号池、任务库增长、频繁状态查询下可能成为后续优化点。
- `data/image_tasks.db` 会随历史结果增长；应继续控制 b64 结果存储、保留期和轻量查询。
- 自动删号必须保持显式确认，不应因生图失败直接清号。

## 后续方向

1. **Panda 低并发观察**：真实业务先按当前低并发跑，确认 Relai/NewAPI 不再长期排队。
2. **账号调度优化**：不急于重写调度；优先补可观测性和候选失败归因，见 `docs/04-improvement-backlog.md`。
3. **扩池策略**：本地自动上传已开启且成功后删除本地；从本地 clean ready 池补 Panda 时每批后必须观察 Panda `incoming/verified/removed`，不要无脑连灌。
4. **SQLite mmap 评估**：只作为后续性能优化，不在当前保号池阶段启用。
5. **文档维护**：主文档只保留当前事实，历史流水写入月度日志或 `docs/archive/`。

## 2026-07-08 本地注册机 no-circuit 诊断

当前本地注册机已支持把域名拒绝的“记录”和“拦截”拆开：

```text
domain_rejection.enabled=true
domain_rejection.enforce=false
domain_rejection.block_free_provider_when_known_roots_quarantined=false
```

2026-07-08 16:43 复核：`data/register.json` 曾被写成 0 字节，导致本地后端加载默认空注册配置，表现为 `/register/` 页面信息消失。已从 `data/backups/register-no-circuit-20260708-105716/register.json` 恢复，并把安全参数固定为 `enabled=false`、`total=200`、`threads=40`、`domain_rejection.enforce=false`、`domain_rejection.block_free_provider_when_known_roots_quarantined=false`、`domain_candidates.probe_unknown_limit=200`；本地后端已重启并验证 `/api/register` 返回 provider、proxy 和历史 stats。

含义：

- `registration_disallowed` 仍会写入 `data/register_domain_candidates.json`。
- 本地域名熔断不再提前拦截注册链路。
- TempMail.lol free root 全部熔断时也不再整体阻断 provider。

已执行 `200 / 40` 小批诊断：

```text
job_id=a9e70c681dc04fa49e72289870d359ee
success=2
fail=198
success_rate=1.0%
elapsed=83.5s
domain_quarantined=0
network_proxy_transient=0
tempmail_429=9
create_account_http_400 registration_disallowed=188
other_send_otp_http_503=1
```

结论：

- 去除本地域名熔断后，熔断误杀已归零。
- 本轮网络/代理瞬断已归零；上一轮大量 `curl(56)/503` 与 WARP/monitor 旧问题相关。
- 当前低成功率主因仍是上游建号阶段 `create_account_http_400 / registration_disallowed`，不是本地熔断。
- 新增成功 exact 子域：`ih.gardianwaves.org`、`l8.icodetensor.com`。

报告：`reports/register-no-circuit-200-40-20260708-110204/`。
