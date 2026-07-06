# 当前状态

最后更新：2026-07-05

## 整体状态摘要

项目当前处于「功能可用、额度语义已在本地与生产对齐、维护文档已补齐」的状态。

- 本地与生产（`gptimage.relai.asia`）均已部署额度三态逻辑。
- Python 回归测试与前端构建已通过。
- 生产环境通过 bind-mount 热更新，未重建镜像、未动 `data/` 与 `config.json`。

## 已确认的功能域

### API 与兼容层

- 已支持 `POST /v1/images/generations`
- 已支持 `POST /v1/images/edits`
- 已支持 `POST /v1/chat/completions`
- 已支持 `POST /v1/responses`
- 已支持 `GET /v1/models`
- 已支持图片生成、图片编辑和部分文本 / 搜索兼容路径

### 网页端

- 已有账号管理页、图片工作台、日志页、设置页等主要页面
- 账号页支持搜索、筛选、分页、编辑、刷新、删除、导出
- 图片工作台支持生成、编辑、多图输入与历史查看
- 账号页额度展示为三态：`∞`（真无限额）/ `未知` / 数字

### 账号池与刷新

- 账号池支持刷新、异步进度追踪、重新登录、删除、导入和导出
- 已有失效 Token 自动清理逻辑
- 已有限流账号自动刷新与恢复时间同步逻辑
- **额度语义已归一**（详见 `docs/quota-semantics.md`）：
  - `image_quota_unknown` 不再等同于「无限额」
  - 真无限额仅 `Pro` / `ProLite`
  - 慢刷进度区分 `unlimited_quota` / `unknown_quota` / `quota_total`

### Panda / 同步链路

- 已有 Panda 增量同步链路
- 刷新后可把最近处理的账号交给同步链路
- 同步成功时可按配置删除本地账号
- 失败时会进入 pending 文件等待后续重试

### 代理 / 部署 / 存储

- 支持网页端代理配置
- 支持稳定代理运行时
- 支持 Docker 自托管
- 支持 JSON / SQLite / PostgreSQL / Git 存储后端切换

## 生产环境（Panda VPS）

| 项 | 值 |
| --- | --- |
| SSH 别名 | `panda`（`root@100.69.228.93`） |
| 项目路径 | `/root/gptimage` |
| Compose 文件 | `docker-compose.panda.yml` |
| 容器名 | `chatgpt2api-local` |
| 镜像 | `chatgpt2api:local`（本地 build，非 ghcr 拉取） |
| 对外端口 | `8012` → 容器 `80` |
| 公网域名 | `https://gptimage.relai.asia` |
| 部署模式 | **bind-mount 热更新**：改代码 / 前端静态文件后 `docker compose up -d` 或 restart |

### 当前 bind-mount 文件（热更新范围）

- `data/`、`config.json`
- `web_dist/`（前端静态产物）
- `api/app.py`、`api/support.py`、`api/accounts.py`、**`api/system.py`**
- `services/account_service.py`
- `services/account_refresh_all_service.py`
- `services/account_maintenance_loop_service.py`
- `services/config.py`
- `services/protocol/openai_v1_models.py`

未 bind-mount 的代码仍在镜像内；若需更新其他模块，要加 mount 或 `docker compose build`。

### 2026-06-29 额度语义生产部署

- **备份目录**：`/root/gptimage/backups/quota-fix-20260629-235620/`
- **变更文件**：`account_service.py`、`account_refresh_all_service.py`、`api/system.py`、`web_dist/`、`docker-compose.panda.yml`（新增 `system.py` mount）
- **部署前**：`unlimited_quota_count=1`，响应无 `unknown_quota_count` 字段（旧逻辑误报）
- **部署后**：`unlimited_quota_count=0`，`unknown_quota_count=0`，`healthy=true`，`status=ok`
- **回滚**：见 `docs/deployment.md`「Panda 生产热更新与回滚」

## 当前已验证事实

### 本地

```bash
python -m pytest test/test_account_image_capabilities.py -q   # 20 passed
python -m pytest test/test_account_refresh_all_service.py -q    # 10 passed
cd web && npm run build                                        # 成功
```

### 生产（2026-07-03 文档同步时）

- `https://gptimage.relai.asia/health?format=json` 返回 `200`
- `version=1.5.0`
- `healthy=true`
- `accounts.unlimited_quota_count` 与 `accounts.unknown_quota_count` **字段均存在**
- 部署后未见服务异常；容器日志以 `200 OK` 为主

> 注：`unknown_quota_count=0` 表示当时号池无「正常 + 非 Pro + image_quota_unknown」账号，不代表逻辑未生效。部署瞬间曾存在 1 个误计入 unlimited 的 unknown 账号，部署后已纠正为 0/0。

## 进行中事项

- 注册链路的瞬断 / 超时仍会在诊断日志里出现，需要继续观测
- Panda 同步与本地删除链路需要继续监控实际运行表现
- 刷新 → 增量同步 → 本地删除链路收口（路线图 Now #2）

## 已知阻塞与风险

- 生产热更新依赖 bind-mount 清单；漏加 mount 会导致「主机文件已改、容器仍跑旧代码」
- 注册和验号链路对代理 / 网络抖动敏感，失败可能是 transient，不一定是账号失效
- 旧专题文档仍在 `docs/` 中，容易和当前维护主档混淆

## 下一步

1. 账号页 spot-check：出现 unknown 账号时应显示「未知」而非 `∞`
2. 继续观察注册后验号诊断日志，区分 transient 和 invalid
3. 继续观察 Panda 同步、pending 重试和本地删除行为
4. 推进 OPS-001：健康页 JSON 定期检查或简单告警

## 与旧文档的关系

- 根目录 `README.md` 是对外介绍，不是内部维护真相源
- **`docs/quota-semantics.md`** 是额度三态的专题规范
- `docs/feature-status.en.md` 仍可作专题参考，但不替代本文件
- 其他旧专题文档只作补充，不作事实主档


## 2026-07-03 新增计划：账号池与生图性能升级

状态：**计划已落地文档，代码尚未执行**。

本轮新增维护入口：

- 根目录 `plan.md`：后续执行主计划。
- `docs/07-account-pool-performance-upgrade.md`：账号池、本地 SQLite、Panda SQLite、maintenance、b64 回传落地设计。
- `docs/sync-strategy.md`：Panda 水位线、动态公网 IP 同步保护、HMAC、幂等和限频。
- `docs/performance-acceptance-test-plan.md`：多轮测试、压测和验收计划。

已确认的新边界：

- 新号本地探测时间采用更保守的 `1h / 3h / 6h`，三次通过后才允许进入 Panda 上传队列。
- Panda 水位线计划提高到 `high=1500`、`low=500`，并保留 `emergency=200`、`critical=100`。
- 本地公网 IP 经常变化，因此公网同步入口保护不依赖固定 IP allowlist，改用限频、强鉴权、HMAC、nonce、idempotency 和应用层水位限流。
- `https://canvas.best/` 是外部公共调用方，不作为内部无线画布队列处理；公共 API 只允许短等和快拒绝，不做无限等待队列。

待执行的高优先级风险修复：

1. 修复 Panda `dictionary changed size during iteration` 并发读写 bug。
2. 限制公网 `import-batch` 高频调用并加入水位限流。
3. 本地账号池从高频文件写入迁移到 SQLite 主存储。
4. Panda 账号存储从全量保存语义升级为增量 SQLite / 批量 transaction。
5. 生图 preflight、maintenance、mark_image_result 降低写盘并错峰。
6. b64 大响应回传增加独立窗口，避免尾部拥塞拖垮公共 API。


## 2026-07-04 新增事实：生图并发 99+ Panda 生产部署版

状态：**本地代码已落地并通过受影响测试；生产 Panda 已部署并完成受控验收**。

已确认事实：

- Panda 当前 `image_global_concurrency=6`、`image_account_concurrency=1`；6 worker 体验明显优于 12 worker。
- 12 worker 压测时 CPU、内存、磁盘、带宽都未打爆，但 p95/尾流、524、连接重置、远端断开明显恶化。
- 当前主要瓶颈不是 Panda CPU，而是上游长尾、账号质量、Cloudflare/NewAPI 长连接和 timeout 后换号重试放大。
- Panda CPU 当前仍有空闲，因此后续生图调度模型可从 `1.0 vCPU` 预算提升到 `1.5 vCPU`；但 `CPU >= 90%` 必须作为 deadlock_guard 熔断线，不允许作为常态目标。

新增执行边界：

- 同步 OpenAI/NewAPI 兼容入口保持短等/快拒绝，不做无限队列。
- 高并发进入异步任务队列：中央调度、SQLite 任务状态、timeout_pending 续轮询。
- 任务拿到 `conversation_id` 后，poll timeout 不允许换号重新提交同一张图。
- worker 采用 6 -> 8 -> 10 阶梯和自适应升降档；12 不作为常驻目标，除非多桶/多出口/多实例后重新验收。

本地已完成：

- `services/image_task_service.py`：从“提交即开线程”改为 SQLite 持久化中央队列；新增 `timeout_pending`、per-user running 限制、队列满快拒绝、后台续轮询。
- `services/protocol/conversation.py`：拿到 `conversation_id` 后 poll timeout 直接转 `image_timeout_pending`，不再换号重开图。
- `services/image_deadlock_guard_service.py`：新增按 1.5 vCPU 预算归一的 CPU 熔断器。
- `services/account_maintenance_loop_service.py`：deadlock_guard 触发时 maintenance 直接 pause。
- `api/image_tasks.py`：异步队列满或熔断时返回 `429 + Retry-After`。
- `api/app.py` / `services/register_service.py`：修复注册服务 import 即 auto-start 的副作用，改为 FastAPI lifespan 进入时按 enabled 恢复。

本地验证：

```bash
python -m pytest test/test_image_task_service.py test/test_image_tasks_api.py test/test_account_maintenance_loop_service.py test/test_v1_images_generations.py test/test_v1_images_edits_api.py test/test_account_image_capabilities.py test/test_account_refresh_all_service.py test/test_json_storage.py test/test_image_storage_service.py test/test_multi_image_results.py test/test_register_service_panda_batch.py -q
# 71 passed

python -m py_compile services/image_task_service.py services/image_deadlock_guard_service.py services/config.py services/protocol/conversation.py services/account_maintenance_loop_service.py services/backup_service.py services/register_service.py api/app.py api/image_tasks.py
# passed
```

生产部署与验收（2026-07-04）：

- 生产目录：`ssh panda` → `/root/gptimage`
- 生产备份：`/root/gptimage/backups/p6-image-queue-20260704-175026/`
- 回滚脚本：`/root/gptimage/backups/p6-image-queue-20260704-175026/ROLLBACK.sh`
- 部署文件：`api/app.py`、`api/image_tasks.py`、`services/config.py`、`services/account_maintenance_loop_service.py`、`services/image_task_service.py`、`services/image_deadlock_guard_service.py`、`services/backup_service.py`、`services/register_service.py`、`services/protocol/conversation.py`
- 容器资源：`NanoCpus=1500000000`，`Memory=1610612736`；即 Panda app 从 1.0 vCPU 提升到 1.5 vCPU 预算。
- 本地与公网健康页：`/health?format=json` 均返回 `healthy=true`。
- `https://gptimage.relai.asia/image-manager/`：`HTTP=200`，公网约 `44ms`，本机约 `3ms`。
- 生产证据文件：`/root/gptimage/backups/p6-image-queue-20260704-175026/post-deploy-validation.json`

受控压测：

- 12 个异步生图任务一次性提交，提交接口全部 `200 queued`，总提交耗时约 `0.49s`，提交 p50 约 `421ms`，p95 约 `448ms`。
- 后台 per-user running 实测为 `2`，没有把 12 个任务同时推给同一用户。
- 12/12 最终 `success`，无 `error`、无 `timeout_pending`。
- 完成耗时：min `38s`，p50 `202.5s`，p95 近似 `295s`，max `304s`。
- 压测后容器资源：CPU 约 `0.39%` 空闲态，内存 `251MiB / 1.5GiB`；压测期间峰后观测 CPU 约 `6.32%`，内存约 `240MiB`。
- 严格日志检查：`Traceback`、`dictionary changed`、`image service busy`、HTTP `5xx`、`524/502`、`connection reset`、`timeout_pending` 命中数为 `0`。

待完成：

- 未做 R5.5 真实 100 异步任务压测、CPU 熔断压测和 b64 回传窗口验收。
- `data/image_tasks.db` 已成为异步图片任务主状态文件，备份链路已纳入 `image_tasks.db/-wal/-shm`。

### 2026-07-04 R5.6 Stage A：18 async tasks 公网混合输入压测

状态：**已执行；18/18 成功；严格门槛下不进入 24**。

- 发起方式：本地机器经公网 `https://gptimage.relai.asia` 并发提交，不从 Panda 本机压测。
- 输入：6 个文生图、12 个图生图；8 个单参考图、4 个双参考图；未做输入减重。
- 总 payload：约 `29.37MB`；参考图原始 PNG 总量约 `22.02MB`。
- 报告：
  - 本地 `reports/loadtest-20260704-191600-stage-18/`
  - Panda `/root/gptimage/backups/loadtest-20260704-191600-stage-18/`
- 结果：`18/18 HTTP 200 queued`，最终 `18/18 success`，`timeout_pending=0`，`error=0`，严格错误日志 `0`。
- Panda 资源：CPU p95 `13.78%`，内存峰值 `526.6MiB`，健康页 p95 `13.07ms`。
- Panda 带宽：总带宽 p95 `14.56Mbps`，max `25.25Mbps`，`>=24Mbps` 最长连续 `5s`，未达到 60s 停止线。
- 公网提交：整体 p95 `49.32s`；图生图提交 p95 `50.96s`。
- 公网状态查询：p95 `4.88s`。

判断：

- Panda 端没有 CPU / 内存 / 健康页 / 持续带宽瓶颈。
- 当前瓶颈优先暴露在公网大参考图上传和公网状态查询路径。
- 按 R5.6 严格进入下一档条件，`submit_p95 < 1s` 和 `status_query_p95 < 500ms` 未通过，因此不直接进入 24。

### 2026-07-04 R5.6 Stage B：24 async tasks 公网混合输入压测

状态：**已执行；不进入 30**。

- 用户确认真实大参考图上传不再要求 `submit_p95 < 1s` 后执行。
- 发起方式：本地机器经公网 `https://gptimage.relai.asia` 并发提交。
- 输入：8 个文生图、16 个图生图；10 个单参考图、6 个双参考图；未做输入减重。
- 总 payload：约 `40.38MB`；参考图原始 PNG 总量约 `30.28MB`。
- 报告：
  - 本地 `reports/loadtest-20260704-194005-stage-24/`
  - Panda `/root/gptimage/backups/loadtest-20260704-194005-stage-24/`
- 提交：`22/24 HTTP 200 queued`，`2/24 HTTP 429`。
- 429 原因：`image task queue is full for current user (20/20)`。
- 已接纳任务：`21/22 success`，`1/22 error`，`timeout_pending=0`。
- 唯一错误：`'ConfigStore' object has no attribute 'proxy_url'`。
- Panda 资源：CPU p95 `7.77%`，内存峰值 `594.2MiB`。
- Panda 带宽：总带宽 p95 `22.31Mbps`，max `298.20Mbps`，`>=24Mbps` 最长连续 `49.14s`。
- 公网状态查询：p95 `6.37s`。

判断：

- CPU / 内存不是 24 档瓶颈。
- 当前单用户队列上限实际只允许约 `20 queued + 2 running = 22`，无法完整接纳 24。
- 24 档同时暴露 `ConfigStore.proxy_url` 错误、状态查询慢和带宽警戒。
- 不进入 30；先修或重新定义队列上限语义。

## 2026-07-04 新增事实：P6-12 队列接纳修复与 24 档复测

状态：**Panda 已部署；24 档仍未通过；不进入 30**。

已部署：

- `ConfigStore.proxy_url` 兼容属性，修复续轮询路径属性缺失。
- `image_task_queue.per_user_queue_max` 默认提高到 `36`，执行并发仍保持 `submit_workers=6`、`per_user_running_max=2`。
- 新增轻量状态查询：`GET /api/image-tasks/status?ids=...`，不返回 `data` / `payload` / 图片结果大字段。

生产备份：

- `/root/gptimage/backups/p6-queue36-status-20260704-210512/`
- 回滚：`/root/gptimage/backups/p6-queue36-status-20260704-210512/ROLLBACK.sh`

生产验收：

```text
health: healthy=true, status=ok
submit_workers=6
per_user_running_max=2
per_user_queue_max=36
GET /api/image-tasks/status: contains_data_key=false, contains_payload_key=false
```

本地验证：

```text
python -m py_compile services/config.py services/image_task_service.py api/image_tasks.py
python -m pytest test/test_image_task_service.py test/test_image_tasks_api.py -q
# 13 passed
python -m pytest test/test_image_task_service.py test/test_image_tasks_api.py test/test_account_maintenance_loop_service.py test/test_v1_images_generations.py test/test_v1_images_edits_api.py -q
# 21 passed
```

R5.6 Stage C 24 复测：

- 本地报告：`reports/loadtest-20260704-211253-stage-24/`
- Panda 报告：`/root/gptimage/backups/loadtest-20260704-211253-stage-24/`
- 输入：8 文生图 + 16 图生图，总 payload 约 `40.38MB`，参考图原始 PNG 总量约 `30.28MB`，未输入减重。
- 结果：24 请求中 19 入队，5 个在公网大参考图上传阶段连接失败；入队任务最终 18 success / 1 error。
- 5 个未入队错误：`ConnectionResetError(10054)` / `RemoteDisconnected('Remote end closed connection without response')`。
- 已入队错误：`/backend-api/files failed: status=500, body=`。

结论：

- 旧的 `20/20` 队列接纳问题已修复。
- 旧的 `ConfigStore.proxy_url` 属性缺失问题已修复。
- 当前 24 档瓶颈转为“单阶段大参考图公网上传 / 上游文件上传”，不是 Panda CPU、内存或 worker 数。
- 不进入 30；下一步必须做上传链路解耦和上传窗口保护。

## 2026-07-04 新增事实：本地 40080 / TempMail 注册链路修复

状态：**本地已修复主注册网络面；WSL/FlareSolverr 仍需系统层恢复**。

已确认事实：

- 网线环境下原 40080 故障链为：`127.0.0.1:40080 -> Docker privoxy -> WARP`，WARP 在有线网络下出现 `Network: unstable`、CONNECT 503、TLS timeout。
- `scripts/warp_health_monitor.ps1` 已改为不因 `Network: unstable` 单独重启，避免 30 秒自激重启造成 CONNECT 503 峰值。
- 40080 入口已保留，上游改为本机稳定代理桥：`40080 -> 172.21.0.1:17897 -> Windows 127.0.0.1:7897`；当前 WSL 异常时临时启用 Windows 侧兜底 `127.0.0.1:40080 -> 127.0.0.1:7897`。
- 当前 WSL 发行版 `HermesUbuntu` 启动失败：`Wsl/Service/CreateInstance/E_UNEXPECTED`；本进程非管理员，无法启动 `LxssManager`，因此 FlareSolverr 容器 `8191` 暂不可用。
- 40080 兜底连通性：`auth.openai.com` 5/5 返回 HTTP 403（可达），`chatgpt.com/cdn-cgi/trace` 5/5 返回 HTTP 200，`api.tempmail.lol` 5/5 返回 HTTP 200，无 CONNECT 503 / timeout。
- 5 线程 / 50 次注册验证（补丁前）：17 success、2 fail、31 transient；transient 主因从 CONNECT 503 转为 `TempMail.lol HTTP 429 Rate limited (free)` 和少量 `curl(35) TLS connect error`；2 个真实失败均在 `token exchange` 阶段。
- 已新增 `TempMail.lol` provider 级全局限速：默认 `/inbox/create` 间隔 `12s`，HTTP 429 后按 `Retry-After` 或默认 `60s` 全局退避，可用 provider 配置 `create_min_interval_sec` / `rate_limit_backoff_sec` 覆盖。
- 已修复 token exchange 诊断：失败时输出 HTTP 状态、URL、content-type、request id / cf-ray 和脱敏 body，不再只显示 `token换取失败`。
- 补丁后 5 线程 / 8 次注册验收：8/8 success，0 fail，0 transient；新增 post-verify 诊断 8/8 ok。

本地验证：

```text
python -m py_compile services/register/mail_provider.py services/register/openai_register.py services/proxy_service.py scripts/host_proxy_forwarder.py
# passed
python -m pytest test/test_proxy_service.py test/test_register_proxy_runtime.py test/test_register_mail_provider.py test/test_register_worker.py test/test_register_worker_transient_redaction.py test/test_register_service_panda_batch.py -q
# 30 passed
```

待确认：

- 需要管理员权限或系统重启恢复 WSL / `LxssManager` 后，再恢复 Docker privoxy + FlareSolverr 完整清障链路并复测 `/api/proxy/clearance/test`。
- 当前 Windows 侧 40080 兜底可恢复注册主链路，但不提供 FlareSolverr 清障能力；遇到 Cloudflare challenge 时仍依赖 WSL/Docker 恢复。

## 2026-07-05 新增事实：本地 40080 空跑止损与自启防线

状态：**WSL 发行版已恢复，注册空跑已停止；40080 Windows 侧兜底已恢复；Docker/FlareSolverr 仍不可用**。

已确认事实：

- `HermesUbuntu` 当前可启动，裸 `wsl.exe -d HermesUbuntu` 默认进入 `lenovo`；`/mnt/d/SelfMadeTool/AutoRegister/gptimage` 可访问。
- WSL 内当前没有可用 `docker` 命令，因此完整 Docker privoxy + FlareSolverr 链路仍不能恢复，`127.0.0.1:8191` 仍未监听。
- 本地端口现状：`127.0.0.1:7897`、`127.0.0.1:8000`、`127.0.0.1:40080` 已监听；`8191`、`17897` 未监听。
- 40080 当前由 Windows 侧 `scripts/host_proxy_forwarder.py` 兜底转发到 `127.0.0.1:7897`。
- 注册服务曾在 `enabled=true`、`threads=20`、`proxy=http://127.0.0.1:40080` 且 40080 缺失时持续空跑，累计 `curl: (7) Failed to connect to 127.0.0.1 port 40080` 失败。
- 已通过 `/api/register/stop` 停止注册任务；最终运行态为 `enabled=False`、`running=0`。

本地防线：

- `services/register/openai_register.py` 与 `services/register/mail_provider.py` 已将 `curl: (7)`、`failed to connect`、`could not connect to server` 归类为 transient，避免代理端口瞬断被计为真实注册失败。
- `services/register_service.py` 已在启动注册前检查 loopback 代理端口；本地代理不可用时拒绝启动并保持 `enabled=false`，避免后端重启后自动空跑。

本地验证：

```text
python -m pytest test/test_register_worker.py test/test_register_service_panda_batch.py test/test_register_mail_provider.py -q
# 6 passed

python -m py_compile services/register_service.py services/register/openai_register.py services/register/mail_provider.py
```

## 2026-07-05 新增事实：本地慢刷并发上限解除

状态：**本地已修改并重启后端**。

背景：

- 本地账号池约 5.6k，Panda 号池空时需要快速筛活。
- 页面填写慢刷并发 `100` 时显示 `100 -> 4`。

根因：

- `config.json` 中 `account_refresh_all.max_concurrency=4`。
- `services/account_refresh_all_service.py` 的 `AccountRefreshAllOptions.from_mapping()` 旧逻辑把配置 max_concurrency 当绝对上限，并且代码硬封顶 `16`。

已完成：

- 后端慢刷请求归一化改为：配置 max 只作为默认值；请求显式传 `max_concurrency` 时可提高，保留 `512` 作为防误操作保险。
- 本地 `config.json` 调整为：

```text
account_refresh_all.concurrency=64
account_refresh_all.max_concurrency=128
account_refresh_all.batch_size=200
account_refresh_all.delay_between_accounts_sec=0.05
account_refresh_all.delay_between_batches_sec=0.2
```

验证：

```text
python -m py_compile services/account_refresh_all_service.py services/config.py api/accounts.py
# passed
python -m pytest test/test_account_refresh_all_service.py -q
# 13 passed
请求 100 -> concurrency 100
请求 999 -> concurrency 512
本地 API /api/settings 返回 concurrency=64, max_concurrency=128
```

运行状态：

- 本地后端 `127.0.0.1:8000` 已重启，新进程已监听。
- 前端 `127.0.0.1:3000` 未重启，无需变更。

注意：

- 这不是无限并发；保留 512 防误操作。
- 当前 `delay_between_accounts_sec` 仍是全局节流锁，默认 0.05 约等于最多 20 个账号/秒启动速率。

## 2026-07-05 追加事实：本地 40080 / WSL 安全防线加固

状态：**本地已加固并验收；未触碰 WSL Docker 启动路径**。

新增事实：

- 本地后端已重启加载最新源码；健康页 `healthy=true`、`status=ok`。
- 当前账号存储为 SQLite：`data/accounts.db`，健康页 `account_count=43`，`accounts.total=43`。
- 注册任务保持停止：`enabled=false`、`running=0`，代理已恢复为 `http://127.0.0.1:40080`。
- 40080 Windows 侧兜底仍可达：`auth.openai.com` 返回 HTTP 403、`chatgpt.com/cdn-cgi/trace` 返回 HTTP 200、`api.tempmail.lol` 返回 HTTP 200。
- 真实 API 防线验收：临时把注册代理改为 `http://127.0.0.1:9` 后调用 `/api/register/start`，返回 `enabled=false`、`running=0`，日志为 `拒绝启动注册任务：本地代理不可用 127.0.0.1:9`；随后已恢复为 `http://127.0.0.1:40080`。

新增代码防线：

- `services/register/openai_register.py` 与 `services/register/mail_provider.py` 将 `connection refused`、`actively refused` 归为 transient，覆盖 Windows 连接拒绝报错。
- `test/test_register_service_panda_batch.py` 增加 `start_if_enabled` 与 `auto_start` 自启防线测试，防止后端重启时代理缺失仍空跑。
- `scripts/start_proxy_stack.ps1` 启动前先检查 WSL 项目路径、`docker`、`docker info`、`docker compose`；缺失时快速失败，不进入启动脚本。
- `scripts/start_proxy_stack_wsl.sh` 默认 safe mode：没有 `docker` 命令或 Docker daemon 不可用时退出；只有显式设置 `GPTIMAGE_WSL_ALLOW_DOCKER_START=true` 才会尝试启动 Docker/dockerd。
- `scripts/warp_health_monitor.ps1` 在重启代理栈前增加 WSL/Docker preflight；Docker 缺失时只记录 `proxy_stack_restart_skipped`，不反复打 WSL。

本地验证：

```text
python -m pytest test/test_register_service_panda_batch.py test/test_register_worker.py test/test_register_mail_provider.py -q
# 10 passed

python -m pytest test/test_proxy_service.py test/test_register_proxy_runtime.py test/test_register_mail_provider.py test/test_register_worker.py test/test_register_worker_transient_redaction.py test/test_register_service_panda_batch.py -q
# 36 passed

python -m py_compile services/register_service.py services/register/openai_register.py services/register/mail_provider.py scripts/host_proxy_forwarder.py
# passed

PowerShell parser:
# PARSE_OK scripts/start_proxy_stack.ps1
# PARSE_OK scripts/warp_health_monitor.ps1

wsl.exe -d HermesUbuntu -- bash -n /mnt/d/SelfMadeTool/AutoRegister/gptimage/scripts/start_proxy_stack_wsl.sh
# passed

powershell -File scripts/start_proxy_stack.ps1 -TimeoutSeconds 5
# 预期快速失败：docker command is not available in WSL
```

仍待处理：

- WSL 内当前没有可用 `docker` 命令，因此 Docker privoxy + FlareSolverr 完整清障链仍未恢复；当前只保证注册主链路通过 Windows `40080 -> 7897` 兜底可用。

## 2026-07-05 追加事实：注册平均耗时与 Docker/FlareSolverr 状态复查

状态：**只读诊断；未启动 Docker，未改注册配置**。

已确认事实：

- 当前注册配置代理为 `http://127.0.0.1:7897`，不是 `40080`；`40080` 仍由 Windows 侧 forwarder 监听，仅作为兼容入口。
- UI `平均注册单个` 对应后端 `avg_seconds`，计算口径是 `从任务启动到当前的总运行时间 / success`，不是单任务各阶段耗时的均值。
- 当前慢点集中在 `tempmail_lol` 创建邮箱阶段：provider 默认 `create_min_interval_sec=12.0`，多个线程并发启动时实际变成每约 12 秒放出一个邮箱创建请求。
- 实测日志：任务 1 邮箱创建约 3.0s，任务 2 等约 12.7s，任务 3 等约 24.6s，任务 4 等约 36.6s，任务 5 等约 48.7s；后续 OpenAI 注册、验证码、token 阶段多数为个位数秒。
- 当前 Windows 无 `docker` 命令，未发现默认 Docker Desktop 安装目录；WSL `HermesUbuntu` 内无 `docker`、无 `/var/run/docker.sock`，因此 Docker privoxy + FlareSolverr 不能直接恢复。
- 40080 与 7897 curl 探测延迟同级：40080 只是本地 TCP forwarder，多出的本机转发开销不是当前 15~20s 平均耗时来源。

## 2026-07-05 追加事实：取消 tempmail_lol 邮箱创建全局节流后的 5线程/50次复测

状态：**本地已按用户要求取消邮箱创建全局锁/串行/等待；已重启后端并完成 5线程/50次复测**。

代码改动：

- `services/register/mail_provider.py`：`TempMailLolProvider.create_mailbox()` 不再调用 `_reserve_tempmail_lol_create_slot()`。
- `services/register/mail_provider.py`：`TempMailLolProvider._request()` 遇到 HTTP 429 不再写入全局 backoff。
- `test/test_register_mail_provider.py`：更新测试，验证即使配置 `create_min_interval_sec=60` 也不会调用全局 rate slot；HTTP 429 不更新全局 backoff。

验证：

```text
python -m pytest test/test_register_mail_provider.py test/test_register_worker.py test/test_register_service_panda_batch.py -q
# 10 passed

python -m pytest test/test_proxy_service.py test/test_register_proxy_runtime.py test/test_register_mail_provider.py test/test_register_worker.py test/test_register_worker_transient_redaction.py test/test_register_service_panda_batch.py -q
# 36 passed
```

5线程/50次复测结果：

```text
proxy=http://127.0.0.1:7897
threads=5
total=50
success=32
fail=1
transient=17
elapsed=311.6s
avg_seconds=9.7s
success_rate=97.0%  # success/(success+fail)，transient 不计入分母
```

transient / fail 分布：

- `TempMail.lol 请求失败: POST /inbox/create, HTTP 429, body={"error":"Rate limited (free)"}`：10 次。
- 注册后验号 transient：4 次，主要是 `/backend-api/accounts/check/v4-2023-04-27 failed: HTTP 403`、`/backend-api/conversation/init failed: HTTP 403`、`post_register_verification_no_refresh_success`。
- Cloudflare / clearance 失败：1 次真实失败，触发 `Just a moment...`，原因是当前 FlareSolverr `127.0.0.1:8191` 不可用，clearance 刷新未返回可用 Cookie。

判断：

- 邮箱创建全局串行已取消，平均耗时从 15~20s 回落到约 9.7s，前段一度约 4~6s。
- 429 不是由邮箱创建锁导致；取消锁后仍由官方 `https://api.tempmail.lol/v2/inbox/create` 返回真实 HTTP 429。
- 已清理 `tempmail_lol` 的历史第三方邮箱字段残留；当前生效配置只保留 `type/enable/api_key/domain`，实际请求官方 `https://api.tempmail.lol/v2`。
- 当前注册代理为 `http://127.0.0.1:7897`，不是 Docker WARP/privoxy 40080；出口变化也可能影响 TempMail.lol free rate limit。

FlareSolverr 旧链路复原：

- 旧 compose：`docker-compose.warp.yml`，服务为 `warp-proxy`、`privoxy`、`flaresolverr`、`app`。
- 旧 privoxy 备份：`data/backups/proxy-40080-hostproxy-20260704-213142/privoxy-warp.conf`，内容为 `forward-socks5t / warp-proxy:1080 .`。
- 旧端口：`warp-proxy 127.0.0.1:40000->1080`，`privoxy 127.0.0.1:40080->8118`，`flaresolverr 127.0.0.1:8191->8191`。
- 当前 privoxy 已被改为 `forward / 172.21.0.1:17897`，即容器内 privoxy 走 WSL host bridge 到 Windows `7897`。



### 2026-07-05 本地 WSL WARP 链状态

- 当前注册代理：`http://127.0.0.1:40080`。
- 当前旧链：`40080 -> Docker privoxy -> warp-proxy:1080 -> WARP`。
- 当前容器：`chatgpt2api-warp-proxy`、`chatgpt2api-privoxy`、`chatgpt2api-flaresolverr` 均 healthy。
- WSL Docker daemon 已恢复；若 WSL 重启后 daemon 未运行，可用 `powershell -File scripts/start_proxy_stack.ps1 -AllowDockerStart` 显式恢复，脚本会为 dockerd 保留代理环境用于拉镜像。

## 2026-07-05 追加事实：WSL Docker Engine 与旧 WARP 链已恢复

状态：**本地注册网络主链路恢复为旧 Docker WARP 链；Windows 40080->7897 仅保留为历史兜底，不再是当前主路径**。

本轮恢复目标：

- 清理 `tempmail_lol` provider 中历史第三方邮箱字段残留，避免误判邮箱 provider 仍在走非官方邮箱接口。
- 恢复 WSL `HermesUbuntu` 内 Docker Engine。
- 恢复旧代理链：`127.0.0.1:40080 -> Docker privoxy -> warp-proxy:1080 -> WARP`。
- 恢复 FlareSolverr 入口 `127.0.0.1:8191`，让后续 Cloudflare clearance 链路具备容器侧基础条件。
- 保持注册任务停止，不在链路恢复过程中继续烧任务或把瞬断刷成失败。

关键现状：

- 注册运行态代理已热更新为 `http://127.0.0.1:40080`。
- 注册任务保持停止：`enabled=false`。
- `data/register.json` 中 `tempmail_lol` provider 当前只保留 `type/enable/api_key/domain`。
- 当前 Docker compose 容器：`chatgpt2api-warp-proxy`、`chatgpt2api-privoxy`、`chatgpt2api-flaresolverr` 均为 healthy。
- WARP 状态为 `Connected / Network healthy`。
- 40080 出口经 Cloudflare trace 验收为 `warp=on`、`loc=US`。
- TempMail 官方 API 经 40080 验收 HTTP 200。
- FlareSolverr 根路径经 Windows localhost 验收 HTTP 200。

本轮实际根因链：

1. `127.0.0.1:40080` 一度被 Windows 侧 `host_proxy_forwarder.py -> 7897` 占用，不是 Docker privoxy。
2. WSL Docker Engine 起初未安装/不可用，compose 没有实际容器在跑。
3. 安装 Docker Engine 后，`dockerd` 直连 Docker Hub 拉镜像超时；普通 WSL `curl` 可走代理，但 `dockerd` 进程没有继承 proxy 环境。
4. 给 `dockerd` 显式注入 HTTP/HTTPS proxy 后，镜像拉取成功，旧 Docker compose 链恢复。
5. 文件配置先改为 40080 后，运行中的 `register_service` 内存仍停留在 7897；必须通过 `/api/register` 热更新运行态，才算真正切回 40080。

本轮修复结果：

- 停止 Windows 侧 `host_proxy_forwarder.py 127.0.0.1:40080 -> 127.0.0.1:7897`，释放 40080。
- 使用 `docker-compose.warp.yml` 启动 `warp-proxy`、`privoxy`、`flaresolverr`。
- 恢复 `scripts/privoxy-warp.conf` 为 `forward-socks5t / warp-proxy:1080 .`。
- 修补 `scripts/start_proxy_stack.ps1`：新增 `-AllowDockerStart`，默认仍安全；显式允许时才让 WSL 脚本恢复 dockerd。
- 修补 `scripts/start_proxy_stack_wsl.sh`：显式启动 dockerd 时保留 HTTP/HTTPS proxy 环境，避免 Docker Hub 拉镜像绕过 7897 直连超时。
- 回写 `docs/logs/2026/2026-07.md` 和本 current-state，避免后续继续按“Docker/FlareSolverr 未恢复、注册代理 7897”处理。

验收摘要：

- `SCRIPT_PARSE_OK`：PowerShell parser 与 `bash -n start_proxy_stack_wsl.sh` 均通过。
- `/api/register` 运行态：`proxy=http://127.0.0.1:40080`，`enabled=False`，provider keys 为 `type,enable,api_key,domain`。
- 40080 出口：Cloudflare trace HTTP 200，`warp=on`、`loc=US`。
- TempMail：经 40080 HTTP 200。
- FlareSolverr：`127.0.0.1:8191` HTTP 200。
- Docker compose：三个代理/清障容器均 healthy。

后续接手注意：

- 如果 WSL 重启后 40080 不通，先确认是不是 Docker daemon 未运行；不要直接恢复 Windows `40080 -> 7897` 兜底，除非明确要临时绕过 WARP/FlareSolverr。
- 如果 Docker pull 再次超时，优先检查 `dockerd` 进程是否继承了 HTTP/HTTPS proxy，而不是只测 WSL 里的 `curl`。
- 如果手动改了 `data/register.json`，还要同步热更新运行中的 `/api/register`，否则页面/任务可能继续使用旧内存配置。
- 不要在代理链恢复过程中启动大注册任务；先完成 40080、8191、WARP、TempMail 四项验收。

## 2026-07-05 追加事实：3轮 24 并发公网混合输入压测

状态：**3轮已完成；生产未变更；本地发现并修复一个续轮询 P0 bug，尚未部署 Panda**。

执行方式：

- 每轮 24 个 async image task。
- 每轮输入一致：8 文生图 + 16 图生图；10 单参考图 + 6 双参考图。
- 总 payload 每轮约 `40.38MB`；参考图原始 PNG 每轮约 `30.28MB`。
- 不做输入减重。
- 本地经公网 `https://gptimage.relai.asia` 发起；Panda 只监控。

报告：

```text
Round 1 local: reports/loadtest-20260705-143006-stage-24/
Round 1 panda: /root/gptimage/backups/loadtest-20260705-143006-stage-24/
Round 2 local: reports/loadtest-20260705-150923-stage-24/
Round 2 panda: /root/gptimage/backups/loadtest-20260705-150923-stage-24/
Round 3 local: reports/loadtest-20260705-154552-stage-24/
Round 3 panda: /root/gptimage/backups/loadtest-20260705-154552-stage-24/
Aggregate local: reports/stage24-3rounds-20260705/aggregate-summary.json
Aggregate panda: /root/gptimage/backups/stage24-3rounds-20260705/aggregate-summary.json
```

三轮聚合：

```text
requested_total=72
submit_ok_total=72
submit_failed_total=0
final_success_total=66
final_error_total=6
submit_latency_ms p50=15532, p95=21075, max=22914
status_query_latency_ms p50=1970, p95=2756, max=3685
```

Panda 资源三轮最大 p95：

```text
cpu_p95_max=13.665%
memory_p95_max=724.105MiB
bandwidth_total_p95_max=9.204Mbps
health_latency_p95_max=19.54ms
```

Panda 资源三轮瞬时最大：

```text
cpu_max=40.74%
memory_max=724.3MiB
bandwidth_total_max=87.453Mbps
health_latency_max=53.05ms
```

错误分类：

1. Round 1 / Round 2 各 1 个文生图任务失败：

```text
curl: (92) HTTP/2 stream 1 was not closed cleanly: INTERNAL_ERROR
```

- 两个失败均无 `conversation_id`，属于 pre-conversation 或上游 HTTP/2 长尾/断流。
- 持续时间约 29~31 分钟，说明当前失败释放太慢。

2. Round 3 有 4 个图生图任务失败：

```text
OpenAIBackendAPI.__init__() got an unexpected keyword argument 'proxy_url'
```

- 这些任务已有 `conversation_id`，发生在 timeout_pending 续轮询路径。
- 根因：`services/image_task_service.py::_run_resume_poll()` 调用 `OpenAIBackendAPI(proxy_url=...)`，但 `OpenAIBackendAPI.__init__()` 只接受 `access_token`，代理应由内部 `proxy_settings` 处理。
- 本地已修复为 `OpenAIBackendAPI()`，并补充测试。

本地修复验证：

```text
python -m py_compile services/image_task_service.py test/test_image_task_service.py
python -m pytest test/test_image_task_service.py test/test_image_tasks_api.py -q
# 14 passed
```

判断：

- 当前 24 并发在“公网上传/入队”层面三轮全部成功：`72/72` HTTP 200 queued。
- 当前瓶颈不是 Panda CPU、内存、健康页或稳定带宽；资源仍有明显余量。
- 真实体验仍不合格：提交 p95 约 21s，status p95 约 2.8s，单轮完成等待最长约 34 分钟。
- 24 档最终成功率当前为 `66/72=91.7%`，低于 95%；但其中 4 个失败是本地已修的续轮询实现 bug，部署后预估可恢复为 `70/72=97.2%`，仍需处理 2 个上游 HTTP/2 INTERNAL_ERROR 长尾失败。
- 不能直接进入 30；先部署续轮询 bugfix，再落地 IMG-005 上传/入队解耦和 pre-conversation 失败快收敛。

## 2026-07-05 追加事实：IMG-005 一期已部署并完成 3 轮 24 压测

状态：**Panda 已部署；两阶段上传/入队成功；生成侧未达标；不进入 30**。

生产部署：

```text
BUG-002 续轮询修复备份：/root/gptimage/backups/bug002-resume-poll-20260705-162726/
IMG-005 一期备份：/root/gptimage/backups/img005-assets-phase1-20260705-163634/
清理压测残留任务备份：/root/gptimage/backups/img005-cleanup-unfinished-20260705-190020/
```

新增接口：

```text
POST   /api/image-assets/references
GET    /api/image-assets/references/{asset_id}/status
DELETE /api/image-assets/references/{asset_id}
POST   /api/image-tasks/edits 支持 asset_ids[]
```

生产最小真实验收：

```text
公网 health：healthy=true, status=ok, version=1.5.0
asset upload：200，asset status=ready
asset_ids edit task：200 queued -> success，duration_ms=44805
部署后严格日志检查：无 Traceback / dictionary changed / image service busy / 5xx / 524 / 502 / unexpected keyword / proxy_url
```

IMG-005 两阶段 3 轮 24 压测报告：

```text
本地：reports/img005-stage24-3rounds-20260705-164948/aggregate-summary-corrected.json
Panda：/root/gptimage/backups/img005-stage24-3rounds-20260705-164948/aggregate-summary-corrected.json
```

压测口径：

- 每轮 24 个任务：8 文生图 + 16 图生图。
- 16 个图生图先走 multipart 上传 reference asset，再用 `asset_ids` 提交任务。
- 每轮参考图原始 PNG 总量约 `30.28MB`，不做输入减重。

Corrected 聚合结果：

```text
requested_total=72
asset_upload_ok_total=48
asset_upload_failed_total=0
submit_ok_total=72
submit_failed_total=0
final_success_total=56
final_error_total=9
final_unfinished_total=7
asset_upload p95 max≈17.98s
submit p95 max≈2.31s
status p95 max≈2.35s
cpu p95 max≈8.28%
memory max≈750.6MiB
bandwidth_total p95 max≈6.30Mbps
bandwidth_total max≈83.40Mbps
health p95 max≈15.45ms
```

判断：

- IMG-005 一期已经证明：大参考图上传和任务入队可以解耦；上传 `48/48` 成功，任务入队 `72/72` 成功。
- submit p95 从上一轮约 `21s` 降到约 `2.3s`，任务提交体从 MB 级降到约 `300~418B`。
- Panda CPU / 内存 / 健康页 / 稳定带宽仍不是瓶颈。
- 24 档生成体验仍不合格：45 分钟 cutoff 下 `56/72 success`、`9 error`、`7 unfinished`。
- 错误主因已经转移到账号/上游生成侧：
  - `HTTP/2 INTERNAL_ERROR`：4 个，无 `conversation_id`，pre-conversation 阶段失败释放过慢。
  - `token invalidated during image poll task check`：4 个，已有 `conversation_id`，说明 120s poll timeout + 续轮询期间撞上账号失效。
  - `no available image quota (tried 8 tokens)`：1 个，说明当前约百级号池、数百级总额度对连续 3 轮 24 压测余量不足。
  - 5 个 error 是压测残留清理，不计作自然失败；清理前 Round 3 有 `2 running + 5 queued` 超过 45 分钟未终态。

当前 Panda 号池快照：

```text
total=102
active=97
limited=5
total_quota=470
panda_ready_count=97
schedulable=96
```

下一步：

1. IMG-005 二期：上传窗口保护和资产 TTL/清理。
2. IMG-006：pre-conversation 阶段硬超时、有限重试、账号/出口短 backoff，避免单任务占用 30 分钟。
3. IMG-007：post-conversation poll 策略重调，`image_poll_timeout_secs=120` 对 24 并发复杂图偏短；需要更长首轮等待或更稳的续轮询账号策略，减少 `token invalidated`。
4. 账号池补量/质量恢复后再跑 24；当前不进 30。

## 2026-07-05 追加事实：IMG-006 / IMG-007 / IMG-005 二期已部署并完成 24 三轮复测

状态：**Panda 已部署；上传/入队目标达成；生成侧未达标，不进入 30**。

部署备份：

```text
/root/gptimage/backups/img006-007-005p2-20260705-194447/
ROLLBACK.sh 已生成
```

已落地：

- IMG-005 二期：reference asset 上传窗口、bytes-inflight 限制、TTL 清理。
- IMG-006：pre-conversation HTTP/2/reset/remote disconnected 快收敛，最多 2 次尝试。
- IMG-007：动态 poll timeout：generation 180s、edit 300s、多参考 360s；timeout_pending 默认 300s、最多 4 次；token invalidated 优先刷新同账号 token 后继续 poll 原 conversation。

生产生效配置：

```text
image_task_queue.timeout_pending_poll_secs=300
image_task_queue.timeout_pending_max_attempts=4
generation_poll_timeout_secs=180
edit_poll_timeout_secs=300
multi_reference_poll_timeout_secs=360
pre_conversation_timeout_secs=240
pre_conversation_max_attempts=2
image_reference_assets.upload_global_concurrency=6
image_reference_assets.upload_per_user_concurrency=3
image_reference_assets.upload_max_bytes_inflight=96MiB
```

本地与生产验证：

```text
目标测试：34 passed
扩展回归：93 passed
py_compile：通过
生产 health：healthy=true
部署后异常日志：0
```

三轮 24 合并报告：

```text
本地：reports/img006-007-005p2-stage24-3rounds-combined-20260705/aggregate-summary.json
Panda：/root/gptimage/backups/img006-007-005p2-stage24-3rounds-combined-20260705/aggregate-summary.json
```

合并结果：

```text
requested_total=72
asset_upload_ok_total=48
asset_upload_failed_total=0
submit_ok_total=72
submit_failed_total=0
final_success_total=45
final_error_total=26
final_unfinished_total=1

asset_upload_p95_ms_max≈19078
submit_p95_ms_max≈2626
status_query_p95_ms_max≈3638
cpu_p95_pct_max≈15.0%
memory_mib_max≈875.4
bandwidth_total_p95_mbps_max≈11.1
strict_bad_count_60m_max=0
```

错误分布：

```text
no available image quota (tried 8 tokens): 23
image poll timeout 300s: 2
image poll timeout 360s: 1
```

关键判断：

- `102 total / 470 quota` 纸面上足够 72；之前“烧穿”表述不精确。
- 这次失败主因不是 Panda CPU、内存、稳定带宽或大图上传；上传和入队均为 100%。
- 当前瓶颈转为实际可调度面：生产 `image_token_max_attempts=8`，每个任务最多抽 8 个候选 token；即使号池总 quota 足够，8 个候选都 preflight/限流/不可调度时仍会报 `no available image quota`。
- IMG-007 生效后 timeout_pending 没有直接爆成 token invalidated 终态，但会拉长占槽时间；若候选面不扩大，连续 24 轮次仍会排队和长尾。

下一步建议：

1. 不进入 30。
2. 先做配置 A/B：`image_token_max_attempts 8 -> 24/32`，只跑单轮 24，观察 `no available image quota` 是否下降以及 preflight 开销。
3. 若 no-available 降下来，再考虑 `per_user_running_max 2 -> 3`；否则加 running 只会更快打到候选面失败。
4. status 查询 p95 仍是 2~4s，需要继续优化公网轻量状态接口链路。

## 2026-07-06 追加事实：BUG-003 与 IMG-008 token 候选面 A/B

状态：**Panda 已部署 BUG-003；IMG-008 第一档 image_token_max_attempts=24 已完成单轮 24 压测；不进入 30**。

BUG-003：

- 根因：services/image_task_service.py::ImageTaskService.__init__() 在构造时执行 _recover_unfinished_locked()，导致 docker compose exec ... import image_task_service 这种只读检查也会把生产 DB 里的
unning 任务当作“服务重启”改成 queued/timeout_pending/error。
- 影响：IMG-008 首轮 img005-stage24-1rounds-20260705-224321 的最后 1 个 running 任务被一次只读检查污染；后续它最终完成，但该轮本地 summary 因会话中断不可作为完整报告。
- 修复：构造/导入只加载 DB，不恢复未完成任务；只有 start_background() / worker 运行前执行一次 runtime recovery。
- 本地验证：py_compile 通过；	est_image_task_service.py + test_image_tasks_api.py 共 19 passed。
- 生产备份：/root/gptimage/backups/bug003-image-task-import-side-effect-20260705-235012/，含 ROLLBACK.sh。

IMG-008 A/B：

- 生产配置：image_token_max_attempts 8 -> 24，备份 /root/gptimage/backups/img008-token-attempts24-20260705-223751/。
- 完整压测报告：
  - 本地：
eports/img005-stage24-1rounds-20260705-235210/aggregate-summary.json
  - Panda：/root/gptimage/backups/img005-stage24-1rounds-20260705-235210/aggregate-summary.json
- 输入：单轮 24，8 文生图 + 16 图生图，10 单参考 + 6 双参考，参考 PNG 总量 30.28MB，未做输入减重。
- 结果：上传 16/16，入队 24/24，最终 23 success / 1 error / 0 unfinished。
- 错误：1 个文生图 ChatGPT 生图超时（已等待 300.0 秒）；本轮
o available image quota 为 0。
- 关键指标：asset upload p95 17.37s，submit p95 2.39s，status p95 3.15s，CPU p95 8.69%，内存 max 710.3MiB，bandwidth total p95 18.09Mbps，strict bad logs 。
- 账号池变化：pre active/quota/schedulable 96/438/96 -> post 88/385/87；扩大候选面有效解决抽样过窄，但会更快暴露坏号/限流号并消耗水位。

判断：

- IMG-008 第一档有效：上一轮三轮 24 中
o available image quota (tried 8 tokens) 23 次，本轮 24 候选面下为 0。
- Panda CPU/内存不是瓶颈，状态查询仍偏慢，长尾来自上游生成/poll 与账号质量。
- 不建议直接进 30；下一步若继续优化，应在补号/水位恢复后小心 A/B per_user_running_max 2 -> 3，并继续保持 image_token_max_attempts=24，暂不升到 32。


## 2026-07-06 追加事实：本地 Panda staging 补池节奏优化

状态：**本地已落地并重启生效；未部署生产 Panda 代码**。

已确认问题链：

- 本地并不是没有可用号：本轮检查时本地约 `staging=9202`、`ready=6523~6698`。
- Panda 远端低水位：只读查询显示 `schedulable=38`、`total_quota=230`，此前本地 staging 状态读取到 `remote_current=9~10`。
- 旧策略瓶颈在本地出货：`upload_max_batch=20` 且 `sync_interval_minutes=30`，理论最多 `40/h`，在 Panda 空池时明显不够。
- 旧循环顺序是先跑 staging 探活再上传 ready；应急大批探活会拖住 ready 上传，和“本地积压、Panda 空、补得慢”的症状一致。

本地已改：

- 探活档位从固定 `1h/3h/6h` 扩展为水位驱动三档：normal `30/120/360min`，low `10/30/90min`，emergency `5/15/45min`。
- 探活吞吐三档：normal `100/轮, concurrency=4`；low `150/轮, concurrency=6`；emergency `200/轮, concurrency=8`。
- 上传节奏三档：normal 仍 `30min`；low `60s`；emergency `30s`。
- 单批上传仍受 `public_import_max_batch_size=20` 约束，避免触发远端 `413` 或单批打满 Panda。
- 低水位且本地 ready backlog 足够时，循环先上传 ready，并跳过本轮大探活：`skipped_probe_reason=ready_backlog_prioritized_for_panda_upload`。
- `/api/accounts/panda-staging/status` 的 `last_probe.token` 已改为脱敏 token，避免状态接口泄露完整 access token。

本地验收：

```text
python -m py_compile services/config.py services/panda_staging_service.py services/account_refresh_all_service.py api/accounts.py
python -m pytest test/test_config.py test/test_panda_staging_service.py test/test_account_refresh_all_service.py test/test_register_service_panda_batch.py test/test_account_maintenance_loop_service.py -q
# 28 passed
```

运行态验收：

```text
本地 health: healthy=true, status=ok
低水位运行态：state=idle, skipped_probe_reason=ready_backlog_prioritized_for_panda_upload
连续上传：20/批，mode=emergency，间隔约 30s
ready 计数：6510 -> 6470，说明上传成功后本地删除生效
Panda 只读查询：schedulable=38, total_quota=230
```

判断：当前最优解不是简单把 `1/3/6h` 全面缩短，而是“ready backlog 优先补 Panda + 水位驱动探活档位”。在 Panda 空池且本地 ready 已积压时，探活不是主瓶颈，上传间隔才是主瓶颈。新策略理论补池上限从 `40/h` 提升到约 `2400/h`，同时保留远端单批 20 的保护。

## 2026-07-06 追加事实：running=3 A/B 失败与补号质量问题

状态：**已回滚 per_user_running_max=3；已部署 maintenance 生图期间降速配置；当前不继续压测**。

执行过程：

- 用户补号后，Panda health 一度显示约 134 active / 734 quota / 134 schedulable，后续同步继续增加到 160+ active。
- 做配置 A/B：image_task_queue.per_user_running_max=3，image_token_max_attempts=24 保持不变。
- 配置备份：/root/gptimage/backups/img008-running3-20260706-014122/。
- 单轮 24 报告：
  - 本地：
eports/img005-stage24-1rounds-20260706-014253/aggregate-summary.json
  - Panda：/root/gptimage/backups/img005-stage24-1rounds-20260706-014253/aggregate-summary.json

结果：

`	ext
asset_upload=16/16
submit=24/24
final=0 success / 24 error
error=24 x no available image quota / no available image quota (tried 24 tokens)
`

关键发现：

- 这轮不是 CPU/内存瓶颈，错误在取号/preflight 阶段快速失败。
- 同期 Panda maintenance 正在 normal 模式扫号，且连续批次显示补入账号真实可用率极低：
  - batch：80 processed / 0 available / 80 failed / 74 removed
  - batch：80 processed / 1 available / 79 failed / 53 removed
  - slow batch 后仍有 5 processed / 0 available / 5 failed / 4 removed
- 因此 health 中的 ctive/quota/schedulable 在补号刚导入时会虚高；真实取号还会受 preflight 失败/backoff 影响。
- 单任务 smoke 也失败：
o available image quota (tried 24 tokens)。

已执行回滚/修正：

- 已执行 /root/gptimage/backups/img008-running3-20260706-014122/ROLLBACK.sh，恢复默认 per_user_running_max=2，保留 image_token_max_attempts=24。
- 新增 maintenance 生图期间降速配置，备份 /root/gptimage/backups/maint-slow-on-image-20260706-015303/：
  - slow_when_image_inflight=1
  - pause_when_image_inflight=0
  - slow_batch_limit=5
  - slow_delay_between_accounts_sec=8
  - slow_cooldown_sec=30
  - startup_delay_sec=30

判断：

- 当前不能继续 24/30 压测；需要先让 maintenance 清完假活号，或补入经过本地 1h/3h/6h 探测的高置信账号。
- 后续应在 health/监控增加 preflight_backoff_count、真实可调度候选数、最近 preflight 失败数，否则 schedulable 会误导压测决策。


## 2026-07-06 Panda 小档压力检查：生图失败主因是账号实际可调度面，不是资源

### 目标

检查不同档位下 Panda 生图、maintenance、同步的压力。因 1 档已失败，按停止线未继续 3/6 档。

### 执行口径

- 新建临时安全 monitor：`.codex_tmp/panda_safe_monitor.py`，不调用无参数 `/api/image-tasks/status`，只按指定 task ids 查询，避免全量历史任务响应造成压力。
- 新建临时小档 loadgen：`.codex_tmp/panda_small_pressure.py`，支持 stage `1/3/6`。
- 实际只跑 stage=1 两次，均为 1 个文生图任务。

### 观察到的事实

1. 不应再使用无参数 `/api/image-tasks/status` 做压测基线。
   - 该接口会返回历史全量任务，响应极大。
   - 本轮之后安全 monitor 改用 `ids=` 轻量查询。

2. Panda 资源不是当前瓶颈。
   - stage=1 第一次：CPU p95≈26.2%，内存≈665MiB，带宽 total p95≈3.95Mbps，health p95≈13.5ms。
   - stage=1 第二次：CPU p95≈31.3%，内存≈639MiB，带宽 total p95≈7.03Mbps，health p95≈27.3ms。
   - 两次均远低于 CPU/内存/带宽停止线。

3. stage=1 生图两次均失败。
   - 第一次：`no available image quota (tried 20 tokens)`。
   - 第二次：`no available image quota (tried 24 tokens)`。
   - 第二次任务曾进入 `running`，约 21.6s 后失败，说明任务队列/worker 能接住，请求死在取号/preflight。

4. 账号池账面水位与实际可调度面严重不一致。
   - 压测前后 Panda 账面曾显示 `schedulable=156~189`、`total_quota=870~965`。
   - 同期 maintenance 一轮观察到 `160 processed / 159 failed / 150 removed`，说明大量刚同步进 Panda 的号很快被验证为坏号。
   - 抽样 Panda 账号显示 `status=正常, quota=5, panda_sync_state=ready`，但 `last_refresh/last_refresh_at/updated_at` 为空；部分远端样本也缺少可证明 Panda 本地刷新过的 `last_quota_refresh_at`。

5. 同步压力需要区分“异常风暴”和“正常节奏”。
   - 本地补池清理后，Panda 最近 2 分钟 import-batch 为 4 次且全 200，约 30 秒一批，符合 emergency 节奏。
   - 此前日志里大量 429 主要来自导入间隔保护触发；不能简单扩大本地批量/频率。

6. maintenance 与生图存在策略冲突。
   - 当前 production settings 已是 `slow_when_image_inflight=1, slow_batch_limit=5, slow_delay_between_accounts_sec=8.0`，但在 image_inflight=0 时仍会正常批量验证。
   - 当补池持续灌入大量低质量账号时，maintenance 会快速删除，账面水位短时间虚高，生图 preflight 仍可能 24 连败。

### 判断

- 当前不能进入 3/6/18/24 档；1 档已经证明瓶颈不在 CPU/内存/带宽/队列，而在账号实际可调度面。
- 继续压测只会消耗候选尝试、加重 preflight 和 maintenance，不会得到有效容量结论。
- 下一步应先修账号质量/同步口径：Panda `schedulable` 必须更接近“已在 Panda 或可信本地 recently verified 且未 backoff 的账号”，否则账面 quota 没意义。

### 产物

- `reports/panda-pressure-20260706-014622-stage-1/`
- `reports/panda-pressure-20260706-015407-stage-1/`
- `.codex_tmp/panda_safe_monitor.py`
- `.codex_tmp/panda_small_pressure.py`

## 2026-07-06 追加事实：Panda 账号账面可调度口径被失败刷新污染

状态：**只读检查完成；尚未部署修复**。

关键证据：

- 生产配置仍为 `image_require_recent_quota_refresh=false`，生图候选初筛主要依赖 `status=正常 && quota>0`。
- `import-batch` 当前直接 `import_account_items -> _add_account_payloads`，导入账号会进入主账号池；没有 Panda 接收隔离区，也没有 Panda 侧 promote 前验证。
- `account_refresh_all_service._record_failure()` 对 transient failure 会写入 `last_quota_refresh_at=now`，这会让失败账号看起来像“刚刷新过”。
- 若账号近期存在 transient 记录，后续 `token invalidated` 也可能被 `_has_recent_transient_token_error()` 合并进 transient 分支，导致账号继续保持 `status=正常, quota>0`。
- Panda 只读抽样显示大量账号同时具备：
  - `status=正常`
  - `quota=5`
  - `panda_sync_state=ready`
  - `last_refresh_error=token invalidated (/backend-api/me)`
  - `last_quota_refresh_error=CONNECT tunnel failed / curl 56`
  - `quota_refresh_failure_kind=transient`
- 生产 DB 只读量化快照：`505 ready` 中 `503 active_quota_recent`，但 `380 active_quota_refresh_invalidated`，`495 active_quota_qfail_gt0`，`458 quota_error_connect_503_or_56`。
- 同期 maintenance 仍在持续失败：近期累计可见 `120 processed / 120 failed / 91 removed`。

判断：

- 当前最大问题不是 CPU、内存、带宽，也不只是本地补池慢；而是 Panda 把“导入 / transient 失败 / 本地刷新时间”混成了“Panda 侧真实可调度”。
- 后续修复必须把链路拆成：本地 verified ready -> Panda quarantine/incoming -> Panda verifier promote -> schedulable。
- 修复前不应继续扩大生图压测档位，否则只会继续打 preflight、maintenance 和坏号池。

## 2026-07-06 追加事实：注册后快死问题的分层归因

状态：**只读检查完成；尚未部署修复**。

本地注册与入池：

- 注册 worker 代码会先 `add_account_items()`，随后 `refresh_accounts([access_token], defer_invalid_removal=False)`。
- 只有 post-register refresh `ok` 才会进入本地 Panda staging；`invalid` 与 `transient` 会立即 `delete_accounts()`，不会进入本地账号池。
- `register_post_verify_diagnostics.jsonl` 历史统计：`96618` 条里 `ok=91162`、`transient=5451`、`invalid=5`。
- 但最近窗口变差：最近 100 条只有 `ok=36`、`transient=64`；最近 500 条 `ok=335`、`transient=165`。
- 本地最近 staging 样本多为 post-register ok 账号，`invalid_count=0`、`quota_refresh_fail_count=0`。

本地账号库：

- 本地 SQLite 账号表仍是 `access_token + data JSON` 兼容形态。
- 只读检查 `13476` 行：`mismatch_col_data=0`，`dup_col_count=0`，`dup_json_count=0`，`bad_jwt=0`，`exp_past=0`。
- 因此没有证据表明 SQLite 把 token 写坏、截断或重复覆盖。

本地代理链：

- 当前注册配置代理为 `http://127.0.0.1:40080`。
- 40080 是 WSL Docker `chatgpt2api-privoxy -> warp-proxy`。
- 小样本 live-check 全部被代理层挡住：`HTTP/1.1 503 Too many open connections` / `curl: (56) CONNECT tunnel failed`。
- `scripts/privoxy-warp.conf` 当前 `keep-alive-timeout=300`、`socket-timeout=300`，且未显式提高/收敛 `max-client-connections`。
- 这会导致 20 线程注册、staging 探活、同步补池同时运行时，40080 连接池堆积，产生大量 transient，无法判断账号真实死活。

账号年龄与死亡迹象：

- 本地 0~60 分钟新号基本仍是 `invalid_count=0`。
- 60 分钟后开始出现 `token invalidated`；6~12 小时段大量账号已出现 invalid 记录，但仍被账面算作 `active_quota`。
- Panda 侧更明显：2~6 小时账号中大量已有 `token invalidated`，6~12 小时段多数已有 invalid 记录，但仍保持 `status=正常, quota>0`。

归因：

- 注册链路不是当前首要污染源；它已经删除 post-register invalid/transient。
- SQLite 不是当前首要根因；未发现 token 损坏。
- 生图调度不是“制造死号”的主因；它通过 preflight/取号更快暴露坏号。
- 刷新/maintenance 的失败记录逻辑存在明确问题：transient 会刷新 `last_quota_refresh_at`，且近期 transient 可能吞掉后续 `token invalidated`，导致坏号继续保持可调度。
- 40080 代理连接池耗尽会放大 transient，使本地和 Panda 都无法稳定确认账号质量。

### 2026-07-06 本地注册机启动/停止状态修复
- 注册机停止不再被旧 runner 反向重新置为 enabled=true；如果上一轮仍在收尾，启动请求会明确提示“上一轮注册任务仍在收尾”。
- 注册 runner 等待 worker 使用 1s tick 更新状态，停止超过 90s 会释放启动锁，避免 stuck runner 让前端长期无法启动。
- 账号刷新探活调用 OpenAIBackendAPI 后会关闭底层 session，避免本地 uvicorn 对 40080 积累 CLOSE_WAIT。

## 2026-07-06 追加事实：IMG-009 health 真实候选指标已部署并完成 24 异步压测

状态：**Panda 已部署；health JSON/HTML 已验收；24 异步压测完成但严格 24/24 未完全通过**。

已部署：

- `services/account_service.py`：`get_stats()` 新增运行态生图候选指标：
  - `preflight_backoff_count`
  - `ready_candidate_count`
  - `available_candidate_count`
  - `dispatchable_candidate_count`
  - `image_inflight_count`
  - `image_account_concurrency_limit`
  - `image_global_concurrency_limit`
  - `image_global_queue_timeout_secs`
  - `image_global_limit_reached`
- `api/system.py`：health HTML 卡片新增“预检退避 / 退避后候选 / 当前可派发 / 生图占用/全局上限”。

本地验证：

```text
python -m py_compile services/account_service.py api/system.py
python -m pytest test/test_account_image_capabilities.py -q
# 23 passed
python -m pytest test/test_image_task_service.py test/test_image_tasks_api.py -q
# 19 passed
```

生产备份：

```text
/root/gptimage/backups/img009-health-runtime-candidates-20260706-093545/
/root/gptimage/backups/img009-health-runtime-candidates-20260706-093545/ROLLBACK.sh
```

生产验收：

```text
syntax_compile_ok
/health?format=json 新字段 missing=[]
HTML cards ok
最终 health：schedulable=142, preflight_backoff_count=0, ready_candidate_count=142, available_candidate_count=142, dispatchable_candidate_count=142, image_inflight_count=0
同步兼容入口仍为 image_global_concurrency_limit=6, image_global_queue_timeout_secs=0.0
```

24 异步两阶段真实混合输入压测：

```text
本地报告：reports/img005-stage24-1rounds-20260706-094205/aggregate-summary.json
Panda 报告：/root/gptimage/backups/img005-stage24-1rounds-20260706-094205/aggregate-summary.json
输入：8 generation + 16 edit；10 单参考 + 6 双参考；参考图总量 30.28MB；未输入减重。
asset_upload=16/16
submit=23/24
accepted final=23 success / 0 error / 0 timeout_pending / 0 unfinished
strict_bad_count_60m=0
CPU p95=9.468%, max=14.42%
内存 max=628.1MiB
总带宽 p95=10.643Mbps, max=17.575Mbps
status query p95=3499ms
```

严格判断：

- 账号候选面已不再虚高：压测后 `dispatchable_candidate_count=142`，`preflight_backoff_count=0`。
- Panda 资源不是本轮瓶颈；入队成功的 23 个任务全部成功。
- 本轮严格 24/24 未通过：1 个 generation 在公网提交阶段 TLS handshake 120s 超时，未进入 Panda 任务库。
- 同步 OpenAI/NewAPI 兼容入口仍是 `6 + 0s queue_timeout`，超过 6 会立即触发 `image service busy`；这与异步 `/api/image-tasks` 24 入队是两条链路，不应直接把同步入口拉到 24。
- 下一步若要减少截图里的同步入口 502，应单独 A/B：先把 `image_global_queue_timeout_secs` 调到 `8~10s`，再小步测试 `image_global_concurrency 6 -> 8`；不建议公共同步入口做无限队列。

## 2026-07-06 IMG-011：pre-conversation 硬超时与 3 轮 24 压测

状态：**Panda 已部署；3 轮 24 公网混合输入压测完成；70/72 成功，0 未完成**。

已执行：

- 生产备份：/root/gptimage/backups/img011-image-task-hard-timeout-20260706-124825/，含 ROLLBACK.sh。
- services/image_task_service.py：为异步生图 worker 增加任务级 hard timeout，避免上游 SSE/pre-conversation 阻塞导致
unning + conversation_id=false 无限占槽。
- scripts/img005_asset_stage_loadgen.py：
  - asset upload 增加窗口与可重试：IMG005_ASSET_UPLOAD_WINDOW=8，IMG005_ASSET_UPLOAD_MAX_ATTEMPTS=2。
  - submit/status 查询统一使用 HTTP/2 keepalive，降低本地压测客户端 TLS 长尾噪音。
- 本地回归：	est_image_task_service.py + test_image_tasks_api.py + account_maintenance + v1 images generations/edits = 28 passed。
- 生产验收：容器内 hard-timeout smoke 通过；部署后 health 正常。

压测报告：

`	ext
本地：reports/img005-stage24-3rounds-20260706-125316/aggregate-summary.json
Panda：/root/gptimage/backups/img005-stage24-3rounds-20260706-125316/aggregate-summary.json
`

结果：

`	ext
requested_total=72
asset_upload_ok_total=48
asset_upload_failed_total=0
submit_ok_total=72
submit_failed_total=0
final_success_total=70
final_error_total=2
final_unfinished_total=0
strict_bad_count_60m_max=0
asset_upload_p95_ms_max≈67.4s
submit_p95_ms_max≈5.33s
status_query_p95_ms_max≈2.33s
cpu_p95_pct_max≈7.78%
memory_mib_max≈812.8
bandwidth_total_p95_mbps_max≈10.51
`

错误分布：

- Round 1：2 个 generation 任务触发 hard timeout：image task hard timeout before upstream completion (510.0s); no conversation_id captured。
- Round 2：24/24 success。
- Round 3：24/24 success。

判断：

- 24 档公网大参考图上传与入队已经稳定：3 轮均 100% 上传、100% submit。
- HTTP/2 keepalive 后 status 查询从之前 2~8s 常态明显下降，p95 最差约 2.33s。
- Panda CPU/内存/带宽不是瓶颈；per_user_running_max=2 仍是单用户 24 任务总耗时的主要排队来源。
- hard timeout 能避免无限卡槽，但本质上仍是降级止血；根治应把上游调用放进可 kill 的进程/子进程，或降低 pre-conversation 阈值并做上游连接层 hard cancel。
- 压测结束后验收：healthy=true、ctive={}、image_inflight_count=0、dispatchable_candidate_count=186、
erified_total_quota=794。

## 2026-07-06 IMG-011 详细压测数据与 4/6/8/10 后台运行并发预测

### 有效报告

```text
本地：reports/img005-stage24-3rounds-20260706-125316/aggregate-summary.json
Panda：/root/gptimage/backups/img005-stage24-3rounds-20260706-125316/aggregate-summary.json
```

### 3 轮 24 总结果

| 指标 | 数值 |
| --- | ---: |
| requested_total | 72 |
| asset_upload_ok_total | 48 |
| asset_upload_failed_total | 0 |
| submit_ok_total | 72 |
| submit_failed_total | 0 |
| final_success_total | 70 |
| final_error_total | 2 |
| final_unfinished_total | 0 |
| strict_bad_count_60m_max | 0 |
| asset_upload_p95_ms_max | 67,396ms |
| submit_p95_ms_max | 5,332ms |
| status_query_p95_ms_max | 2,328ms |
| cpu_p95_pct_max | 7.78% |
| memory_mib_max | 812.8MiB |
| bandwidth_total_p95_mbps_max | 10.506Mbps |

### 分轮结果

| 轮次 | 最终结果 | asset upload | submit | task duration | CPU p95/max | 内存 max | 总带宽 p95/max | status p95 | 备注 |
| --- | --- | --- | --- | --- | --- | ---: | --- | ---: | --- |
| R1 | 22 success / 2 error | 16/16，p95 67.4s | 24/24，p95 2.95s | p50 61.6s，p95 447.4s，max 510.0s | 7.64% / 47.44% | 709.7MiB | 8.83 / 10.84Mbps | 2.33s | 2 个 generation pre-conversation 无 conversation_id，hard timeout 打掉 |
| R2 | 24 success | 16/16，p95 64.3s | 24/24，p95 3.25s | p50 57.2s，p95 85.1s，max 94.9s | 7.49% / 16.33% | 777.8MiB | 9.73 / 12.08Mbps | 1.56s | clean 轮 |
| R3 | 24 success | 16/16，p95 52.3s | 24/24，p95 5.33s | p50 53.8s，p95 89.6s，max 92.8s | 7.78% / 19.79% | 812.8MiB | 10.51 / 11.92Mbps | 1.44s | clean 轮 |

### 预测口径

- 当前有效 clean 轮 R2/R3：运行态单任务 avg≈59~61s、p50≈54~57s、p95≈85~90s。
- 当前后台实际运行并发约 2；24 个任务总轮耗时约 12.4 分钟。
- CPU 当前有大量余量；带宽按用户确认的 30Mbps 作为主要约束。
- 带宽粗略按后台运行并发近似线性放大：当前 2 并发总带宽 p95≈10.5Mbps。
- pre-conversation 卡死概率按本轮观测约 2/72≈2.8% 估计；并发越高，单个卡槽对整体吞吐影响越小，但底层上游/账号 in-flight 残留风险更高。

### 后台运行并发 4/6/8/10 预测

| 后台运行并发 | 24 入队预估完成时间 | CPU p95 预估 | 内存预估 | 总带宽 p95 预估 | 成功率/风险判断 | 结论 |
| ---: | --- | --- | --- | --- | --- | --- |
| 4 | 6~8 分钟 | 12~16% | 0.9~1.1GiB | 18~22Mbps | 成功率大概率接近当前；pre-conv 卡槽影响减半；带宽安全 | 推荐第一档 A/B |
| 6 | 4~6 分钟 | 18~25% | 1.1~1.3GiB | 27~33Mbps | 接近 30Mbps 带宽上限；需要观察 b64/图片回传尾流；CPU安全 | 最可能的甜点档 |
| 8 | 3~5 分钟 | 25~35% | 1.3~1.5GiB | 36~44Mbps | CPU仍安全，但带宽超过预算，长尾/断流/524 风险明显上升 | 需回传窗口/直连优化后再试 |
| 10 | 2.5~4.5 分钟 | 32~45% | 1.5GiB 附近或更高 | 45~55Mbps | 当前 30Mbps 单机不建议；可能把问题从排队变成回传拥塞和超时 | 不建议直接上 |

### 结论

- `4` 是安全试验档，基本只减少排队时间，不太可能打穿 Panda。
- `6` 是当前单机 30Mbps 条件下最值得落地的目标档；CPU/内存够，但带宽已经接近上限。
- `8/10` 不应直接作为默认档；除非先做图片/b64 回传窗口、NewAPI 到 Panda 直连或多出口/多桶。
- 如果 NewAPI 同步入口改成内部异步队列，不应沿用当前 per-user running=2；建议先给 OpenAI 兼容入口单独设置 running=4 做 A/B，通过后再到 6。

### 2026-07-06 补充解释：带宽 p95/max 与 24 入队 2 running 的总耗时口径

- 监控脚本每 5 秒采样一次 Panda 网卡 rx/tx，`bandwidth_total_mbps` 是 5 秒窗口平均速率，不是毫秒级瞬时尖峰。
- R3 的 `10.51 / 11.92Mbps` 表示：总带宽 p95≈10.51Mbps、单个 5 秒采样窗口 max≈11.92Mbps；不是整个 12 分钟持续 10~12Mbps。
- clean 轮带宽持续性：
  - R2 监控约 720s，`>=10Mbps` 仅 15s，最长连续 5s；`>=5Mbps` 约 150s，最长连续 80s。
  - R3 监控约 705s，`>=10Mbps` 约 65s，最长连续 30s；`>=5Mbps` 约 140s，最长连续 50s。
  - 三轮均无 `>=15Mbps` 持续采样，更没有接近 24/30Mbps。
- `task duration p50` 是单个任务拿到后台运行槽后的运行时长，不包含前面排队等待，也不包含参考图上传阶段。
- 24 入队、2 running 的直观模型：后台只有 2 条流水线，每条约 1 分钟出 1 张；24 张约等于 12 个波次，因此 clean 轮总完成时间约 12 分钟。
- 实测：R2 从入队后到全完成约 745s≈12.4min，R3 约 743s≈12.4min。若再计入参考图 asset upload，端到端每轮约 14 分钟。

## 2026-07-06 IMG-012 NewAPI 同步入口内部异步化

状态：**已本地实现 + Panda 已部署 + 16:41 restart**；busy_6 验收通过；24 路全成功未通过。

方案文档：`docs/08-image-pipeline-newapi-async-plan.md` §13

### 已实现

```text
api/ai.py：非 stream /v1/images/* → submit image_task + wait_for_result
services/image_sync_adapter.py：run_generation_sync / run_edit_sync
services/image_task_service.py：wait_for_result、payload queue_coordinated=True
services/account_service.py：skip_global_limit（队列路径绕过全局 6）
services/protocol/conversation.py：request.queue_coordinated → skip_global_limit
Panda config：per_user_running_max=6, burst_enabled=false, image_return_window_size=3
部署脚本：scripts/img012_deploy.py / patch_config / verify / newapi_sync_loadgen / enable_burst
```

### 压测（2026-07-06 16:42，NewAPI 单轮 24）

```text
报告：reports/img012-newapi-sync-stage24-1rounds-20260706-164210/
busy_6：0（✅ 核心目标达成）
成功：5/24（文生图 60–111s）
失败：19/24（JSONDecodeError 8 + HTTP/2 ConnectionTerminated 11，NewAPI 传输层）
```

### 未做

```text
burst 8 动态升档（burst_enabled 仍 false）
3 轮 72 路验收
stream=true 同步入口改造
NewAPI 网关 24 长连接 / HTTP/2 稳定性
下载/回传独立窗口（IMG-012E 完整形态）
```

### 运维注意

```text
改 Python 代码后必须 docker compose restart，up -d 不会 reload 进程。
16:18 部署代码但 12:51 启动的容器在 restart 前仍报 busy_6，即此原因。
image_global_concurrency=6 配置仍在，仅 queue_coordinated 路径跳过。
NewAPI 可见 110–300s = 排队（6 槽位）+ 上游执行，非单次生图纯时间。
```

下一步：修 NewAPI 传输层 → 重跑 24 路 → burst 8 → 3 轮 72 路。

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

补充验收：2026-07-06 19:59 +08 复查 Panda：`image_inflight_count=0`，`image_tasks.db unfinished=0`，`dispatchable_candidate_count=161`。

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
