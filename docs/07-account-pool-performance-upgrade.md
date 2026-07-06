# 账号池与生图性能升级落地文档

最后更新：2026-07-04

## 1. 背景

近期 Panda / NewAPI 生图链路暴露出以下问题：

- Panda 端账号文件全量写放大，CPU/BlockIO 尖峰明显。
- `import-batch` 曾出现公网高频调用，导致每次同步都可能触发账号池全量写。
- maintenance 循环正在大量发现死号，说明远端承受了过多新号质量筛选压力。
- 生图取号 preflight、mark image result、删除死号等路径均可能触发账号持久化。
- NewAPI 侧出现 429、500、502、broken pipe、长尾耗时。
- `dictionary changed size during iteration` 明确说明存在账号池并发读写 bug。
- 大量 b64_json 同时回传可能形成尾部带宽、CPU 和内存拥塞。
- 12 worker 实测不是 CPU/内存打爆，但上游尾流、524/reset、poll timeout retry 明显恶化；说明并发提升必须走双通道队列和自适应 worker，而不是固定拉高 worker。

本升级的核心原则：

```text
本地主筛号，Panda 只接收高置信账号；
本地和 Panda 都降低账号状态写放大；
公共 API 不做无限队列；
最后按优化后的真实数据再调并发。
```

## 2. 模块边界

| 模块 | 当前事实 | 本次目标 |
| --- | --- | --- |
| 本地账号池 | 代码主要使用 `data/accounts.json`，用户口径含 `Accounts.js` | SQLite 主存储，文件仅低频快照 |
| 本地探活 | 注册后快速同步会把短命号带给 Panda | `1h/3h/6h` 三次探测，high confidence 才上传 |
| 本地同步 | `scripts/sync_accounts_delta_to_panda.ps1` 读 JSON 并上传 | 改为从 SQLite 选 `verified_ready`，按水位上传 |
| Panda 同步入口 | `/api/accounts/import-batch` | 限频、签名、幂等、水位限流、低写放大 upsert |
| Panda 账号存储 | JSON 或现有 DB backend 仍存在全量保存语义 | 增量 SQLite / batch transaction |
| Panda maintenance | 负责大量清死号 | 改成抽检、错峰、批量提交 |
| 生图 preflight | 易触发账号状态写 | 无变化不写，关键变化进入 dirty queue |
| b64 回传 | 大响应可能拖尾 | 独立回传窗口，URL 优先，公共 API 快拒绝 |
| 生图队列 | 现有 `ImageTaskService` 是提交即开线程，不是真队列 | 中央调度队列 + SQLite 任务状态 + timeout_pending 续轮询 |
| CPU 预算 | Panda CPU 当前有空闲，但上游长尾才是主瓶颈 | 生图预算按 1.5 vCPU 设计，CPU>=90% 触发 deadlock_guard |

## 3. 本地账号池 SQLite 设计

### 3.1 迁移目标

- SQLite 是主存储。
- `data/accounts.json` / `Accounts.js` 只作为兼容导出。
- 注册、探活、删除、同步状态都写 SQLite。
- 文件导出低频化，保护本地磁盘。

### 3.2 状态字段

推荐状态：

```text
local_staging
probe_1_waiting
probe_1_passed
probe_2_waiting
probe_2_passed
probe_3_waiting
verified_ready
upload_pending
uploaded_to_panda
dead_confirmed
quarantine
transient_retry
```

置信度：

```text
low       注册完成但未完成探测
medium    通过 1h + 3h
high      通过 1h + 3h + 6h
dead      明确死号
transient 临时失败待重试
```

### 3.3 写入策略

- 注册结果：20~100 个一组 transaction upsert。
- 探活结果：批量写账号状态和 `probe_events`。
- 死号：先软标记 `dead_confirmed`，后续低频清理。
- 上传成功：批量更新 `panda_sync_state=uploaded`。
- 文件快照：15~30 分钟导出一次，或手动/退出/备份点导出。

## 4. 本地探测策略

探测时间：

```text
T+1h
T+3h
T+6h
```

轻探活内容：

- `/backend-api/me`
- token refresh 能力
- entitlement / plan / account 状态
- quota / limits 元数据

不默认真实生图。

死号判定：

- `401`
- `revoked`
- `invalidated`
- `token invalidated`
- 明确 refresh token failure 且达到阈值

临时失败：

- timeout
- 502/503/504
- 代理异常
- TLS/HTTP2 临时错误

临时失败进入 retry，不直接删。

## 5. Panda 端低写放大设计

### 5.1 storage backend 需要补的能力

当前 `StorageBackend` 只有：

```python
load_accounts()
save_accounts(accounts)
```

这天然鼓励全量保存。需要新增或在服务层封装：

```python
upsert_account(account)
upsert_accounts(accounts)
delete_accounts(tokens)
update_account_fields(token, updates)
flush_dirty_accounts()
```

### 5.2 SQLite 表建议

```sql
CREATE TABLE accounts (
  token_hash TEXT PRIMARY KEY,
  access_token TEXT NOT NULL,
  status TEXT,
  quota INTEGER,
  type TEXT,
  source_type TEXT,
  email TEXT,
  user_id TEXT,
  created_at TEXT,
  updated_at TEXT,
  last_used_at TEXT,
  last_quota_refresh_at TEXT,
  last_invalid_at TEXT,
  invalid_count INTEGER DEFAULT 0,
  raw_json TEXT NOT NULL
);

CREATE INDEX idx_accounts_sched ON accounts(status, quota, type, source_type);
CREATE INDEX idx_accounts_updated ON accounts(updated_at);
```

### 5.3 事务策略

- `import-batch`：一个 batch 一个 transaction。
- maintenance：一个小批次或批结束一个 transaction。
- preflight：无变化不写，有变化进入 dirty queue。
- mark image result：累积后 5~15 秒或 20~50 个 mutation flush。

## 6. Panda maintenance 策略

### 6.1 由全量清理改为水位驱动抽检

| Panda 可用号 | maintenance 策略 |
| ---: | --- |
| >=1500 | 不全量扫，每小时抽检 50~100 个 |
| 500~1499 | 轻量维护，每轮 100~300 个，并发 1~3 |
| 200~499 | 优先本地补货，只清明显死号 |
| <200 | 避免大规模探活占用最后可用资源，优先生图和补货 |

### 6.2 并发估算

见 `../plan.md` P5。执行时必须记录单号耗时 p50/p95，然后动态调参。

## 7. b64 回传窗口

### 7.1 原则

- URL 优先，b64 兼容。
- 生成窗口、落地窗口、回传窗口分离。
- 公共 API 不做无限等待。

### 7.2 初始参数

```text
b64_active_limit = 4~6
b64_bytes_inflight_limit = 32~64MiB
b64_queue_timeout = 5~10s
```

### 7.3 行为

- 窗口有槽：开始 encode / copy response。
- 窗口满：短等。
- 短等失败：公共 API 返回 429/503 + Retry-After。
- 客户端断开：立即释放回传窗口，记录 broken pipe/cancel。


## 8. 生图双通道、CPU 预算与死锁保护

代码与生产状态（2026-07-04）：

- `ImageTaskService` 已改为 SQLite 中央队列，主状态文件 `data/image_tasks.db`。
- `timeout_pending` 语义已落地：带 `conversation_id` 的 poll timeout 不再换号重开图。
- `image_deadlock_guard` 已新增，maintenance 已在 guard tripped 时 pause。
- API 队列满/熔断返回 `429 + Retry-After`。
- 本地受影响测试集合 71 passed。
- 已部署到生产 Panda，备份目录 `/root/gptimage/backups/p6-image-queue-20260704-175026/`，回滚脚本 `ROLLBACK.sh`。
- 生产 12 async task 受控压测 12/12 success；完成耗时 min `38s` / p50 `202.5s` / p95 近似 `295s` / max `304s`。
- R5.5 的 100 任务容量压测、CPU>=90% 人工熔断验收、R7 soak 仍未执行。

### 8.1 双通道

```text
同步快车道：OpenAI/NewAPI 兼容接口，6 worker，短等/快拒绝，不承接长队。
异步任务道：图片管理页/批处理，高并发先入队，后台按 worker 平滑提交。
```

同步入口保留兼容性，但不追求 100 个请求长连接同时等完；异步入口负责高并发体验。

### 8.2 timeout 不重开图

当前最需要修正的语义：拿到 `conversation_id` 后，poll timeout 不能换号重新提交。正确行为：

```text
conversation_id exists -> timeout_pending -> 后台继续 poll 原 conversation
conversation_id missing -> 仅允许有限 pre-submit 重试
```

### 8.3 CPU 预算

```text
生图预算：1.0 vCPU -> 1.5 vCPU
正常目标：CPU p95 <= 70%
告警阈值：CPU p95 > 80%
死锁阈值：CPU >= 90% 持续 60s
```

90% 不是运行目标，而是熔断线。触发后必须暂停新增提交、暂停 maintenance、降低 worker、同步入口快拒绝。

### 8.4 初始参数

```text
sync_image_workers = 6
async_submit_workers = 6
async_submit_workers_max = 8
async_poll_workers = 24
async_poll_global_qps = 4
async_download_workers = 4
b64_return_window = 4
global_queue_max = 200
per_user_running_max = 2
per_user_queue_max = 20
```

### 8.5 18 / 24 / 30 压测前不提最终优化

P6 已在 Panda 完成 12 async task 受控压测。下一步必须先执行 R5.6：

```text
18 async tasks
24 async tasks
30 async tasks
```

该阶梯用于判断真实瓶颈，不直接等同于 worker 数。压测前后必须采集：

- CPU / memory / PIDs。
- BlockIO read/write。
- 健康页与任务查询延迟。
- 账号池 active / schedulable / total_quota。
- submit / queue_wait / running / total duration。
- timeout_pending / duplicate_submit / retry 分布。
- HTTP 5xx、524、reset、broken pipe。
- **带宽**：`bandwidth_rx_mbps`、`bandwidth_tx_mbps`、`bandwidth_total_mbps`。

公网 30Mbps 带宽判定：

```text
p95 < 18Mbps：正常
18~24Mbps：警戒
>=24Mbps 持续 60s：停止进入下一档
>=28Mbps 连续 15s：停止本档新增提交
```

R5.6 报告出来前，不做最终 worker、RPM、CPU、桶数建议。

### 8.6 输入不减重

禁止通过降低输入复杂度换取漂亮数据：

- 不压缩 / 缩小 / 删除参考图。
- 不删 prompt、mask、多图输入、参考输入。
- 不降低真实业务请求的 reference fidelity。

可以优化输出路径，例如 URL 优先或 b64 回传窗口，但这属于输出回传，不得改变输入。

### 8.7 多桶 / 多出口候选设计

只有 R5.6 数据证明单桶继续加 worker 会拉长尾流或增加 5xx/reset 时，才进入多桶 / 多出口设计。

桶的定义：

```text
bucket = scheduler + account subpool + egress/proxy + circuit breaker + metrics
```

每个桶独立：

- running / queue / p95 / error_rate。
- bandwidth rx/tx。
- account quota pressure。
- 524/reset/broken pipe。
- 熔断与恢复。

新任务路由：

```text
选择 score 最低的桶：
score = running + queue_wait + recent_p95 + error_rate + bandwidth_pressure + quota_pressure
```

多出口目标：

- 分散 Cloudflare/NewAPI 长连接和上游波动。
- 某个出口坏了，只熔断该桶。
- 避免单出口 30Mbps 带宽和连接稳定性成为全局短板。

### 8.8 账号质量分层不作为主方案

当前账号死得太快、号池持续刷新，长期 gold/silver/bronze 质量分层成本高，且容易被短期死亡窗口打穿。

保留的只是短期运行态：

```text
ready
in_use
limited
invalid
transient_backoff
quarantine
```

调度只基于当前可用性、近期错误、quota、短期 backoff，不依赖长期质量标签。

### 8.9 减少无效重试

如果 R5.6 发现失败来自重复上游提交或无效重试，可以优先做：

- 拿到 `conversation_id` 后 timeout 不重开图，只进入 `timeout_pending`。
- `401/403/token invalid/revoked/quota exhausted` 不重试同号。
- pre-submit transient 网络错误最多 1 次重试，带 jitter。
- `task_id` / idempotency key 防重复提交。
- 对账号和出口分别做短期 circuit breaker。

验收指标：

```text
duplicate_submit_rate < 1%
attempt_count p95 <= 1~2
timeout_pending 不导致第二个上游 conversation
无效账号不被连续反复命中
```

### 8.5 升降档

升档必须满足：成功率、p95、timeout_pending、HTTP/2/reset、CPU p95 同时健康。降档任一异常即可触发。

## 9. 不做事项

- 不给 `canvas.best` 做内部无限队列。
- 不把公共 NewAPI 请求排成几十/上百长队。
- 不依赖固定 IP allowlist。
- 不在 SQLite 迁移时丢弃未知字段；必须保留 raw_json。

## 10. 验收入口

- 执行计划：`../plan.md`
- 同步策略：`sync-strategy.md`
- 验收测试：`performance-acceptance-test-plan.md`



## 11. 2026-07-06 本地 staging 动态档位落地

本轮把“新号本地成熟后再上传 Panda”的策略从固定档位改成水位驱动：

- 正常水位：`30/120/360min`，兼顾质量与较早发现死号。
- 低水位：`10/30/90min`，缩短成熟时间。
- 应急水位：`5/15/45min`，至少跨过 5 分钟死亡窗口，但不直接无探活上传。
- 上传侧优先消化 `ready` backlog；Panda 空池时不让大批 staging 探活阻塞 ready 上传。
- 单批仍默认 20，依赖 30/60 秒间隔加速，而不是靠超大批次压远端导入入口。

当前判断：Panda 空池时，首要瓶颈是本地 ready 到 Panda 的出货节流，不是注册入池速度。
