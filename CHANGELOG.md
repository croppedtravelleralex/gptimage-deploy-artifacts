# Changelog

## Unreleased

+ [优化] 生图取号懒刷新：`restore_at` 已过且账面 `quota≤0` 时允许进候选，acquire 时再拉上游 limits（事件驱动，非到点齐刷）。
+ [运维] 2026-07-18 拟人化 Phase B/C 正式部署 Panda：artifacts `humanlike-v22` @ `d6ab644`；备份 `git-artifacts-deploy-20260718-110441`；容器符号/软熔断修复/RUNTIME_OK 已验收；workload 仍 shadow。
+ [修复] 软熔断/上游 429 不再把账号 `status` 打成「限流」导致永久不可调度；软解除可愈合历史误写；`is_rate_limit_http_error` 收紧为明确 429。回归测 `test_humanlike_softband_heal.py`。
+ [功能] 拟人化调度全量接线（docs/10 Phase B 续优化 + C）：选号夜间/午餐软权重；`timezone_from_egress`；文本 gap；上游 429→`apply_429_cooldown`；submit 间隔 Uniform 抖动；`C_g=max(cfg,D)`；fail_streak/cohort/新号 soft；resume 退避+总墙取消；prompt 短窗去重；`workload.auto_live_min_ready`（默认 0）。单测 `test_humanlike_scheduler.py` 13 passed。
+ [运维] 2026-07-18 Phase A+B 落地 Panda：关 maintenance/recovery；`submit_start_min_interval_ms=5000`；`scheduler.enabled=true`（gap/soft band）；`proactive_refresh` SG 日历 `p_rest=0.35`；备份 `config.phaseA-before-20260718-101054.json`；公网 health healthy。
+ [文档] `docs/10-human-like-workload-plan.md` v2.2：§4.6 现网/v2.1/Hard 吞吐对照（小时≈持平、日≈−30%；Hard 小时≈−41%、日≈−48%）；§5.4 降封天花板（v2.1≈30–50%，Hard≈45–60%，≥70% 不现实）；默认推 v2.1。
+ [文档] `docs/10-human-like-workload-plan.md` v2.1：探活时区 `Asia/Singapore`（跟 SG 代理）；休息日 `p_rest=0.35` 低频非零；§5 降封分信号估计（总风险直觉约 30–50%，禁止伪精确承诺）；夜间/午餐软权重、失败冷却、新号限额续优化；性能参数表同步。
+ [文档] `docs/10-human-like-workload-plan.md` v2：去过时开源权威（L0–L3）；容量 Uniform/Poisson 抖动与日熔断软带；工作日随机探活；落地性能/参数总表；Phase A–D。
- [回滚/修复] 2026-07-17：逆向协议生图链路回滚误改（仅协议层）：去掉 SSE `post_ready=15s`；estuary 下载恢复 `session.get` + 显式 Bearer；`skipped_mainline` 不再当「换号失败」；生图模型一律允许 conversation poll；poll 勿用 initial file_id 预填 last_hit_key；下载 404/403 短重试。S3/upload 仍用无鉴权 resource session。Panda 验收：观察号 iv***3 `ok=true` b64≈725KB ~73s，备份 `git-artifacts-deploy-20260717-224254`。
- [运维] 2026-07-17 20:51：指纹分化 + `/me` 轻头回退 + maturity 字段已 artifacts 部署 Panda（备份 `git-artifacts-deploy-20260717-205132`）；health ok，markers OK。
+ [优化] 账号指纹轻微分化：`build_diversified_fp(seed)` 在 Chrome 120/124/131 × Windows/macOS × accept-language 间稳定选型；完整 fp 不重写 device/session。
+ [修复] `/me`（及 accounts/check）遇 CF 边缘 403 后改用轻量头（Bearer+device/session+UA）重试，缓解 Olivia 类间歇拦截。
+ [新增] 成熟度字段透传 `maturity_stage` / `maturity_checked_at` / `cohort_id`；`panda_canary_observe.py --write-maturity`。
+ [文档] 澄清 FlareSolverr：注册走 Camoufox，Panda 全局 clearance 保持关；重写 `02-current-state` 摘要并刷新 `09` / ACC-010 过时证据。
+ [运维] 2026-07-17：CF403 文案 + `/me` 边缘重试已经 artifacts 最小 overlay 部署到 Panda（备份 `git-artifacts-deploy-20260717-185949`）；公网 accounts 已无「Web 验证 403，需重登」。
+ [修复] 账号页不再把 `/backend-api/* 403` 误写成「需重登」；改为提示 CF/出口边缘拦截（可重试/换节点）。事故恢复文案同步去掉强制重登暗示。
+ [修复] `OpenAIBackendAPI`：`/me`、`conversation/init`、`accounts/check` 识别 CF HTML 403，错误前缀 `cf_edge_block:`，并做最多 3 次短暂重试。
+ [运维] `panda_rebind_unique_proxies.py`：共享 binding 号池换独立 Webshare；`update_account_identity(clear_isolation=True)` 允许解除 isolation。
+ [修复] 生图/聊天 `_bootstrap`：CF HTML 403 等边缘拦截时软失败回退默认 PoW；首页头经 `build_headers` 合并 clearance；生图 prepare/start 遇 CF 边缘 403 短暂重试。
+ [新增] 账号页：全宽平铺、类型/来源与 Token/邮箱合并列、默认 100/页、去 Panda 列与成功失败列、流量可读、流水贝塞尔+坐标轴。
+ [新增] Canonical `chatgpt_web_request` builder + `RequestPhaseTracker` 阶段耗时；聊天/生图出站收敛。
+ [新增] WorkloadPolicy shadow/live + canary Rimg 豁免 + 文本队列（默认 off 同步直通）；`/health` 暴露队列深度。
+ [新增] 反 bot：per-account clearance jar；共享 binding 入库强制 isolation；节点软租约字段门禁。
+ [修复] Outlook 恢复优先账号 sticky proxy 并写回 binding/egress；`recover-outlook` 需随 api 层部署。
+ [新增] canary `/me` 门禁脚本与 isolation preflight；update/import 保留 `identity_isolated`。
+ [修复] refresh-all 成功路径保留 `panda_receive_state=identity_isolated`，避免 canary 同伴被刷回 `verified_ready` 导致共享绑定门禁再次挡住调度。
+ [运维] P7：t1h/t6h 已采集且漂移报告 pass；回滚演练 web_dist/backend 路径证据；公网 accounts chunk 与宿主 hash 一致且含「代理/出口」「累计流量」。
+ [优化] 生图 poll：`ImagePollBudget` 硬上限（conversation GET / tasks GET）+ tasks 低频；外层重复 poll 收敛为 1 次；conversation 恢复改用 submit 前 `started_at`；`body_shape` 接入 conversation 请求日志。
+ [运维] P5 证据目录 `account-identity-remediation-p5-*`（hash 对账/字段合同）；canary 候选 `40de2f332c0d3fd4`；本地矩阵 `matrix-results.json` 三套 suite 全绿。
+ [修复] canary apply 出口探测：缺失 `proxy_egress_hash` 时三次测量后再写身份。
+ [修复] Panda 部署：`account_refresh_all_service` 改从轻量 `services/proxy_url_utils.is_local_only_proxy_url` 导入，避免依赖未下发的 `real_browser_register` 导致容器 ImportError 重启。
+ [运维] Panda 22:09 SSH 只读深检：根分区 49%（磁盘门禁解除）、调度面仍 0、fp/egress 0/18、生产码漂移；报告 `data/runlogs/panda-deep-audit-readonly-20260716-220921.md`，脚本 `scripts/panda_readonly_deep_audit.py`；同步刷新 `02-current-state` / `09` / `04` ACC-010。
+ [运维] 发布门禁：ACC-010 **仅本地** compile/pytest/前端 build（`scripts/panda_acc010_local_build.ps1`）；验收通过后经 **GitHub push → Panda git pull**，禁止在 Panda 上 build，禁止脚本直推 scp 业务码。误触的 scp 热更已从 `acc010-fingerprint-backend-20260716-222708` 回滚（`openai`→`1fa10b…`，fingerprint 已移除，health ok）。
+ [新增] 账号身份门禁：`update_account_identity` / `ensure_account_identity_ready` / 普通 update·import 写保护；Panda 上传完整性校验；重复 `proxy_binding_hash` 调度拦截。
+ [新增] `scripts/repair_panda_account_identity.py`（audit/canary/apply）+ Panda 18 号脱敏 inventory（E12/B6，H1 真共享）。
+ [修复] ACC-008：超过确认窗的「正常+invalid」Outlook 进入自动恢复候选。
+ [修复] 去掉 conversation「最新对话」兜底，避免同账号并发串会话。
+ [优化] `/version` 暴露 backend_commit / frontend_build_id / build_drift；前端 build 写 `web_dist-manifest.json`。
+ [新增] `services/request_shape.py`：聊天/生图请求头脱敏 shape hash 遥测。
+ [优化] Camoufox 注册默认走 **收件 OTP**（密码页点「使用一次性验证码注册」），避免 UDeal 下设密提交卡住。
+ [优化] Camoufox 注册/重登：监听 `oauth/token`（或 callback PKCE 换票）拿到 **RT 后再关浏览器**，并 `add_account_items` 入库；支持 `--relogin email password [proxy]`。
+ [修复] Camoufox 认证失败：读取页面 **body 正文**（封禁/停用关键词）并抛 `account_banned_or_disabled`，禁止只看 title 后盲重试。
+ [运维] `scripts/probe_signal_account.py` + `install_signal_probe_task.ps1`：每 30 分钟探测 yumail 信号号存活（默认 `mthomas4jl6@yumail.co`），走账号 sticky 代理。
+ [运维] `scripts/yumail_camoufox_openai_register.py`：yumail **池 acquire** + **Camoufox**（反检测 Firefox / Playwright API）跑 OpenAI 注册；等 OTP 时盯 CF/出错页。
+ [修复] yumail 收码：优先扫 `/pool/inbox`；`otp/poll` 覆盖中文主题「验证码」（Manage 默认 `verification` 会漏掉 ChatGPT 中文信）。
+ [运维] `scripts/yumail_incognito_openai_register.py`：Selenium Chrome 无痕路径（易被 CF 拦，保留作对照）。
+ [修正] `registration_disallowed` 不能直接等同于邮箱域名被封：yumail.co 手工浏览器可完成建号；协议路径 OTP 通过后卡在 `create_account`，优先怀疑指纹/出口/会话态。
+ [修复] `create_account`：先暖 `about-you`，对齐无 sentinel 再补发；错误文案不再误导“域名被封”。
+ [修复] 清除误写入的 `yumail.co` 本地域名熔断；canary 默认 `domain_rejection.enforce=false`。
+ [修复] 代理选择优先级改为 `account > explicit > runtime > legacy`，避免注册显式住宅代理被 `proxy_runtime.single_proxy`（如 40080）盖掉。
+ [修复] yumail OTP `POST /pool/otp/poll`：关闭 curl_cffi chrome impersonate（会吞 POST body 导致 FastAPI 422）；本地 API 用 `data=` 发 JSON。
+ [优化] yumail acquire 改为 `/pool/accounts` 列表轮转 + `data/yumail_openai_used.json` 去重，避免 FIFO 反复领同一邮箱。
+ [运维] `scripts/yumail_openai_register_canary.py`：选健康 Webshare 后跑协议注册 canary。

+ [新增] `yumail` 邮件 provider：对接 yumailManage `/api/v1/pool/*`（acquire/register + `otp/poll`）；API Key 可配或读 `data/runlogs/yumail_api_key.secret.txt`。
+ [优化] 邮件路由：仅当启用的 provider **全是** `outlook_token` 时才走真实浏览器；与 yumail 等并存时改走协议注册引擎，避免 Outlook 独占整单。
+ [UI] 注册页增加 yumail 类型（API Base / API Key / acquire|register）。
+ [测试] `test/test_yumail_mail_provider.py`；`test_real_browser_register` 增补 mixed provider 路由断言。
+ [运维] `scripts/probe_yumail_pool.py`：本机 `127.0.0.1:8780` catalog/accounts（可选 acquire）探针。
+ [优化] Webshare/住宅代理校验升级为 CSRF + 双次 egress hash 粘性；Panda 上传对 `webshare`/`udeal`/`sticky_one_ip_full` 保留账号级 proxy（`proxy_scope=account_sticky`）。

+ [新增] Panda Outlook 自动恢复循环：按间隔扫描异常/rejected Outlook，串行调用既有 recover-outlook；账号页提供开关与下次检测倒计时。
+ [配置] `outlook_auto_recovery`（默认 `enabled=false`、`interval_sec=1800`、`max_per_cycle=1`）；生图 inflight 不暂停。
+ [测试] `test/test_outlook_auto_recovery_loop.py`。

+ [修复] Outlook 恢复/读信：消费级账号在 `outlook.office365.com` 上 OAuth 成功但 SELECT 报 `authenticated but not connected` 时，自动回退 `outlook.live.com` / `imap.outlook.com`，并在 AUTH 后短暂 settle。
+ [修复] Graph 显式 `Mail.Read` 遇 `AADSTS70000` 时回退 `https://graph.microsoft.com/.default`；不再把仍可用的 refresh_token 误标 `token_invalid`。
+ [优化] Outlook 一键恢复增加 `mailbox_preflight`，登录发 OTP 前先确认邮箱可读；预检走 IMAP 成功时钉死 IMAP，避免后续再撞 Graph scope。
+ [测试] `test_register_mail_provider.py` / `test_recover_panda_outlook_accounts.py` 增补 IMAP host 回退、Graph `.default` 回退与预检失败快路径。

+ [修复] IMG-018：标准 `/v1/images/*` 同步过载不再返回空 `object=image.task` 的 HTTP 200；改为 **429 + Retry-After + `image_service_busy`**，避免 NewAPI/canvas 假成功空输出。
+ [新增] 同步 admission 与上游 `image_inflight` 解耦；新增 `newapi_image_sync_admission_max`（默认 12）与 `newapi_image_sync_admission_max_eta_secs`（默认 180，EWMA ETA 门）。
+ [优化] 上游开工 pacing：`image_task_queue.submit_start_min_interval_ms`（默认 1500）；小池 soft burst 可用 `burst_min_dispatchable_candidates=8`。
+ [优化] 显式 `panda-async` 提交信封对齐 `queue_position` / `estimated_start_after_secs`；同步成功响应附加非破坏字段 `task_id`。
+ [测试] `test/test_image_sync_admission_eta.py`；验收脚本 `scripts/img018_sync_admission_acceptance.py`。
+ [部署] Panda 2026-07-11：IMG-018 代码 + 升档 `global=6` / `per_user=2` / `submit_workers=2` / `admission_max=12` / `eta=180` / burst soft；备份 `backups/img018-sync-eta-pacing-20260711-092302/`。

+ [新增] IMG-012：`/v1/images/*` 非流式入口改为内部 sync-over-async（`submit image_task + wait_for_result`），解除同步全局 6 并发快拒。
+ [新增] `ImageTaskService.wait_for_result()` 与 `services/image_sync_adapter.py` 适配层。
+ [新增] `queue_coordinated` → `skip_global_limit` 链路，队列路径绕过 `image_global_concurrency=6`。
+ [新增] 部署/压测脚本：`scripts/img012_deploy.py`、`img012_patch_config.py`、`img012_verify.py`、`img012_newapi_sync_loadgen.py`、`img012_enable_burst_deploy.py`。
+ [优化] 默认 `per_user_running_max` 6、`image_return_window_size` 3；新增 `newapi_image_sync_wait_timeout_secs` 等配置。
+ [部署] Panda 2026-07-06：IMG-012 代码 + config patch；**16:41 restart** 后新代码生效。
+ [验收] NewAPI 24 路单轮压测：`busy_6=0` ✅；`5/24` 成功 ❌（19 失败为 NewAPI HTTP/2 断连，见 `reports/img012-newapi-sync-stage24-1rounds-20260706-164210/`）。
+ [文档] 精简 `docs/04-improvement-backlog.md`；同步 IMG-012 实施状态至 `08-image-pipeline-newapi-async-plan.md` §13、`02-current-state.md`、`06-handoff.md`。

## 1.5.0 - 2026-06-13

+ [新增] 新增 WARP / Privoxy / FlareSolverr 清障方案，注册遇到 Cloudflare 拦截后可刷新 clearance 并重试。
+ [新增] 新增 `outlook_token` 邮箱池，支持 Outlook/Hotmail 注册验证码读取。
+ [新增] 新增网页搜索兼容接口、图片编辑 mask 和图片任务相关能力。
+ [优化] 更新 sentinel/PoW 获取方式，提高上游请求兼容性。
+ [优化] 调整代理优先级和注册请求重试逻辑。

## 1.4.1 - 2026-06-03

+ [新增] 账号刷新改为异步模式，支持前端轮询刷新/重新登录进度。
+ [新增] 号池管理页面新增重新登录功能，支持密码登录恢复异常账号。
+ [新增] 刷新后自动重新登录异常账号（可在设置页开启）。
+ [新增] 图片生成支持并行模式，多张图片使用独立线程和账号同时生成。
+ [新增] 图片轮询超时自动换账号重试（最多4次），连接超时同账号递增等待重试。
+ [新增] 图片二次确认机制与先check再hit可配置化，关闭后可跳过等待直接返回结果。
+ [新增] 图片任务进度追踪，显示当前生成步骤（上传/预热/获取token/生成中等）。
+ [新增] 图片超时后续轮询功能，前端显示"继续等待"按钮。
+ [新增] 设置页新增图片二次确认、超时等待时间、自动重新登录等配置项。
+ [优化] 优化生图页面滚动加载性能，图片懒加载、会话切换滚动位置保存与恢复。

## 1.4.0 - 2026-05-31

+ [新增] 新增AI生成可编辑PSD文件逆向。
+ [新增] 新增AI生成可编辑PPT文件逆向。

## 1.3.1 - 2026-05-30

+ [新增] 新增ChatGPT搜索调试、Skills。

## 1.3.0 - 2026-05-30

+ [新增] 新增ChatGPT搜索接口逆向。

## 1.2.4 - 2026-05-30

+ [新增] 添加聊天补全缓存与重复请求合并。
+ [新增] 新增无限画布一键跳转功能

## 1.2.3 - 2026-05-29

+ [新增] 新增账号级代理。
+ [修复] 修复503异常信息、前端邮箱换行问题。

## 1.2.2 - 2026-05-29

+ [新增] 新增Codex链路生图、支持2k,4k。
+ [新增] 支持RT刷新账号信息。

## 1.2.0 - 2026-05-28

+ [新增] 当前版本基线，包含 Web 面板、画图、号池管理、注册机、图片管理、日志管理和设置能力。
+ [新增] 前端版本号支持点击查看版本更新弹窗，展示当前版本、最新版本和更新日志。
+ [优化] 优化注册机效率，成功率大幅提高。
+ [优化] 优化生图页面配置选项。
