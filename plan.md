# 账号池与生图性能升级执行计划

最后更新：2026-07-04

## 0. 执行 Contract

### 目标

把当前「本地注册机 -> 本地账号池 -> Panda 远端账号池 -> NewAPI / canvas.best 公共生图调用」链路升级为低 IO、可错峰、可回滚、可验收的稳定架构。

核心目标：

1. 新号先进入本地 staging 号池，经过 `1h / 3h / 6h` 三次保守探测后才允许上传 Panda。
2. 本地账号文件（用户口径 `Accounts.js`，当前代码事实主要是 `data/accounts.json`）迁移到 SQLite 主存储，保护本地磁盘。
3. Panda 水位线提高到 `high=1500`、`low=500`，按水位动态控制同步频率，避免 Panda 被同步/探活拖垮。
4. Panda 端修复账号池并发读写 bug，降低 preflight / maintenance / import-batch 写盘压力，并逐步迁移 SQLite。
5. 公网同步入口在动态公网 IP 场景下使用限频、强鉴权、HMAC、nonce、idempotency 保护，不依赖固定 IP allowlist。
6. `https://canvas.best/` 按外部公共调用方处理，不做内部无限队列；公共 API 只允许短等和快拒绝。
7. 对 b64 大响应回传设计独立窗口，避免生成完成后尾部回传拖垮 NewAPI / Panda。
8. 最后用多轮测试验收并按优化后数据重新估算并发、RPM、CPU 核心和瓶颈。
9. 生图链路升级为“同步快车道 + 异步任务队列”双通道：同步入口保体验、异步入口承接高并发。
10. Panda 生图 CPU 预算从原 `1.0 vCPU` 设计提升到 `1.5 vCPU`，但 `CPU >= 90%` 只允许作为死锁/熔断阈值，不允许作为常态运行目标。

### 本轮边界

- 本计划是后续执行指导文档。
- 执行生产改动前必须备份、标记回滚路径、明确验证命令。
- 未经单独批准，不直接改生产、不重启生产、不删除生产数据。
- `canvas.best` 是外部公共用户，不进入内部批处理队列设计。

### 当前已确认事实

- Panda 远端：`ssh panda`，项目路径 `/root/gptimage`，公网 `https://gptimage.relai.asia`。
- NewAPI 公网：`https://new.relai.asia`，当前主要暴露 Panda 生图长尾、429、502、broken pipe。
- Panda 容器曾确认限制约 `1 CPU / 1.5GB`，主机 2 核；2026-07-04 新策略允许给生图调度模型增加 `0.5 vCPU` 预算，即按 `1.5 vCPU` 设计，但必须配套 90% CPU 死锁熔断。
- 2026-07-04 只读复核：panda `image_global_concurrency=6`，`image_account_concurrency=1`，账号池约 96 个账号 / 365 总额度；容器 CPU 低位、内存约 435MiB，说明当前主要瓶颈不是 CPU/内存，而是上游长尾、账号质量、Cloudflare 长连接和 timeout 后重试放大。
- Panda 当前历史问题包括：`accounts.json` 全量写放大、`import-batch` 高频公网同步、maintenance 清大量死号、`dictionary changed size during iteration` 并发读写 bug、b64 大响应尾流。
- 本地仓库已有 storage backend 抽象和 `DatabaseStorageBackend`，但现有实现仍是 `save_accounts()` 全量 delete + insert，不等于真正低写放大增量存储。
- 当前同步脚本 `scripts/sync_accounts_delta_to_panda.ps1` 直接读取 `data/accounts.json` 并调用 Panda `import-batch`。

## 1. 执行总览

```text
P0 远端止血
P1 本地账号池 SQLite 主存储
P2 本地 1h/3h/6h 探测和高置信上传
P3 水位驱动同步 + 公网同步入口保护
P4 Panda SQLite / 低写放大改造
P5 maintenance / preflight 批量提交和错峰
P6 生图双通道异步队列、CPU 预算和死锁保护
P7 b64 回传窗口与公共 API 快拒绝
P8 多轮测试、压测、验收、重新估算并发/RPM/CPU
```

执行顺序不能倒置：先止血和存储，再做队列/并发。否则只是把尾部问题藏进队列。

---

## P0. 远端 Panda 止血

### P0-1 修复 `dictionary changed size during iteration`

目标：消除生图取号时 `_accounts` 被 maintenance/import 同时修改导致的 500。

动作：

- 搜索所有遍历 `self._accounts.values()` 的路径。
- 对候选账号列表生成使用锁内 snapshot：

```python
with self._lock:
    accounts = [dict(item) for item in self._accounts.values()]
# 锁外遍历 accounts
```

- 重点修 `AccountService.get_available_access_token()` 前置候选数统计和 `_list_ready_candidate_tokens()` 调用边界。

验收：

```bash
python -m pytest test/test_account_image_capabilities.py test/test_account_refresh_all_service.py test/test_account_maintenance_loop_service.py -q
```

生产日志验收：

```text
NewAPI/Panda 最近日志中 dictionary changed size during iteration = 0
```

回滚：恢复修改前 `services/account_service.py`。

---

### P0-2 公网 `import-batch` 限频

目标：即使本地公网 IP 变化，也不能让同步脚本循环把 Panda 打爆。

动作：

- 不做固定 IP allowlist。
- Nginx 对 `/api/accounts/import-batch` 单独限频：建议 `1 req/min`，`burst=2~3`。
- 返回 429 时带清晰错误，客户端按 `Retry-After` 退避。

验收：

```bash
# 连续快速请求应出现 429，正常间隔请求应 2xx
```

回滚：恢复 Nginx 配置备份并 reload。

---

### P0-3 maintenance slow 阈值修正

目标：生图有 in-flight 时 maintenance 进入 slow，不再和生图抢 CPU/IO，但不完全暂停，避免死号清不掉。

建议配置：

```json
{
  "slow_when_image_inflight": 1,
  "slow_batch_limit": 5,
  "slow_delay_between_accounts_sec": 8,
  "slow_cooldown_sec": 30,
  "pause_when_image_inflight": 0
}
```

验收：

- 生图 inflight > 0 时 maintenance status 应显示 slow 或实际批次规模/延迟下降。
- Panda BlockIO 和 CPU 尖峰下降。

---

### P0-4 preflight 无变化不写

目标：生图取号前探活不应每次都写完整账号池。

动作：

- `fetch_remote_info -> update_account` 前比较关键字段。
- 如果结果与当前账号无实质变化，只更新内存态或低频 dirty queue。
- 只有 token/status/quota/type/source_type 等调度关键字段变化时才持久化。

验收：

- 8 并发生图期间 BlockIO 写入显著下降。
- 可用账号调度不回退。

---

## P1. 本地账号池 SQLite 主存储

### 目标

本地账号池不再以高频 `Accounts.js` / `data/accounts.json` 文件写入为主，改为 SQLite 主存储，旧文件仅作为低频兼容导出。

### 注意命名

用户口径里称 `Accounts.js`；当前仓库代码事实主要是：

```text
data/accounts.json
scripts/sync_accounts_delta_to_panda.ps1
```

执行时必须先确认本地注册机实际读写文件名，兼容历史 `.js/.json` 备份。

### 表设计

主表：

```sql
CREATE TABLE local_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT UNIQUE NOT NULL,
  access_token TEXT NOT NULL,
  email TEXT,
  user_id TEXT,
  source_type TEXT DEFAULT 'web',
  register_batch_id TEXT,
  status TEXT NOT NULL DEFAULT 'local_staging',
  confidence TEXT NOT NULL DEFAULT 'low',
  probe_stage INTEGER NOT NULL DEFAULT 0,
  probe_pass_count INTEGER NOT NULL DEFAULT 0,
  probe_fail_count INTEGER NOT NULL DEFAULT 0,
  transient_fail_count INTEGER NOT NULL DEFAULT 0,
  next_probe_at TEXT,
  last_probe_at TEXT,
  last_probe_result TEXT,
  last_probe_error TEXT,
  first_registered_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  verified_at TEXT,
  upload_ready_at TEXT,
  uploaded_at TEXT,
  panda_sync_state TEXT DEFAULT 'not_uploaded',
  panda_sync_attempts INTEGER DEFAULT 0,
  panda_sync_last_error TEXT,
  raw_json TEXT NOT NULL
);
```

事件表：

```sql
CREATE TABLE local_probe_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL,
  stage INTEGER NOT NULL,
  probe_at TEXT NOT NULL,
  result TEXT NOT NULL,
  error TEXT,
  latency_ms INTEGER,
  raw_json TEXT
);
```

同步批次表：

```sql
CREATE TABLE panda_upload_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  uploaded_at TEXT,
  status TEXT NOT NULL,
  account_count INTEGER DEFAULT 0,
  panda_response TEXT,
  error TEXT
);
```

SQLite 参数：

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
```

### 兼容策略

- SQLite 是主存储。
- `data/accounts.json` / `Accounts.js` 变为低频导出快照。
- 禁止每新增、探活、删除、同步一个账号就写文件。
- 导出频率：15~30 分钟、程序退出、手动导出、关键备份点。

验收：

- 注册成功账号进入 SQLite。
- 探活状态进入 SQLite。
- 死号标记/删除不高频写文件。
- 同步成功批量更新 `panda_sync_state`。
- 文件写入频率相比当前显著下降。

---

## P2. 本地 1h / 3h / 6h 探测

### 状态机

```text
registered
-> local_staging
-> probe_1_waiting
-> probe_1_passed
-> probe_2_waiting
-> probe_2_passed
-> probe_3_waiting
-> verified_ready
-> upload_pending
-> uploaded_to_panda
-> local_archived / local_deleted
```

失败：

```text
probe_failed_invalid -> dead_confirmed -> local_deleted/quarantine
probe_failed_transient -> retry_later
```

### 探测时间

```text
T+1h
T+3h
T+6h
```

通过条件：

- 三次探测均通过。
- 无 `401 / revoked / invalidated / token invalidated`。
- 无连续 refresh failure。
- 最近一次探测结果仍有效。

探测方式：

- 优先轻探活：`/backend-api/me`、entitlement、quota/limits、token refresh 能力。
- 不默认真实生图，避免在本地消耗图片额度。
- 真实生图仅抽样，例如每 100 个 verified_ready 抽 1~3 个。

验收：

- 6 小时内的短命号不会上传 Panda。
- 本地 dead/quarantine 数量可统计。
- Panda 端 `token invalidated` 新增量下降。

---

## P3. 水位驱动同步和公网入口保护

### 水位线

```text
high = 1500
low = 500
emergency = 200
critical = 100
```

### 同步节奏

| Panda 可用号 | 同步频率 | 批量 | 策略 |
| ---: | ---: | ---: | --- |
| >=1500 | 30~60min | 20~50 | 慢同步，保护 Panda |
| 500~1499 | 15~30min | 30~100 | 正常补货 |
| 200~499 | 5~15min | 50~150 | 加速补货 |
| 100~199 | 3~5min | 50~200 | 紧急补货 |
| <100 | 人工确认/紧急策略 | 视情况 | 默认仍只传 high confidence |

默认不上传 medium confidence；保留开关但默认关闭：

```json
{
  "allow_medium_confidence_upload_when_remote_critical": false
}
```

### 动态公网 IP 保护

不用固定 IP allowlist。使用：

1. Nginx 限频。
2. Bearer admin key。
3. HMAC 签名：`X-Sync-Timestamp`、`X-Sync-Nonce`、`X-Sync-Signature`。
4. `X-Idempotency-Key` 防重复写。
5. 应用层水位限流和最小同步间隔。

验收：

- Panda 高水位时同步请求被应用层降频。
- 重复 batch 不重复写库。
- nonce 重放被拒绝。
- 动态公网 IP 不影响合法同步。

---

## P4. Panda SQLite / 低写放大改造

### 当前问题

现有 `DatabaseStorageBackend.save_accounts()` 仍是整表 delete + insert，不是低写放大增量存储。Panda 端必须进一步改为单账号 upsert / delete / batch transaction。

### 目标能力

- `upsert_account(account)`
- `upsert_accounts(accounts)`
- `delete_accounts(tokens)`
- `update_account_fields(token, updates)`
- `mark_dirty_and_flush()` 或事务批量提交

### 迁移方式

1. 备份 `accounts.json`。
2. 导入 SQLite。
3. 校验账号数、状态分布、随机字段。
4. feature flag 切换：`STORAGE_BACKEND=sqlite`。
5. 保留 JSON 快照导出。
6. 可回滚到 JSON。

验收：

- 单账号更新不触发全量 JSON 写。
- import-batch 是 transaction upsert。
- maintenance 删除是批量 transaction。
- 8 并发时 BlockIO 降低 80%+。

---

## P5. maintenance / preflight 批量提交和错峰

### 单号耗时模型

维护循环需要记录并持续计算：

```text
单号探活耗时 p50/p95
死号耗时
正常号耗时
timeout 耗时
每批处理数
每批 CPU/IO 开销
```

### high-low 区间规模

`1500 - 500 = 1000` 个缓冲账号。

1000 个号耗时估算：

| 单号均值 | 并发 1 | 并发 3 | 并发 6 | 并发 12 |
| ---: | ---: | ---: | ---: | ---: |
| 2s | 33min | 11min | 5.6min | 2.8min |
| 5s | 83min | 28min | 14min | 7min |
| 10s | 167min | 56min | 28min | 14min |

### 策略

- SQLite 未完成前：maintenance concurrency 保持 1，只做慢速抽检。
- SQLite 完成后：低峰可 3~6，高峰 1，生图繁忙进入 slow。
- 不建议 12+，除非 Panda 升 CPU 且低峰窗口明确。

验收：

- maintenance 不再导致生图 p95 爆炸。
- 生图 inflight 存在时 maintenance 自动降速。
- 删除死号批量提交，非每号写盘。

---

## P6. 生图双通道异步队列、CPU 预算和死锁保护

### P6-1 结论

当前 6 worker 的体验优于 12 worker；12 worker 没有打爆 CPU/内存，却把尾部拉到 280~380s，并集中出现 524、连接重置、远端断开。这说明瓶颈在上游长尾、账号质量、Cloudflare/NewAPI 长连接和重试放大，不在 Panda CPU。

因此本轮不再把“worker 固定拉高”作为目标，而是：

```text
同步入口：保持 6 worker，短等/快拒绝，避免公共 API 长连接拖死。
异步入口：真实任务队列承接高并发，后台按 6 -> 8 -> 10 阶梯释放压力。
CPU 预算：从 1.0 vCPU 设计提升到 1.5 vCPU，但 90% CPU 是死锁熔断线，不是运行目标。
```

### P6-2 同步快车道

适用：NewAPI/OpenAI 兼容 `/v1/images/generations`、`/v1/images/edits`。

初始参数：

```text
sync_image_workers = 6
sync_queue_timeout = 0~2s
sync_poll_timeout = 105s
sync_retry_after_conversation_created = 0
```

行为：

- 有生成槽：执行。
- 没生成槽：快速返回 `429/503 + Retry-After`。
- 一旦拿到 `conversation_id`，poll timeout 后不允许换号重新提交同一张图。
- 同步请求不承诺 100% 覆盖上游 200s+ 长尾；超时应让调用方重试或转异步任务。

### P6-3 异步任务队列

适用：图片管理页、批处理、高并发用户提交。

状态流：

```text
queued -> submitting -> polling -> resolving -> downloading -> success
                                  -> timeout_pending
                                  -> failed
```

初始参数：

```text
async_submit_workers = 6
async_submit_workers_max = 8
async_poll_workers = 24
async_poll_global_qps = 4
async_download_workers = 4
global_queue_max = 200
per_user_running_max = 2
per_user_queue_max = 20
```

硬规则：

- `ImageTaskService` 不能再“提交一个任务就开一个线程直接跑”；必须改为中央调度队列。
- 任务状态必须持久化到 SQLite，不能继续每次 update 全量写 `image_tasks.json`。
- 任务拿到 `conversation_id` 后，poll timeout 进入 `timeout_pending`，后台继续 poll 原 conversation 5~10 分钟。
- 只有 pre-submit 失败或完全没有 `conversation_id` 时，才允许一次有限重试。
- 队列满必须快速返回 429/503，不能无限排队。

### P6-4 CPU 预算和 90% 死锁熔断

CPU 预算：

```text
baseline_image_cpu_budget = 1.0 vCPU
new_image_cpu_budget = 1.5 vCPU
normal_target_cpu_p95 <= 70%
warning_cpu_p95 = 80%
deadlock_cpu_threshold = 90%
```

`CPU >= 90%` 的含义：

- 不是“还可以继续压”。
- 必须视为死锁/熔断信号。
- 触发后立刻暂停新增异步 submit、暂停 maintenance、同步入口快拒绝、worker 降级。

熔断条件任一满足即触发：

```text
container CPU >= 90% 持续 60s
或 host CPU idle < 10% 持续 60s
或 event loop / health check 延迟 > 5s 持续 3 次
或 running task 120s 内无任何状态推进且 CPU 高位
或 HTTP 5xx / connection reset 突增超过阈值
```

熔断动作：

```text
1. async_submit_workers 降到 4
2. 暂停接受新异步任务，保留 status 查询
3. 同步入口返回 503 + Retry-After
4. maintenance pause
5. b64_return_window 降到 2
6. 记录 deadlock_guard 事件
7. 连续 120s 无恢复才进入受控重启流程；重启前必须保留任务状态
```

恢复条件：

```text
CPU p95 < 65% 持续 5 分钟
5xx/reset 回落到阈值内
队列状态可读写
health check 正常
```

恢复动作：每 10 分钟最多增加 1 个 worker，直到回到熔断前配置。

### P6-5 自适应 worker 阶梯

```text
初始：6
健康 30 分钟：+1
最高先到：8
只有账号池 >300、p95 稳定、timeout_pending 低时才测试 10
12 不作为常驻目标，除非多桶/多出口/多实例后重新验收
```

升档条件：

```text
success_rate >= 95%
p95 <= 180s
timeout_pending_rate <= 10%
upstream_http2_error_rate <= 3%
CPU p95 <= 70%
```

降档条件：

```text
p95 > 240s
timeout_pending_rate > 20%
524/reset/broken pipe > 5%
CPU p95 > 80%
账号池可用额度下降异常
```

### P6-6 容量口径

当前 6 worker、平均 120s 时：

```text
RPM ≈ 6 * 60 / 120 = 3 RPM
100 张图理论完成时间 ≈ 33 分钟
```

异步队列改善的是“连接不炸、任务不丢、排队可见、不会重复烧号”，不是突破上游真实生成速度。要把稳定吞吐提升到 8~12 RPM，需要后续多桶、多出口或多 Panda 实例。

### P6-7 落地状态（2026-07-04）

已完成本地代码切片：

- `ImageTaskService` 已从每任务线程改成 SQLite 中央队列，主状态文件为 `data/image_tasks.db`。
- 异步任务支持 `queued/running/timeout_pending/success/error`，队列满返回 429，不无限排队。
- `conversation_id` 已生成后的 poll timeout 不再换号重开图，而是进入 `timeout_pending` 后台续轮询。
- 新增 `image_task_queue` 与 `image_deadlock_guard` 配置归一化。
- 新增 CPU deadlock_guard；maintenance 在 guard tripped 时直接暂停。
- 修复注册服务 import 即 auto-start 的副作用，避免测试/导入时误触发注册。

验证结果：

```bash
python -m pytest test/test_image_task_service.py test/test_image_tasks_api.py test/test_account_maintenance_loop_service.py test/test_v1_images_generations.py test/test_v1_images_edits_api.py test/test_account_image_capabilities.py test/test_account_refresh_all_service.py test/test_json_storage.py test/test_image_storage_service.py test/test_multi_image_results.py test/test_register_service_panda_batch.py -q
# 71 passed
```

仍未完成：

- R5.5 真实 100 异步任务压测。
- b64 回传窗口 P7。

生产 Panda 部署状态：

- 已完成生产备份、部署、启动、验收和受控压测。
- 备份目录：`/root/gptimage/backups/p6-image-queue-20260704-175026/`
- 回滚脚本：`/root/gptimage/backups/p6-image-queue-20260704-175026/ROLLBACK.sh`
- 容器资源已调整为 `1.5 vCPU / 1.5GiB`，与 `image_deadlock_guard.cpu_budget_vcpu=1.5` 对齐。
- 12 个 async image task 受控压测：提交全 `200 queued`，最终 `12/12 success`；完成耗时 min `38s`、p50 `202.5s`、p95 近似 `295s`、max `304s`。
- 严格日志检查未发现 `Traceback`、`dictionary changed`、`image service busy`、HTTP `5xx`、`524/502`、`connection reset` 或 `timeout_pending`。
- 生产证据：`/root/gptimage/backups/p6-image-queue-20260704-175026/post-deploy-validation.json`

### P6-8 18 / 24 / 30 受控压测先行

下一步不是直接改 worker，也不是直接做多桶。先按 `docs/performance-acceptance-test-plan.md` 的 R5.6 执行生产受控压测：

```text
18 async tasks -> 冷却复核 -> 24 async tasks -> 冷却复核 -> 30 async tasks
```

关键边界：

- 18 / 24 / 30 指一次性提交的 async task 数量，不等于 worker 数。
- 当前 worker / 队列参数保持不变，除非单轮报告证明需要单独变更实验。
- 每一档独立生成报告；18 不过不进 24，24 不过不进 30。
- 监控口径把“网络”拆成明确的 **带宽**：`bandwidth_rx_mbps`、`bandwidth_tx_mbps`、`bandwidth_total_mbps`。
- 30Mbps 公网带宽按阈值观察：p95 < 18Mbps 正常，18~24Mbps 警戒，>=24Mbps 持续 60s 停止升档，>=28Mbps 连续 15s 停止本档新增提交。
- 不做输入减重；prompt、参考图、mask、多图输入必须保持真实业务形态。
- 输出侧 b64/url 可以统计，但 b64 回传优化归 P7，不混入 R5.6 变量。

R5.6 报告出来前，不给最终并发、RPM、CPU、worker、桶数建议。

### P6-9 后续异步化、多桶 / 多出口设计边界

R5.6 数据出来后，再按瓶颈选择改造方向。

异步化目标：

```text
同步快车道：短等 / 快拒绝，不承接长队和长连接尾流。
异步任务道：持久化状态、可恢复、可观测、后台按窗口释放压力。
窗口拆分：submit window / poll window / download window / b64 return window。
```

多桶定义：

```text
bucket = 独立 scheduler + 独立账号子池 + 独立出口/代理 + 独立熔断器 + 独立指标
```

路由策略：

```text
新任务进入 score 最低的桶：
score = running_weight + queue_wait_weight + recent_p95_weight + error_rate_weight + bandwidth_weight + quota_pressure_weight
```

多出口目标：

- 不靠单一公网出口承接所有上游长连接。
- 每个出口独立记录 reset / 524 / latency / bandwidth。
- 某个出口异常时，只熔断该桶，不拖垮全局。

明确不做：

- 不通过输入减重换吞吐；参考图完整性优先。
- 不把“账号长期质量分层”作为主方案。当前死号太快，号池持续刷新，长期 gold/silver 分层成本高且不稳定。
- 只保留短期运行态：`ready / in_use / limited / invalid / transient_backoff / quarantine`，用于调度避让和减少无效重试。

减少无效重试的可做方向：

- 已拿到 `conversation_id` 后 timeout 不重开图，只进入 `timeout_pending` 续 poll。
- `401/403/token invalid/revoked/quota exhausted` 不重试同号。
- pre-submit 的 transient 网络错误最多 1 次重试，带 jitter。
- 同一 `task_id` 使用 idempotency，防 duplicate upstream submit。
- 对账号和出口分别做短期 circuit breaker，避免坏号/坏出口被反复打。

是否落地上述改造，以 18 / 24 / 30 的数据判定。

### P6-10 18 档压测结果（2026-07-04）

执行方式正确性：

- 本地机器通过公网 `https://gptimage.relai.asia` 发起 18 个并发 async image task。
- Panda 只运行监测脚本，不从本机 `127.0.0.1` 发起压测。
- 输入未减重：6 个文生图，12 个图生图；8 个单参考图、4 个双参考图。
- 总 payload 约 `29.37MB`，参考图原始 PNG 总量约 `22.02MB`。

报告位置：

```text
本地：reports/loadtest-20260704-191600-stage-18/
Panda：/root/gptimage/backups/loadtest-20260704-191600-stage-18/
关键汇总：combined-summary.json
```

结果：

```text
提交：18/18 HTTP 200 queued
最终：18/18 success
timeout_pending：0
error：0
strict bad logs：0
```

性能：

```text
公网提交 p95=49.32s，max=53.97s
公网状态查询 p95=4.88s，max=7.16s
Panda 任务总耗时 p95=109.93s，max=125.42s
Panda CPU p95=13.78%，max=29.52%
Panda 内存 max=526.6MiB
Panda 总带宽 p95=14.56Mbps，max=25.25Mbps，>=24Mbps 最长连续 5s
```

门槛判断：

- 不进入 24。
- 原因不是 Panda CPU / 内存 / 健康页 / 服务器侧带宽打爆。
- 阻塞项是公网大参考图提交延迟和公网状态查询延迟：`submit_p95 < 1s`、`status_query_p95 < 500ms` 均未通过。

下一步讨论点：

- 不能用输入减重解决。
- 需要先判断 R5.6 对“大参考图异步提交”的提交 p95 门槛是否应该拆成：
  - 小 JSON 任务提交 p95。
  - 大参考图上传 p95。
  - 服务端入队写入 p95。
- 如果要继续 24，需要先接受“真实大输入公网提交本身就是几十秒级”的事实，或先做上传链路/状态查询路径优化。

### P6-11 24 档压测结果（2026-07-04）

用户确认真实大参考图上传不再要求 `submit_p95 < 1s` 后，已执行 24 档。

执行方式：

- 本地公网 `https://gptimage.relai.asia` 发起。
- Panda 只监测。
- 输入未减重：8 文生图 + 16 图生图；10 单参考图 + 6 双参考图。
- 总 payload 约 `40.38MB`，参考图原始 PNG 总量约 `30.28MB`。

报告：

```text
本地：reports/loadtest-20260704-194005-stage-24/
Panda：/root/gptimage/backups/loadtest-20260704-194005-stage-24/
关键汇总：combined-summary.json
```

结果：

```text
24 requested
22 accepted / HTTP 200 queued
2 rejected / HTTP 429
accepted final：21 success / 1 error / 0 timeout_pending
strict bad logs：0
```

429 原因：

```text
image task queue is full for current user (20/20)
```

当前配置单用户最多约 `20 queued + 2 running = 22`，所以 24 并发无法完整入队。

唯一任务错误：

```text
'ConfigStore' object has no attribute 'proxy_url'
```

资源：

```text
Panda CPU p95=7.77%，max=31.16%
Panda memory max=594.2MiB
Panda total bandwidth p95=22.31Mbps，max=298.20Mbps
>=24Mbps max consecutive=49.14s
status query p95=6.37s
```

判断：

- 不进入 30。
- CPU/内存不是瓶颈。
- 24 档被当前单用户队列上限、`ConfigStore.proxy_url` 错误和带宽警戒共同卡住。
- 下一步优先修：
  1. per-user queue 上限/语义：如果要测 24/30，需要允许完整入队，或按“22 接纳 + 429 快拒绝”定义为合格行为。
  2. `ConfigStore.proxy_url` 错误。
  3. 状态查询慢和公网带宽突刺。

### P6-12 兼顾体验与 24/30 承载的方案

目标不是让 24/30 同时真实生成，而是让 24/30 能被稳定接纳、状态清晰、后台平滑释放，不让公网大输入、状态查询和生成窗口互相拖垮。

#### 核心原则

```text
接纳容量 != 执行并发
上传压力 != 生成压力
状态查询 != 结果回传
```

当前 24 档失败点：

```text
per_user_queue_max=20 限制 unfinished 总数，2 running 时只能再排 20，总接纳约 22。
ConfigStore.proxy_url 导致 1 个已接纳任务失败。
大参考图公网提交和状态查询慢。
Panda CPU/内存安全，不能据此直接提高生成 worker。
```

#### 第一步：修硬错误

先修 `ConfigStore.proxy_url`：

- `services/image_task_service.py` 续轮询不应访问不存在的 `config.proxy_url`。
- 推荐给 `ConfigStore` 增加兼容属性 `proxy_url`，从 `proxy_runtime.proxy_url` 读取。
- 该错误修完后，至少重跑 18 或小批 timeout/resume 场景，确认不会再因为续轮询失败。

#### 第二步：把队列上限拆成“接纳上限”和“执行上限”

当前 `per_user_queue_max` 实际是 unfinished 上限。为了压住 24/30：

```text
per_user_running_max = 2  # 先不动，保证单用户不挤爆全局体验
per_user_queue_max = 36   # 接纳 30 + 少量余量
global_queue_max = 240~300
submit_workers = 6        # 先不动
submit_workers_max = 8    # 只做后续实验上限
```

这样 24/30 能完整入队，但后台仍按 `per_user_running_max=2` 平滑执行。

如果 30 档完成时间过长，再做第二轮实验：

```text
per_user_running_max = 3
触发条件：CPU p95 < 40%、bandwidth p95 < 18Mbps、5xx/reset=0、timeout_pending<=10%
回退条件：bandwidth p95 >= 18Mbps、status p95 > 5s、错误率 > 3%
```

#### 第三步：大输入上传与任务入队解耦

不能输入减重，但可以改传输方式：

- 图像质量不变。
- 参考图不压缩、不缩小、不删除。
- 优先从 JSON data URL 改为 multipart 文件上传，避免 base64 膨胀。
- 更优方案是“两阶段提交”：

```text
1. upload references -> 得到 asset_id / staged_file_id
2. submit async task -> 只带 prompt + asset_id
```

这样用户看到的是上传进度和入队进度分离；任务入队不再被 30MB~50MB 请求体拖慢。

#### 第四步：状态查询轻量化

当前 `/api/image-tasks?ids=...` 会返回 `data` 等结果字段。压测中公网状态查询 p95 达到数秒，30 档会更差。

新增轻量查询：

```text
GET /api/image-tasks/status?ids=...
```

只返回：

```json
{
  "id": "...",
  "status": "queued|running|success|error|timeout_pending",
  "progress": "...",
  "elapsed_secs": 123,
  "duration_ms": 456,
  "error_code": "...",
  "result_count": 1
}
```

不返回：

```text
data
image urls
b64
payload
reference image
large result fields
```

前端/压测轮询只打轻量接口；只有任务成功且用户打开详情/下载时才取结果。

#### 第五步：体验保护

24/30 入队后，前端不要只显示“等待中 28”。要显示：

```text
已接纳 / 上传中 / 排队中 / 生成中 / 已完成 / 失败
预计开始时间
当前位置
当前运行槽
失败原因
```

服务端返回应包含：

```text
queue_position
estimated_start_after_secs
running_limit
accepted_limit
retry_after
```

当队列超过可接受等待时间，不无限接纳：

```text
per_user_queue_max=36
hard global_queue_max=240~300
estimated_wait > 20~30min 时返回 429/503 + Retry-After
```

#### 第六步：下一轮压测顺序

```text
P6-12.1 修 ConfigStore.proxy_url
P6-12.2 per_user_queue_max 20 -> 36，只提高接纳，不提高 running
P6-12.3 新增轻量 status 接口
P6-12.4 重跑 24
P6-12.5 24 通过后再跑 30
P6-12.6 若 30 完整入队但完成太慢，再测 per_user_running_max=3
```

24 通过标准按新口径：

```text
24/24 accepted
accepted success >= 95%
HTTP 429 = 0
timeout_pending <= 10%
strict_bad_logs = 0
CPU p95 <= 70%
memory < 1.2GiB
bandwidth >=24Mbps 不连续超过 60s
status lightweight p95 < 500ms
```

30 通过标准：

```text
30/30 accepted
HTTP 429 = 0
accepted success >= 95%
完成时间曲线可解释，无无限拖尾
不因状态查询或结果回传拖垮前台
```

---

## P7. b64 回传窗口和公共 API 快拒绝

### canvas.best 处理原则

`https://canvas.best/` 是外部公共调用方，不是内部任务系统。

因此：

- 不做内部无限队列。
- 不返回「等待中 94」这类长队体验。
- 没槽时短等，仍没槽就 429/503 + Retry-After。

### 三个窗口

```text
生成窗口：控制同时向 ChatGPT 生图数量
落地窗口：控制同时下载/保存图片数量
回传窗口：控制同时 b64 encode / 大响应传输数量
```

### b64 窗口建议初值

```text
b64_active_limit = 4~6
b64_bytes_inflight_limit = 32~64MiB
b64_queue_timeout = 5~10s
```

### URL 优先

如果调用方支持：

```json
{"response_format": "url"}
```

优先走 URL，避免 base64 33% 膨胀和大 JSON 响应。

验收：

- b64 回传不会占满生成窗口。
- broken pipe 减少。
- b64 窗口满时公共 API 快拒绝，不无限排队。

---

## P8. 多轮测试和最终验收

详见：`docs/performance-acceptance-test-plan.md`。

最低验收：

1. 单元/回归测试通过。
2. 本地 SQLite 迁移前后账号数和关键字段一致。
3. Panda import-batch 高频写盘消失。
4. `dictionary changed size during iteration` 为 0。
5. 8 并发下 Panda CPU/BlockIO/p95 明显优于当前基线。
6. b64 broken pipe 下降。
7. async task 100 并发提交时，提交接口 p95 < 1s，任务不丢，重复上游提交率 < 1%。
8. CPU p95 不超过 70%；CPU >= 90% 时必须触发 deadlock_guard 熔断，不允许继续升并发。
9. canvas.best 作为公共调用方不会被无限排队拖死。

---

## 止损线

出现以下情况立即暂停并回滚/改路：

- SQLite 迁移后账号数不一致。
- token 字段丢失或账号不可调度。
- Panda import-batch 合法同步被全部拒绝。
- NewAPI 5xx 明显高于改动前。
- b64 窗口导致正常小并发也大量 503。
- maintenance 误删正常号。

## 关联文档

- `docs/07-account-pool-performance-upgrade.md`
- `docs/sync-strategy.md`
- `docs/performance-acceptance-test-plan.md`
- `docs/02-current-state.md`
- `docs/03-roadmap.md`
- `docs/04-improvement-backlog.md`
- `docs/05-ai-maintenance-playbook.md`

## P6-12 执行结果（2026-07-04）

状态：**已部署 Panda；24 档复测未通过；不进入 30**。

已落地：

```text
ConfigStore.proxy_url 修复：完成
per_user_queue_max 20 -> 36：完成
轻量 GET /api/image-tasks/status：完成
submit_workers：仍为 6
per_user_running_max：仍为 2
```

生产证据：

```text
备份：/root/gptimage/backups/p6-queue36-status-20260704-210512/
回滚：/root/gptimage/backups/p6-queue36-status-20260704-210512/ROLLBACK.sh
健康页：healthy=true
配置：per_user_queue_max=36
轻量 status：contains_data_key=false, contains_payload_key=false
```

Stage C 24 复测：

```text
报告：reports/loadtest-20260704-211253-stage-24/summary-partial.json
Panda：/root/gptimage/backups/loadtest-20260704-211253-stage-24/summary-partial.json
requested=24
accepted/stored=19
submit connection failures=5
accepted final=18 success / 1 error
```

结论：

- 旧的 `20/20` queue cap 问题已解决。
- 旧的 `ConfigStore.proxy_url` 问题已解决。
- 24 档仍失败，但失败已经前移到“公网大参考图上传 / 上游 files 上传”。
- 当前不能靠加 worker、加 CPU、扩大队列解决。
- 不进入 30。

下一步改造顺序：

1. 上传链路从 JSON data URL 优先改 multipart。
2. 设计两阶段 reference asset upload：先上传 reference 得到 asset_id，再提交 async task。
3. 增加上传窗口 / 上传并发保护，避免 24/30 个大请求同时穿过 Cloudflare/公网链路。
4. 复测指标拆成：upload_success、queue_accept_success、generation_success。
5. 24 稳定后再讨论 30。

## P6-13 三轮 24 并发复测与 IMG-005 校准（2026-07-05）

### 三轮压测结论

```text
报告：reports/stage24-3rounds-20260705/aggregate-summary.json
Panda：/root/gptimage/backups/stage24-3rounds-20260705/aggregate-summary.json
requested_total=72
submit_ok_total=72
submit_failed_total=0
final_success_total=66
final_error_total=6
submit p95≈21.07s
status p95≈2.76s
cpu p95 max≈13.7%
memory max≈724MiB
bandwidth_total p95 max≈9.2Mbps
```

判断：

- 这三轮未复现入队前大上传断连，72/72 均成功入队。
- Panda CPU、内存、健康页、稳定带宽都不是瓶颈。
- 仍不能进入 30：最终成功率 91.7%，status p95 仍高于 500ms，单轮完成最长约 34 分钟。
- Round 3 的 4 个错误是实现 bug，不是上游或 IMG-005：`OpenAIBackendAPI.__init__() got an unexpected keyword argument 'proxy_url'`。
- 本地已修 BUG-002：`_run_resume_poll()` 使用 `OpenAIBackendAPI()`，测试 14 passed；尚未部署 Panda。
- 剩余 2 个真实上游错误是 pre-conversation HTTP/2 INTERNAL_ERROR，无 `conversation_id`，失败释放过慢。

### IMG-005 最优实现方案

#### 目标拆分

```text
upload_success: 大参考图上传成功率与耗时
queue_accept_success: async task 入队成功率与耗时
generation_success: 上游生图最终成功率与耗时
```

#### 一期：reference asset 两阶段上传

新增资产表 / SQLite：

```text
image_reference_assets(
  asset_id TEXT PRIMARY KEY,
  owner_id TEXT,
  sha256 TEXT,
  mime TEXT,
  filename TEXT,
  bytes INTEGER,
  storage_path TEXT,
  created_ts REAL,
  expires_ts REAL,
  status TEXT
)
```

新增接口：

```text
POST /api/image-assets/references
GET  /api/image-assets/references/{asset_id}/status
DELETE /api/image-assets/references/{asset_id}
```

提交任务时：

```text
POST /api/image-tasks/edits
body: prompt + model + size + quality + asset_ids[]
```

服务端 worker 执行时再从 asset 读 bytes，组装原有 `images`，不改变输出质量。

#### 二期：上传窗口保护

```text
upload_global_concurrency = 4~6
upload_per_user_concurrency = 2~3
upload_max_bytes_inflight = 64~96MiB
asset_max_bytes_each = 按当前业务上限设置
asset_ttl = 2~6h
```

满载行为：

```text
上传窗口满：429/503 + Retry-After
任务队列满：429 + Retry-After
生成窗口满：只排队，不扩大 running
```

#### 三期：前端/压测体验

前端状态拆成：

```text
上传中 -> 上传完成 -> 已入队 -> 排队中 -> 生成中 -> 成功/失败
```

压测指标必须拆开：

```text
upload_latency_ms
asset_commit_latency_ms
queue_submit_latency_ms
status_query_latency_ms
generation_duration_ms
```

#### 四期：pre-conversation 快收敛

针对无 `conversation_id` 的上游 HTTP/2 / reset / internal error：

```text
pre_conversation_hard_timeout = 180~300s
pre_conversation_max_attempts = 1~2
same_account_short_backoff = 5~15min
same_exit_short_backoff = 1~5min（如果未来多出口）
```

注意：拿到 `conversation_id` 后仍不能重开图，继续走 timeout_pending 续轮询。

### 实现后预估

在先部署 BUG-002 的前提下：

```text
24 submit/queue_accept: 24/24 维持稳定
提交接口 p95：从 20~21s 降到 0.5~2s（任务提交本身）
上传 p95：仍取决于总参考图大小和公网，但变为可见上传阶段，不再阻塞入队
status p95：目标 <500ms；当前 2.8s 需要继续查 DB/路由/Cloudflare 延迟
最终成功率：短期可从 91.7% 提升到约 97.2%（修掉 4 个续轮询 bug 后）
剩余失败：主要是 2.8% 左右 pre-conversation HTTP/2 INTERNAL_ERROR，需 IMG-006 快收敛
Panda CPU：预计仍 <20% p95
内存：asset 暂存落盘后保持 <1.0GiB，避免内存堆积
稳定带宽 p95：预计仍 <10~15Mbps，但瞬时尖峰会被上传窗口削平
```

是否进入 30：

```text
先部署 BUG-002 -> 复测 24 一轮
再落地 IMG-005/IMG-006 -> 复测 24 三轮
满足 final_success >=95%、status p95 <500ms、无 24Mbps 持续 60s 后，再测 30
```

## P6-14 IMG-005 一期部署与两阶段 24 压测（2026-07-05）

状态：**IMG-005 一期已部署；上传/入队目标达成；生成侧不达标；不进入 30**。

### 已部署

```text
BUG-002 备份：/root/gptimage/backups/bug002-resume-poll-20260705-162726/
IMG-005 备份：/root/gptimage/backups/img005-assets-phase1-20260705-163634/
```

新增能力：

```text
POST   /api/image-assets/references
GET    /api/image-assets/references/{asset_id}/status
DELETE /api/image-assets/references/{asset_id}
POST   /api/image-tasks/edits 支持 asset_ids[]
```

生产最小验收：

```text
health healthy=true
asset upload 200 ready
asset_ids edit task queued -> success
duration_ms=44805
严格错误日志=0
```

### 两阶段 24 三轮压测

报告：

```text
reports/img005-stage24-3rounds-20260705-164948/aggregate-summary-corrected.json
/root/gptimage/backups/img005-stage24-3rounds-20260705-164948/aggregate-summary-corrected.json
```

口径：

```text
每轮 24：8 文生图 + 16 图生图
图生图先 multipart 上传 reference asset，再用 asset_ids 入队
每轮参考图原始 PNG≈30.28MB
不输入减重
```

结果：

```text
requested_total=72
asset_upload=48/48
submit=72/72
final_success=56
final_error=9
final_unfinished_at_45min=7
asset_upload p95 max≈17.98s
submit p95 max≈2.31s
status p95 max≈2.35s
cpu p95 max≈8.28%
memory max≈750.6MiB
bandwidth_total p95 max≈6.30Mbps
health p95 max≈15.45ms
```

### 结论

IMG-005 一期已经把“大图上传”和“任务入队”拆开：

- 上传成功率：100%。
- 入队成功率：100%。
- submit p95 从约 21s 降到约 2.3s。
- submit payload 降到约 300~418B。
- Panda CPU/内存/稳定带宽不是瓶颈。

但 24 档仍不合格：

- 45 分钟 cutoff 仍有 7 个任务未终态。
- 自然失败主要是上游和账号侧：
  - `HTTP/2 INTERNAL_ERROR`：4 个，无 `conversation_id`，pre-conversation 长尾。
  - `token invalidated during image poll task check`：4 个，有 `conversation_id`，post-conversation 续轮询撞账号失效。
  - `no available image quota`：1 个，当前号池余量不足。
- 本轮压测残留任务已备份并清理：`/root/gptimage/backups/img005-cleanup-unfinished-20260705-190020/`。

### 修正后的最优解

下一阶段不要继续加 worker，也不要压 30。正确顺序：

1. **IMG-006 pre-conversation 快收敛**
   - 在拿到 `conversation_id` 前设置硬超时，例如 `pre_conversation_hard_timeout=240~300s`。
   - `HTTP/2 INTERNAL_ERROR` / reset / remote disconnected 最多 1 次快速重试。
   - 失败账号短 backoff，避免坏号继续占槽。
   - 目标：把无 `conversation_id` 的 30min 长失败压到 3~5min 内释放。

2. **IMG-007 post-conversation poll 策略**
   - 当前 `image_poll_timeout_secs=120` 对 24 并发复杂图偏短。
   - 按任务类型动态 timeout：简单文生图 180s，复杂图/多参考图 240~360s。
   - 已拿到 `conversation_id` 后仍不重开图；但 token invalidated 不能直接判死任务，应刷新/换有效查询凭据后继续查原 conversation。
   - 目标：降低 `token invalidated during image poll task check`。

3. **IMG-005 二期上传窗口**
   - `upload_global_concurrency=4~6`
   - `upload_per_user_concurrency=2~3`
   - `upload_max_bytes_inflight=64~96MiB`
   - asset TTL 2~6h，后台清理。
   - 目标：削平瞬时 80Mbps+ 带宽尖峰，不影响图像质量。

4. **号池水位 / 实际可调度面**
   - `102 total / 470 quota` 纸面足够 72 次请求，不能简单称为“烧穿”。
   - 真正风险来自坏号、过期号、preflight 失败、短期 backoff、`image_token_max_attempts` 候选面过窄、timeout_pending 占槽，以及每任务可能消耗多个候选账号。
   - 24 复测前应同时看 ready 数、verified quota、preflight 成功率、`no available image quota (tried N tokens)` 分布，而不是只看总 quota。

### 实现后预估

```text
24 upload_success：维持 100%
24 queue_accept_success：维持 100%
submit p95：1.5~2.5s（公网 Cloudflare 基线限制）
asset_upload p95：12~20s，启用上传窗口后更平滑但总上传时间可能略长
final_success：从 56/72 cutoff 结果提升到 66~70/72；号池补足后目标 >=95%
unfinished_at_45min：目标从 7 降到 <=2
CPU p95：仍 <15%
内存：<1GiB
bandwidth_total p95：<10Mbps；瞬时 max 由上传窗口控制到更低
```

进入 30 的条件：

```text
24 三轮重新达到：
asset_upload_failed=0
submit_failed=0
final_success>=95%
unfinished_at_45min<=2
HTTP/2 INTERNAL_ERROR 长失败 <=1/72
token invalidated <=1/72
CPU p95 <40%
bandwidth_total p95 <10~15Mbps
```

## P6-15 IMG-006 / IMG-007 / IMG-005 二期部署与 24 三轮复测（2026-07-05）

状态：**已部署 Panda；上传/入队稳定，生成侧不达标；不进入 30**。

部署备份：

```text
/root/gptimage/backups/img006-007-005p2-20260705-194447/
回滚：/root/gptimage/backups/img006-007-005p2-20260705-194447/ROLLBACK.sh
```

已落地：

- IMG-005 二期：reference asset 上传窗口保护、per-user/global/bytes-inflight 限制、asset TTL 清理；默认 `6 global / 3 per-user / 96MiB / TTL 6h`。
- IMG-006：pre-conversation transient 快收敛，`HTTP/2 INTERNAL_ERROR` 等无 `conversation_id` 错误最多 2 次尝试，单次上游 SSE timeout 默认 240s。
- IMG-007：动态 poll timeout，文生图 180s、单参考/普通 edit 300s、多参考 360s；timeout_pending 续轮询默认 300s、最多 4 次；token invalidated 时尝试刷新同账号 token 后继续 poll 原 conversation。

本地验证：

```text
pytest 目标集：34 passed
扩展回归：93 passed
py_compile：通过
```

生产验收：

```text
health healthy=true
配置生效：timeout_pending=300, generation=180, edit=300, multi_reference=360, pre_conversation=240
部署后 2 分钟异常日志：0
```

三轮 24 压测合并报告：

```text
本地：reports/img006-007-005p2-stage24-3rounds-combined-20260705/aggregate-summary.json
Panda：/root/gptimage/backups/img006-007-005p2-stage24-3rounds-combined-20260705/aggregate-summary.json
```

合并口径：

- Round 1：`reports/img005-stage24-3rounds-20260705-195030/round-01/summary.json`
- Round 2/3：`reports/img005-stage24-2rounds-20260705-205027/round-01..02/summary.json`

结果：

```text
requested_total=72
asset_upload_ok_total=48
asset_upload_failed_total=0
submit_ok_total=72
submit_failed_total=0
final_success_total=45
final_error_total=26
final_unfinished_total=1

asset_upload_p95_ms_max=19078
submit_p95_ms_max=2626
status_query_p95_ms_max=3638
cpu_p95_pct_max=15.014
memory_mib_max=875.4
bandwidth_total_p95_mbps_max=11.089
bandwidth_total_max_mbps_max=98.394
strict_bad_count_60m_max=0
```

错误分布：

```text
no available image quota (tried 8 tokens): 23
image poll timeout 300s: 2
image poll timeout 360s: 1
```

判断：

- 上传窗口没有引入失败；`48/48` reference asset 上传成功。
- 两阶段入队稳定；`72/72` submit 成功，submit p95 约 2.6s。
- Panda CPU、内存、稳定带宽都不是瓶颈。
- IMG-007 生效：日志可见 generation=180s、edit=300s、多参考=360s；timeout_pending 被 poll worker 接管。
- IMG-006 生效：日志可见 `image_pre_conversation_transient_retry`，HTTP/2 INTERNAL_ERROR 不再 30min 无界占槽。
- 生成侧仍不达标，主因变为生产配置 `image_token_max_attempts=8` 导致每任务候选面过窄；当前号池虽有正 quota，但每任务最多抽 8 个 token，8 个 preflight/限流/不可调度即报 `no available image quota`。

下一步：

1. 不进入 30。
2. 把 `image_token_max_attempts` 从 8 提到 24~32 做单轮 24 A/B；同时观察 preflight 请求量和账号状态变化。
3. 若仍 `no available image quota` 高，继续查 `fetch_remote_info` 失败原因分布，而不是继续加 worker。
4. 再考虑把 `per_user_running_max` 从 2 调到 3；前提是 `no available` 明显下降，否则只会更快抽干候选面。

## 2026-07-06 状态更新：BUG-003 / IMG-008

- BUG-003 已完成：ImageTaskService 构造/导入不再恢复未完成任务，避免只读检查污染生产任务 DB；恢复动作改到 runtime start。
- IMG-008 第一档已完成：Panda image_token_max_attempts=24 单轮 24 压测上传/入队全成功，最终 23/24，唯一失败为 300s 生图超时，
o available image quota=0。
- 当前结论：候选面过窄问题已被 24 修正；下一瓶颈是上游长尾、状态查询 p95 约 3s、账号池水位消耗。
- 下一步：不进 30；补号/水位恢复后再 A/B per_user_running_max 2 -> 3，暂不升 image_token_max_attempts=32。
