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


## 本地 Panda staging 水位驱动补池规则（2026-07-06）

涉及本地注册号进入 Panda 前的 staging/ready/upload 链路时：

- 不要只看 `staging` 数量；必须同时看 `ready`、Panda `schedulable/active/total` 和 `last_upload`。
- Panda 低水位但本地 `ready` 已大量积压时，优先排查上传间隔/批量/远端导入限频，不要先扩大探活。
- 当前本地策略：normal `30/120/360min`，low `10/30/90min`，emergency `5/15/45min`，仍需三次 OK 才 ready。
- 当前远端导入保护单批仍为 `20`；如果要把 `low_upload_max_batch` / `emergency_upload_max_batch` 提到 20 以上，必须同步确认生产 Panda 的 `public_import_max_batch_size`，否则会触发 `413`。
- 低水位且 ready backlog 足够时，staging 循环应先上传 ready，并跳过本轮大探活，避免探活 timeout 拖住补池。
- `/api/accounts/panda-staging/status` 不允许返回完整 token；只能返回 `anonymize_token()` 后的值。
