# Changelog

## Unreleased

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
