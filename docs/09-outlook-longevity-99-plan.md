# Outlook 账号长期存活、真实队列编排与逆向链路 99+ 方案

最后更新：2026-07-16

## 1. 目标、边界与评分

本方案把“账号数量”改成“完成完整观察周期、具备节点与会话证据的有效账号”，覆盖 Windows 本地注册、Webshare 固定节点、Panda 接收与成熟期、真实文本/生图队列、ChatGPT Web 聊天/生图逆向链路、性能、可观测性和回滚。

chatgpt2api 反代文本模型维持聊天能力；`/v1/chat/completions`、`/v1/responses`、`/v1/messages` 的验收范围为聊天、流式、续聊、错误映射和性能，不扩展通用函数能力，也不把 GPT 文本模型逐个优化列入本方案。

| 模块 | 分值 | 验收核心 |
| --- | ---: | --- |
| Webshare 节点生命周期 | 12 | 分级、绑定、漂移检测、冷却与隔离 |
| 账号与会话连续性 | 12 | 固定节点、持久环境、身份字段一致 |
| cohort 与成熟期 | 12 | 单 canary、1/6/24/72h、7d、自动熔断 |
| 文本与生图运行编排 | 12 | 独立速率域、图片容量保护、真实任务驱动 |
| 聊天/生图逆向链路 | 14 | 请求身份一致、会话连续、SSE/poll、去重、失败归因与性能 |
| 性能与容量 | 10 | Windows/Panda 分层预算、无常驻浏览器扩散 |
| 可观测性与账号页 | 10 | 代理/出口、累计流量、生命周期和错误原因 |
| 测试矩阵 | 10 | 功能、故障注入、兼容性、性能、生产 canary |
| 备份、回滚与生产门禁 | 8 | SQLite、代码、前端、配置和节点租约回滚 |

设计覆盖目标为 `99+/100`。`cohort 7 天存活率 >99%` 是运行 SLO；工程侧负责固定变量、止损、证据与统计可信度，不把小样本短期存活写成统计结论。

## 2. 当前事实与上一轮未完成项

### 2.1 已落地

- 账号页已增加“代理 / 出口”和“累计流量”；代理凭据隐藏，历史未采集流量显示“待统计”。
- 文本与生图已记录应用层上传、下载和总字节数。
- 图片 SSE ready 以真实 `conversation_id` 为准，ping/control 不解除 deadline；已有 conversation 的超时进入原会话续轮询。
- 注册、OTP、首次登录在 Windows 本机执行，账号材料再上传 Panda。
- 文本与生图的额度数值彼此独立，共享账号健康、节点绑定和总 in-flight 门禁。

### 2.2 仍待落地

1. 节点租约表、绑定写保护、出口漂移阻断和释放冷却。
2. `1h/6h/24h/72h/7d` 成熟期任务状态机与补跑机制。
3. `AccountWorkloadPolicyService`、文本 lease、图片容量保护和真实队列 shadow/live canary。
4. Windows 持久 Profile 与 Panda 轻量会话元数据的完整交接。
5. ChatGPT Web 聊天/生图 canonical request builder、请求形状遥测、单一重试协调器和会话恢复去重。
6. survivor 与 terminal 的请求/节点/生命周期差异报告。

## 3. 2026-07-16 Panda 最新只读证据

权威快照改为 **22:09 SSH 深检**（取代 20:34 中已过时的磁盘结论）。报告：`data/runlogs/panda-deep-audit-readonly-20260716-220921.md`；20:34 旧报告仍保留作对照：`panda-account-evidence-readonly-20260716-203416.md`。

| 项目 | 20:34（旧） | 22:09（权威） |
| --- | ---: | ---: |
| 账号总数 | 18 | 18 |
| 正常 / 限流 / 异常 / 禁用 | 4 / 2 / 7 / 5 | 5 / 1 / 7 / 5 |
| 可调度 / ready / dispatchable | 0 / 0 / 0 | 0 / 0 / 0 |
| verified_total_quota | 0 | 0 |
| Panda rejected | 12 | 12 |
| `invalid_count > 0` | 12 | 12 |
| app RSS | 447.5MiB / 1.5GiB | 444.8MiB / 1.5GiB |
| 根分区 | 86.1% | **49%**（门禁已解除） |
| 完整 fp / egress hash | 0 / 0 | 0 / 0 |
| 同代理 host 签名 | 12 条相同 | 12 条相同（`b51d9569ebf5`） |

补充事实（22:09）：

- 账面 quota 合计 271 全在异常/禁用号；5 个「正常」均为 `q=0`。
- 无 `proxy_nodes` / cohort / lease 表；`node_lease` / `cohort_id` / `maturity_*` 字段覆盖 0/18。
- 任务库 success15 / error401 / timeout_pending21 / running1；`image_tasks.db` ≈ 1.6G。
- 近 24h 日志：`429×2024`、`quota×664`、`timeout×661`、`account_deactivated×16`。
- 生产缺 `account_fingerprint.py` / `account_workload_policy.py`；`openai_backend_api.py` 仍为 07-10 哈希，与本地 ACC-010 漂移。

20:34 仍有效的历史样本（未在 22:09 重采）：

- 最近 7 天日志样本的生图成功占比从 07-14 的 `22.0%` 降至 07-16 的 `2.8%`。
- 8961 条有文本样本仅 828 种精确文本，最大同一规范化文本重复 3501 次；9119 条调用均为 `gpt-image-2`。
- `request_shape` 样本极少；请求头未进结构化日志。

当前证据支持的根因优先级：

1. 共同出口、出口迁移和账号强绑定缺失。
2. 高失败重试、密集轮询、超时与零额度压力。
3. token 失效及批次关联处置。
4. 高度重复提示与单一工作负载。
5. 固定 header/payload 形状属于待增加脱敏遥测后验证的因素。
6. 生产代码/模块落后本地，导致 fp 持久化路径无法在 Panda 生效。

## 4. 账号页与证据字段

### 4.1 已实现字段

- 代理 / 出口：优先显示 `proxy_egress_ip`，其次显示脱敏后的 host:port。
- 节点信息：`proxy_provider`、`proxy_scope`、出口 hash 前 12 位。
- 累计流量：`traffic_uploaded_bytes`、`traffic_downloaded_bytes`、`traffic_total_bytes`、`traffic_updated_at`。
- 当前流量是应用层载荷，不等同于 Webshare 账单；TLS、隧道和重传开销由代理商统计。

### 4.2 必补字段

- 节点状态：READY / OBSERVE / QUARANTINED / RESERVED / BOUND / COOLING。
- 成熟度：T+1h、T+6h、T+24h、T+72h、T+7d。
- 注册出口 hash、当前出口 hash、IPv4/IPv6、地区、最后复测时间和漂移次数。
- runtime profile 版本/hash、客户端版本、locale、timezone；正文和 secret 不入日志。
- 文本/生图请求数、分项流量、重复提交数、poll 次数和错误阶段。
- cohort、批次、绑定时间、首个真实任务时间、terminal 事件时间。

## 5. Webshare 节点状态机

```text
DISCOVERED → QUALIFYING → READY → RESERVED → BOUND
                      ↘ OBSERVE       ↓
                        QUARANTINED ← COOLING
```

- `READY`：连续出口检查稳定，地区与协议可用性满足门禁。
- `OBSERVE`：出口稳定但 ChatGPT 入口存在间歇失败。
- `QUARANTINED`：入口不可用、出口漂移或证据不完整。
- `BOUND`：一个节点租约只服务一个账号；普通账号更新接口不得修改绑定。
- `COOLING`：账号终态或人工解绑后保留观察窗口，再决定回 READY 或隔离。

2026-07-16 已有释放节点基线：10/10 三轮在线且出口 hash 稳定，地区均为 SG/SIN，READY=5、OBSERVE=3、QUARANTINED=2。该结果只证明当时节点本身在线；Panda 最新账号行仍缺少注册/运行出口证明，节点在线不等于账号已完成强绑定。

下一批必须同时保存：`node_lease_id`、注册出口 hash、Panda 首次接收出口 hash、每次业务出口 hash。任一不一致立即 `ACCOUNT_PAUSED + NODE_QUARANTINED`。

## 6. Windows 注册机与 Panda 性能边界

### 6.1 成本发生位置

| 阶段 | 运行位置 | 主要成本 |
| --- | --- | --- |
| Chromium 注册、Outlook OTP、首次登录 | Windows | CPU、内存、浏览器 Profile、网络 |
| Profile 持久化与加密冷归档 | Windows | 磁盘、压缩、加密 |
| 少量账号上传 | Windows → Panda | 数 KiB 元数据、一次 SQLite upsert、节点租约写入 |
| Panda 接收后验证、成熟期、恢复、节点复测 | Panda | HTTP、SQLite、少量 worker |
| 生产聊天/生图 | Panda | 连接、SSE/poll、下载、编码、队列与账号槽位 |

因此 Panda 低内存门禁不直接停止 Windows 浏览器注册进程。Panda 上传本身开销很小；真正需要门禁的是接收后的批量验证、恢复、浏览器、节点扫描、压测及生产请求。

### 6.2 Profile 分层

本机 Chromium Profile 样本约 68～124MiB；18 个完整 Profile 约 1.2～2.2GiB，100 个约 6.8～12.4GiB。

- Windows：按账号保存加密冷 Profile，带 ACL、TTL、版本/hash和恢复校验。
- Panda：默认只保存 Profile 版本/hash、稳定 runtime profile、会话元数据和节点租约。
- Panda 浏览器常驻 0、峰值 1，仅在明确恢复任务中启动。

### 6.3 硬门禁

- Panda 可用内存低于 1GiB或低于总内存 25%：允许单账号写入 `incoming/paused`；暂缓批量验证、恢复、浏览器、节点扫描和压测；到期成熟期检查只运行串行轻量探测。
- Windows 仅在 Panda 连入库及观察证据失去保存能力、incoming 积压越线或用户主动暂停时，停止消费下一个 Outlook；原因是交接闭环中断，而非上传性能不足。
- 归一化一分钟负载高于 0.70：暂停新增 Panda 后台任务。
- 根磁盘高于 85%：停止部署、备份、Profile/大日志写入及 live canary，先执行受控空间治理。
- 自动维护、恢复、节点复测和生图压测不得同时扩并发。

2026-07-16 22:09 根分区已回落至 49%；磁盘硬门禁解除。仍禁止无备份 live canary；部署前必须备份并做单 canary 验收。

## 7. 文本与生图的预算关系

文本和生图不共享额度数值，但共享账号健康、节点绑定、会话连续性和总 in-flight 门禁：

| 维度 | 文本 | 生图 | 关系 |
| --- | --- | --- | --- |
| 上游额度 | 按限频/可用性管理 | image quota / restore_at | 独立 |
| 运行保护 | text rate window | image rate/quota window | 独立 |
| 账号状态 | healthy/limited/invalid/terminal | 同一状态源 | 共享 |
| 节点与环境 | 固定账号节点/runtime profile | 固定账号节点/runtime profile | 共享 |
| 单账号并发 | 默认 1 | 默认 1 | 总 in-flight 共享 |
| 401/403/429 | 同一分类体系 | 同一分类体系 | 共享状态机 |

`AccountWorkloadPolicyService` 维护独立的 `text_rate_window`、`image_rate_window`、`text_cooldown_until`、`image_restore_at`，并统一维护 `account_total_inflight`、节点状态、成熟度和连续鉴权失败次数。

## 8. 真实队列与图片容量保护

### 8.1 原则

- 只处理真实用户或真实业务队列产生的任务。
- 不使用“每 N 张图固定插一条文本”的机械周期。
- 随机项只用于同优先级真实任务排序和削峰，不生成填充内容，不延迟已排队生图。
- 生图提示词优化仅在真实产品请求中执行，并保留同一用户/项目、来源任务和幂等 ID。
- 对话删除仅响应用户删除、项目 TTL 或数据生命周期事件。

### 8.2 图片保留量

定义：

- `D`：当前可生图账号数。
- `F`：空闲可生图账号数。
- `Qimg`：生图队列深度。
- `Qtext`：真实文本队列深度。

```text
Rimg(D) = D               , D < 10
          ceil(0.8 × D)   , D >= 10
```

```text
text_admit =
  Qtext > 0
  AND account_text_healthy
  AND node_bound
  AND (
    存在“文本健康但当前不可生图”的账号
    OR (Qimg = 0 AND F > Rimg(D))
  )
```

当可生图账号少于 10 个时，全部可生图账号保留给图片队列。文本优先使用 `image quota=0/restore_at 未到` 但文本健康的账号；文本请求不扣图片额度，也不占图片候选槽位。

真实文本任务排序：

```text
score = priority_weight
      + log(1 + queue_wait_seconds)
      + deadline_pressure
      + health_score
      + Gumbel(0, τ)
```

随机项仅打破同优先级平局；临近 SLA 时 `τ → 0`。生图任务不增加随机等待。

### 8.3 Canary

1. **24h shadow**：只记录选择结果，不发送额外请求。
2. **72h 单号 live**：选择 1 个文本健康、当前不可生图账号；全局文本 in-flight=1；只有真实文本任务，无每日最低次数。
3. **7d 观察**：记录 T+1h、6h、24h、72h、7d 的账号、节点、认证和资源状态。
4. 72h 全部通过且号池满足门禁后，再扩到 2 个账号。

验收：文本导致的图片候选减少、图片 429、图片排队延迟、图片额度非预期变化均为 0；重复 conversation 和重复上游提交为 0；节点漂移和新增 terminal 为 0；调度器 RSS 增量 `<20MiB`，FD/线程/slot 回到基线。

当前 live 条件为 `schedulable=0`、`quota=0` 且磁盘 86.1%，所以先保持 shadow/read-only 观察；恢复 live 的前置条件是磁盘回到门禁内、至少一个绑定完整且文本健康的非图片候选账号，以及真实文本队列存在。

已创建 Codex automation `panda-72h-shadow`：每 6 小时执行一次，共 13 轮覆盖 72 小时。每轮只做一次有界只读 SSH，计算 `D/F/Qimg/Qtext/Rimg/text_admit` 并写本地脱敏报告；它不发送文本/生图请求，也不自动部署。

纯策略已在 `services/account_workload_policy.py` 落地，并提供 `scripts/account_workload_shadow.py` 离线快照入口；10 个单测覆盖小号池全量图片保留、文本专用账号、D=10余量、图片优先、空队列、节点绑定和固定seed平局打散。该模块目前只用于shadow验证，尚未接入生产调度器。

本轮离线结果：当前 Panda 快照因 `D=0` 且节点绑定证据缺失，决策为 `idle/node_not_bound`；5号池模拟中 `Rimg=5`，图片账号保持 `idle/image_reserve_protected`，文本专用账号得到 `text/text_only_capacity`。输入和结果保存在 `data/runlogs/workload-shadow-*-20260716*.json`。

## 9. 自动化暴露面与请求一致性

目标是减少系统自身造成的固定周期、突发并发、重复请求、跨身份矛盾和畸形请求。采用持久且自洽的账号—节点—runtime profile—会话环境；逐请求改动稳定字段会造成更大的内部矛盾。

字段分层：

- 生命周期稳定：Webshare 节点、UA/CH/TLS档位、客户端版本、locale、timezone、设备标识、Profile版本。
- 会话稳定：Cookie jar、鉴权状态、session、conversation lineage。
- 请求唯一：request ID、message ID、幂等键、附件 ID。
- 上游派生：sentinel、CSRF、conversation ID、文件指针，按真实响应保存。
- 内容派生：用户原始文本、提示词、附件元数据；审计兼容层追加的后缀、模板和重复 payload。

门禁：

1. 一个账号始终使用一个节点租约和同一 runtime profile 版本。
2. NewAPI、兼容层和 backend 只保留一个重试协调器，避免三层叠加。
3. 已取得 conversation ID 的超时只轮询原会话，不重新提交生成。
4. 后台观察使用时间窗口，不在固定秒点同时触发全部账号。
5. 真实队列为空时保持空闲。
6. 对话清理只执行真实 TTL、用户删除或项目生命周期操作。
7. 记录 header/body 的脱敏形状 hash、字段集合、大小和阶段耗时，不记录正文、token、Cookie或代理口令。

## 10. 新批 Outlook 接入与 Panda 观察

### 10.1 Windows 本地接入

1. 校验 Outlook 登录、OTP 可读和凭据字段完整。
2. 创建 `source_batch_id`、`cohort_id` 和 Webshare 节点租约。
3. 只注册 1 个 canary；注册、OTP、首次登录、token 验证均使用同一固定节点。
4. 保存注册出口 hash、runtime profile、加密冷 Profile 和交接 manifest。
5. 上传数 KiB 账号元数据、会话材料 hash和节点租约；Panda 先写 `incoming`，不直接进入生产池。

### 10.2 Panda 成熟期

| 时间 | 动作 | 放行标准 |
| --- | --- | --- |
| T+1h | 状态、token 元数据、节点 hash、slot 对账 | 无 invalid、无漂移、slot 差值 0 |
| T+6h | 一次轻量聊天健康检查 | 成功、节点 BOUND、资源释放 |
| T+24h | 一次真实业务任务 | 成功、无重复请求、无认证异常 |
| T+72h | 复核 token、节点、真实任务、错误率 | terminal=0、连续鉴权失败=0 |
| T+7d | cohort 生存与统计评估 | 未达门禁则停止扩批 |

扩容顺序：`1 → 2 → 3 → 每批最多 3`。新批未完成 T+24h 前不启动下一批；任一明确 terminal 立即熔断该 cohort。

## 11. 7 天存活率 >99% 的统计口径

- 小批次要求 terminal=0，属于早期门禁，不等于已经证明真实存活率 >99%。
- 正式分母是“各自完成完整七天观察的独立账号数”；account-days 只作暴露量辅助指标。
- 在零 terminal 的前提下，至少 299 个账号各自完成完整 7 天观察，一侧 95% Clopper-Pearson 下界才略高于 99%。
- terminal 始终保留在原 cohort；删除账号行不改变统计分母。
- 分母排除项必须预先定义，并保留原因、操作者和时间事件。

## 12. 鉴权、节点与生命周期状态机

```text
HEALTHY
  ├─ 401 → REFRESH_ONCE → HEALTHY / RELOGIN_REQUIRED
  ├─ 403 invalid → QUARANTINED → 单次完整确认
  ├─ 429 → LIMITED → restore_at → HEALTHY
  ├─ account_deactivated → TERMINAL
  ├─ egress drift → NODE_QUARANTINED + ACCOUNT_PAUSED
  └─ network error → TRANSIENT_BACKOFF
```

- 401 每个业务请求最多 refresh 一次。
- 403、429、terminal、网络错误分开统计。
- terminal 排除出文本、生图、maintenance、恢复和调度。
- 网络失败先降低节点健康，不直接写账号终态。
- 文本候选必须过滤 invalid 证据、近期 401/403、节点隔离和未成熟账号，并增加单账号 text lease。

## 13. ChatGPT Web 聊天/生图逆向优化清单

### P0：请求构造、会话安全与失败归因

- 聊天和生图分别建立单一 canonical request builder。
- API Session 默认头保持最小集合；Authorization、OAI 身份头只在 ChatGPT API 请求中临时组装，跨域上传/下载使用同节点的独立 resource Session。
- 图片 prepare/start 与聊天使用同一套 sentinel 派生头，缺少必要 conduit 时在 start 前结束并记录阶段错误。
- 账号验号先执行 `/me` fail-fast；通过后再读取 quota/account，避免失效账号一次放大为三次请求和关闭竞态。
- runtime profile 补齐后完整持久化；持久化失败记录结构化事件。对比生产 `fp=0/18` 与本地代码，建立部署版本/hash检查。
- 每个请求记录阶段：`preflight → node_connect → auth → request_build → upstream_submit → sse_ready → conversation_started → poll → result_resolve → download → downstream_write → cleanup`。
- 日志只保存账号、节点、conversation、request-id和请求形状的 hash。

### P1：聊天与生图会话可靠性

- 文本三个兼容端点只验收聊天、流式、续聊和错误映射。
- 首次文本尝试复用已建立的账号 backend；只有 token 切换后再创建新 Session。
- SSE ready 继续以 conversation ID 为准；图片结果只接受上游真实产物指针，输入附件不当作输出。
- conversation 恢复使用请求发出前的 started_at和关联 message ID；移除“取最新对话”兜底，避免同账号并发串会话。
- 已取得 conversation ID 后只续轮询原会话；逻辑任务保存 attempted token与幂等键，重复生成数保持 0。
- JPEG/WebP/GIF 上传文件名扩展与真实 MIME 对齐。
- `quality=auto` 不向用户提示词追加无意义固定句；其他质量指令由产品层显式映射并纳入 A/B。
- client cancel 贯穿 SSE reader、poll、下载和 slot；成功、异常、超时、取消后连接/session/executor/FD/slot回到基线。

### P2：轮询与性能

- conversation 作为主轮询源；tasks 降为低频终态诊断。
- 轮询使用分段退避并设置单任务最大上游 GET 数、最大 wall time 和最大 resume 次数。
- 连接池按账号+节点隔离复用，禁止跨账号共享 Cookie/session。
- 文本流与生图使用独立执行窗口；流量计数后续改为内存累计、30～60秒批量 flush。
- 采集首包、conversation-ready、生成、poll、下载、编码和下游回传各阶段耗时。
- 对重复提交、迟到线程、slot 泄漏、CLOSE_WAIT和长尾连接建立独立指标。

## 14. 测试矩阵

### 14.1 聊天接口

| 维度 | 用例 |
| --- | --- |
| 端点 | chat/completions / responses / messages |
| 模式 | stream / non-stream |
| 会话 | 单轮 / 多轮续聊 / token refresh / cancel |
| 内容 | 文本 / 多消息 / 图片输入（支持处）/ 大输入 |
| 错误 | 401 / 403 / 429 / timeout / disconnect / malformed SSE |
| 资源 | Session复用、关闭、FD/线程回基线、文本lease |

### 14.2 生图接口

| 维度 | 用例 |
| --- | --- |
| 入口 | images/generations / images/edits / Chat兼容生图 / Responses兼容生图 |
| 输入 | 文生图 / JPEG、PNG、WebP参考图 / 单图、多图 |
| 会话 | 正常SSE / 丢 conversation ID / timeout_pending / resume poll |
| 去重 | 客户端重试 / NewAPI重试 / backend重试 / 同幂等键 |
| 错误 | 401 / 403 / 429 / quota0 / timeout / disconnect / terminal |
| 安全 | 跨域资源敏感头缺席、附件名/MIME一致、secret不入日志 |

### 14.3 账号、节点与性能

- 账号数：1 / 2 / 3 / 10 / 18。
- 节点：READY / OBSERVE / QUARANTINED / drift / 缺证据。
- 成熟度：1h / 6h / 24h / 72h / 7d。
- 负载：空闲、文本单请求、生图单请求、真实文本+生图交错、maintenance存在。
- 每档至少3轮，记录CPU、RSS、线程、FD、连接、SQLite写延迟、队列、首包、总延迟和流量。
- 生产从单 canary起步，下一档前必须回到资源基线。

## 15. 验收标准

| 类别 | 标准 |
| --- | --- |
| 节点 | 出口稳定；生命周期漂移 0；注册/运行出口证据覆盖 100% |
| 绑定 | 静默迁移 0；解绑和重绑均有事件 |
| 成熟期 | 1/6/24/72h任务准时率 ≥99.9%，提前入池 0 |
| 生存 | 小批 terminal=0；正式统计按完整7天独立账号 |
| 文本 | 独立限频、text lease、真实任务完成率 ≥99% |
| 生图 | 图片容量保护生效；slot/task差值0；重复提交0 |
| 逆向链路 | 请求形状可审计；跨域敏感头0；会话串线0 |
| 重试 | 已有conversation的重新生成0；单任务poll不越预算 |
| 资源 | 无OOM；内存、负载、磁盘均在门禁内 |
| 连接 | 完成后FD/线程/ESTABLISHED/CLOSE_WAIT回到基线 |
| 流量 | 成功请求产生非负增量；历史未知不伪造为0 |
| UI | 代理凭据泄露0；宽屏充分利用；窄屏可横向滚动 |
| 日志 | token、Cookie、邮箱密码、代理口令和正文泄露0 |
| 回滚 | 代码、web_dist、SQLite、配置和节点租约可在10分钟内恢复 |

测试矩阵结果是发布门禁的一部分；关键组合失败时整版保持未验收。

## 16. 实施顺序

### P0：当前立即执行

1. Panda 磁盘水位治理，恢复到 85%以下后再部署或 live canary。
2. 修复 API/resource Session 隔离、图片 sentinel 头、验号 fail-fast、空 conduit fail-fast和 fp 持久化可观测性。
3. 增加 header/body shape hash、阶段耗时、poll/重复提交计数；建立生产代码/hash对账。
4. 停止零额度账号进入图片调度，收敛 timeout/quota 重试放大。

### P1：下一批 Outlook 前

1. 节点注册表、租约、绑定写保护、出口证据和漂移隔离。
2. cohort表、1/6/24/72h/7d成熟任务和熔断。
3. Windows加密冷Profile交接；Panda轻量runtime profile。
4. `AccountWorkloadPolicyService`、文本健康过滤、text lease和图片容量保护。
5. 24h shadow canary和差异报告。

### P2：扩到2～3个前

1. 72h单号live与7d观察。
2. conversation恢复去串线、poll请求预算和单一重试协调器。
3. 单号/多号性能矩阵、故障注入和回滚演练。
4. 完整7天cohort报表与统计下界。

## 17. 当前结论

1. 上一轮未完成项已重新收敛为：节点租约/成熟期、真实队列canary、逆向链路一致性与失败归因。
2. 工具相关优化、实测矩阵与验收门禁已从本方案移除；chatgpt2api文本模型继续保持聊天能力。
3. Panda注册门禁只约束Panda接收后的重任务；Windows本地少量注册和上传的性能边界已明确。
4. 文本与生图额度独立；小号池全部图片候选保留给生图，随机分布只平滑真实文本队列。
5. 最新账号池已退化到schedulable=0、quota=0；共同出口、绑定证据缺失、失败/轮询放大比“某个固定请求头”更有现成证据。
6. 请求头、后缀和payload统一性继续作为高价值审计项，通过脱敏shape遥测和survivor/terminal差异报告验证。
7. Panda磁盘已回落至49%，磁盘门禁解除；本地修复仍须备份后按单canary部署，并成对带上缺失模块（如 `account_fingerprint.py`）。
