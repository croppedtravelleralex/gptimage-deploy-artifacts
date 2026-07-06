# 改进池（精简版）

最后校准：2026-07-06 16:55
原则：只记录**当前要做**、**已完成**、**已确认暂缓/不做**；过时条目直接删除，不保留「待处理」僵尸项。

---

## 当前主线

### IMG-012 NewAPI 同步入口内部异步化

- **状态**：**已本地实现 + Panda 已部署 + 已 restart**；busy_6 验收通过；24 路全成功与三轮 72 路**未通过**
- **方案**：`docs/08-image-pipeline-newapi-async-plan.md`
- **目标**：`/v1/images/*` 改为内部 `submit image_task + wait_for_result`，解除同步入口 `global concurrency limit 6` 快拒；后台默认 6 上游生成槽
- **已验收**：
  - `global concurrency limit 6 reached`：**0 条**（NewAPI 24 路压测 + Panda 近 15min 日志）
  - Panda 容器 **16:41** restart 后新代码已加载（`skip_global_limit`、`run_generation_sync`）
- **未验收 / 当前阻塞**：
  - NewAPI 24 路单轮：**5/24 成功**（`reports/img012-newapi-sync-stage24-1rounds-20260706-164210/`）
  - 19 失败均为 **NewAPI/传输层**（`JSONDecodeError` 空响应 8 + HTTP/2 `ConnectionTerminated` 11），非 Panda busy
  - 三轮 `70/72`、`unfinished=0` 未跑
  - `burst_enabled=false`，动态 burst 8 未启用
- **下一步**：
  1. 排查 NewAPI 网关 24 长连接并发稳定性（HTTP/2 断连、外层超时 < 540s）
  2. 压测脚本可试 HTTP/1.1 或降低 `IMG012_SUBMIT_WINDOW` 做对照
  3. busy_6 稳定后启用 burst 8（`scripts/img012_enable_burst_deploy.py`）
  4. 补 3 轮 NewAPI 24 混合压测

---

## 已完成（代码已落地）

### 账号池与存储（plan.md P0–P4）

| ID | 内容 | 证据 |
|---|---|---|
| PERF-001 | 账号池并发读写 snapshot | `account_service._list_ready_candidate_tokens` 锁内 snapshot |
| STORE-001 | 本地 SQLite 主存储 | `storage/factory.py` 默认 `STORAGE_BACKEND=sqlite` |
| STORE-002 | 行级 upsert/delete | `database_storage.upsert_accounts` + `_persist_upsert_accounts` |
| SYNC-001 | Panda 水位同步 | `panda_staging_service` + `high=1500/low=500` |
| SYNC-002 | import-batch 限频保护 | `api/accounts.py` batch cap + min_interval + 互斥锁 |
| ACC-005 | 新号成熟度探活 | `PandaStagingService` 三档 probe schedule |
| ACC-001 | 额度三态修复 | 2026-06-29 部署 Panda，`docs/quota-semantics.md` |

### IMG-012 sync-over-async（2026-07-06）

| 切片 | 内容 | 状态 |
|---|---|---|
| IMG-012A | 配置骨架（`newapi_image_sync_*`、`per_user_running_base/burst`） | ✅ 本地 + Panda config |
| IMG-012B | `ImageTaskService.wait_for_result()` | ✅ |
| IMG-012C | `api/ai.py` 非 stream `/v1/images/*` → sync-over-async | ✅ Panda 已部署 |
| IMG-012C+ | `queue_coordinated` → `skip_global_limit` 绕过全局 6 | ✅ |
| IMG-012D | 动态 6/burst8 调度 | ⏸ 骨架在代码，生产 `burst_enabled=false` |
| IMG-012E | 下载/回传窗口拆分 | ⏸ `image_return_window_size=3` 已配，完整流水线未拆 |
| IMG-012F | NewAPI 24 压测 | ⚠️ busy_6=0 通过；5/24 成功未通过 |

关键文件：`services/image_sync_adapter.py`、`services/image_task_service.py`、`api/ai.py`、`services/account_service.py`（`skip_global_limit`）、`scripts/img012_*.py`

Panda 备份：`/root/gptimage/backups/img012-sync-over-async-20260706-*`

### 生图队列与容量（IMG-002 ~ IMG-011）

| ID | 内容 | 备注 |
|---|---|---|
| IMG-002 | 异步 SQLite 中央队列 | 已部署 Panda |
| IMG-003 | poll timeout 不重开图 | 已部署 Panda |
| IMG-005 | 两阶段 reference asset 上传 | 一期+二期已部署 |
| IMG-006 | pre-conversation 快收敛 | 已部署，持续观察 |
| IMG-007 | post-conversation poll 策略 | 已部署，持续观察 |
| IMG-008 | `image_token_max_attempts` 8→24 | 第一档已验证，暂不升 32 |
| IMG-009 | health 真实候选指标 | 已部署 Panda |
| IMG-011 | hard timeout | 3×24 达 70/72 |
| BUG-001~003 | proxy_url / resume_poll / import 副作用 | 均已修复部署 |
| OPS-005 | CPU 1.5 vCPU + deadlock_guard | 已部署 |
| MAINT-002 | 生图期间 maintenance slow | 已部署配置 |
| REG-001 | TempMail.lol 限速 | 本地已验收 |
| OPS-006 | 40080/WSL 代理栈 | 主注册链路已修复 |

---

## 已确认暂缓（非当前主线）

| 项 | 原因 |
|---|---|
| 直接进 30 并发 | 24 档未稳定达标，明确不做 |
| `image_token_max_attempts` 升 32 | 24 档 A/B 后账号池消耗过快，暂不升 |
| `per_user_running_max` 2→3 A/B | 需等账号池水位恢复后再测 |
| SYNC-002 HMAC/nonce 全量方案 | Bearer + 限频已够用，HMAC 非阻塞项 |
| 进程级 kill 上游 I/O | IMG-011 hard timeout 已止血，根治方案留后续 |
| R5.5 真实 100 任务压测 | 非 IMG-012 前置，IMG-012 验收后补 |

### 运维与文档（低优先级，有空再做）

- OPS-001 健康页定期检查/告警
- OPS-002 Panda auth key 专项连通性检查
- ACC-002/003/004 验号分类、同步重试观测、死号口径统一
- DOC-001/002/003 文档分层与月度日志

---

## 执行顺序（当前）

1. **NewAPI 网关稳定性**：修 HTTP/2 断连 / 外层超时，再重跑 24 路（目标 ≥23/24）
2. **3 轮 72 路验收**（`IMG012_ROUNDS=3`）
3. **burst 8 条件升档**（IMG-012D，`burst_enabled=true`）
4. 水位恢复后 `per_user_running_max` 2→3 A/B
5. `stream=true` 的 `/v1/images/*` 是否也接 sync-over-async（当前仍走旧直连接口）

## 已知限制（IMG-012 未覆盖）

| 项 | 说明 |
|---|---|
| `stream=true` | 仍走 `openai_v1_image_*.handle` 直连接口，受全局 6 限制 |
| 部署后须 restart | `docker compose up -d` 不 reload Python；改代码后必须 `restart` |
| NewAPI 可见耗时 | sync-over-async 长连接含排队；24 路下单请求 110–300s 为预期排队叠加，非单次上游纯执行时间 |
| 外层超时 | NewAPI/Cloudflare 若 < 540s，仍可能出现 524/断连（本次 19 失败主因） |

## 2026-07-06 IMG-012 当前修正结论

- `asset_ids` 直传 NewAPI 不可行；已改为 `panda-asset://<asset_id>` pointer file 兼容层。
- Panda 已验证 6/12 NewAPI 同步成功；24 同步失败主因是 NewAPI/closeapi/Cloudflare 约 175~210s 外层超时。
- 当前主线应从“继续优化 Panda 同步等待”改为“NewAPI 侧异步 task/callback 适配，或同步入口 admission 限流”。
- 已部署 `resume_polling` hard-timeout 止血；后续仍需做 slot 泄漏自愈指标。
