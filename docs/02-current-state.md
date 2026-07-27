# 当前状态

最后更新：**2026-07-27 09:30**（Asia/Shanghai）
来源：`29-prod-provenance-audit-20260726.md`（溯源审计）+ 5 路架构分析（slots/proxy/quota/monitor/deploy）

> 下方为本次整体更新后的权威事实。历史流水已归档 `docs/logs/2026/2026-07.md` 与 `docs/archive/`。

---

## 一号池（Panda 生产，2026-07-26 22:40 实测）

| 指标 | 值 | 口径 |
|------|----|------|
| 账号总数 | **19** | `total`（`accounts.db` 实有 19 行） |
| 正常/异常 | 18 / 1 | `philliphicks336926` quota=0 status=异常 |
| 进调度 / 生图可用 | **17** / **17** | 扣 `philliphicks`（status）+ `enricoalfred9264`（identity_isolated） |
| proxy_cf_ok | **19/19** | 全绿 |
| proxy_binding | **16 个 unique hash**（3 个 hash 各被 2 账号共享） | `proxy_binding_max_accounts=2` |
| 有效 account_concurrency | **2** | prod `image_account_concurrency=2` |
| binding_inflight_max | **1**（AUDIT-28 A4-3 已修：`exclude_token` 语义） | 同 binding 其他账号占用 ≤1 时放行 |
| 总额度 / 可用额度 | 430 / **405** | 17×25 + 1×5 + 1×0 |
| quota 新鲜度 | `latest_quota_refresh_at = 2026-07-26 08:38Z` | ~14h 前；`image_quota_freshness_hours` 未强制 |
| inflight / drift | **0** / **0** | 全空载 |

### 调度面分解

| 指标 | 值 | 说明 |
|------|----|------|
| `schedulable` | 17 | 不含 warmup block（该字段只扣 status） |
| `ready_candidate_count` | 17 | **含** warmup block（当前 0 blocked） |
| `available_candidate_count` | 17 | 扣 quota=0 / unknown / cooldown |
| `dispatchable_candidate_count` | 17 | 扣 inflight / preflight_backoff |

`get_schedulable_breakdown()` 当前**没有 warmup_block 桶**（arch-quota §4 发现），被 warmup blocked 的账号在 breakdown 里显示为 `primary_reason="ok"`。CF 封禁时间由 86400s 降至 3600s（AUDIT-28 A2-1）且可自愈，但**可观测性缺口仍在**。

---

## 二号池（槽位 / 调度 / 队列）

权威源：`arch-slots` 完整架构报告（2026-07-27 00:15）

### 17 道闸门（HTTP 准入 → 图片字节返回，按序）

| # | 闸门 | 配置键 | 生效值 | 文件:行 | 作用域 |
|---|------|--------|--------|---------|--------|
| 1 | 暂停锁 | `image_generation_paused` | `false` | `image_task_service.py:184-191` | 全局 |
| 2 | 池已枯竭 | `image_pipeline` min_count=2 | ≥2 | `guards.py:7-23` | 全局 |
| 3 | 全局队列上限 | `global_queue_max` | 200 | `image_task_service.py:1346-1351` | 全局 |
| 4 | 每用户队列上限 | `per_user_queue_max` | 36 | `image_task_service.py:1353-1362` | 每 owner |
| 5 | 提示词去重 | `prompt_dedup_max_parallel` | 4 | `image_task_service.py:875-908` | 每 owner+hash |
| 6 | 提交间隔 | `submit_start_min_interval_ms` | 1500ms + 抖动 + 泊松 | `image_task_service.py:548-581,1578-1581` | 全局 |
| 7 | 每用户运行上限 | `per_user_running_base`/`_max`/`_burst` | base=6, relaxed→sse_slots=10 | `image_task_service.py:1520-1545` | 每 owner |
| 8 | 管线准入 `_in_flight` | `global_queue_max`（管线） | 200 | `orchestrator.py:100; pools.py:186-190` | 全局 |
| 9 | 上传信号量 | `asset_upload_concurrency` | 8 | `orchestrator.py:334-346` | 仅编辑/参考图 |
| 10 | pS 槽位池 | `prompt_slots` | 10 | `orchestrator.py:355-366` | 仅 prompt_enhance |
| 11 | ready_buffer 背压 | max 512MB / 32 项 | 512MB/32 | `ready_buffer.py:62-71; orchestrator.py:380` | 全局 |
| 12 | 账号资格 | `image_account_concurrency=2`, `binding_inflight_max=1` | conc=2, binding=1 | `account_service.py:871-896` | 每 account + 每 binding |
| 13 | CF 预热阻塞 | warmup 连胜衰败 | `cf_fail_max_streak=2`→3600s | `account_service.py:789-796,2190` | 每 account |
| 14 | **sS 槽位池** | `sse_slots` | **10** | `orchestrator.py:377-417` | 全局 |
| 15 | sS 阶段墙超时 | `ss_stage_wall_timeout_secs` | 75s（已限定 SSE 流阶段） | `orchestrator.py:214-237` | 每 image_index |
| 16 | 下载信号量 | `download_concurrency` | 8 | `orchestrator.py:451-466` | 全局 |
| 17 | 冷却间隔 | base=60s + jitter(0.65,1.45) + Poisson(8) → 期望~71s | 派发时打戳（与执行重叠） | `account_service.py:593-642` | 每 account |

**架构决策**: 账号槽(#12)在 sS 槽(#14)**之前**获取。管道: 上传(#9)→pS(#10)→账号获取(#12)→sS(#14)→下载(#16)。仅编辑图经过 #9，仅 `prompt_enhance=true` 经过 #10。

### 10 并发数学（AUDIT-28 修复后）

给定可派发 17 个账号、16 个 distinct binding、`seats_excluding_self` 语义（A4-3 已修）：

| 请求 | 账号状态 | 累计 |
|------|---------|------|
| 1–7 | 7 个不同账号各 1 inflight（不同 binding） | sS 7/10 |
| 8–9 | 两个账号各升至 conc=2（binding 无冲突） | sS 9/10 |
| 10 | 第三个账号升至 conc=2 | **sS 10/10** ← 第一个饱和点 |
| 11+ | 在 sS FIFO 队列中排队 | 等待 `image_pool_acquire_timeout_secs` 超时 |

**第一个限制闸门 = sS 池（#14，硬上限 10）。** 第二个是 CF 预热阻塞（曾被降到 7，当前 0）。第三是下载并发 8（交错完成时不成瓶颈）。冷却 ~71s 期望值在派发时打戳与执行重叠，净停滞 ~11s。

### 氧化（Rust .so）定位

| 项 | 事实 |
|----|------|
| 加载路径 | `/app/native/libimage_schedule_core.so` + `libimage_schedule_trace.so` |
| 大小 | 496KB / 530KB |
| 构建方式 | `scripts/build_schedule_trace_linux.py`（WSL cargo 优先，Docker `rust:1-bookworm` 回退） |
| 运行时角色 | **声影镜像**，非真相来源。Python `account_service._image_inflight` + `PipelinePools.ss._slot_holders` 是唯一的真相来源 |
| 加载失败时 | Python `_PySlotLedger` 回退（功能等效）；加载失败**不再静默**（AUDIT-28 A0-2） |
| 值在哪 | `parking_lot::Mutex` 保证租赁簿记不与 GIL 竞争；避免 `_PySlotLedger.watchdog_tick` 不可重入自死锁（AUDIT-28 B3） |

---

## 三号池（代理）

权威源：`arch-proxy` 完整报告（2026-07-27 00:30）

### 数据模型

| 字段 | 位置 | 含义 |
|------|------|------|
| `proxy` | account dict | 完整代理 URL（`http://user:pass@host:port`） |
| `proxy_binding_hash` | account dict / `proxy_binding_hash()` | 代理 host:port+凭据 的稳定 hash |
| `proxy_egress_ip` | account dict | 最近一次 CF 探测实测出口 IP（`probe_proxy_cf` 写入） |
| `proxy_cf_ok` | account dict | True/False，账号级 CF 缓存（优先于 bulk scan 隔离） |
| `proxy_cf_classification` | account dict | CF 分类字符串（`cf_daily` 灯的依据） |
| `proxy_pool` | **新增**（`services/proxy_pool_service.py`） | `"residential"` / `"datacenter"` |

### 新双池

| 池 | 节点数 | 类型 | 认证 | 带宽 | 优先级 |
|----|--------|------|------|------|--------|
| **住宅** | **20** | Webshare `staticresidential` | IP 白名单 43.156.233.219 + user:pass | 无限 | 生图首选 |
| **机房** | **100** | Webshare 升级数据中心 | user:pass | — | 生图回退 / text+warmup |

文件位置：
- 住宅: `data/runlogs/webshare_residential_proxies.secret.txt`
- 机房: `data/runlogs/webshare_100_proxies.secret.txt`
- 均已 gitignore（`data/runlogs/*.secret.txt`）

### CF 准入链（`proxy_cf_eligibility.py`）

```
1. require_cf_ok_for_image() == False → 放行（跳过检查）
2. account_cf_cache_ok() → 放行（账号缓存短路，优先于 bulk 隔离）
3. is_gpt_unavailable_proxy() → 拒绝
4. scan_verdict(endpoint) == True → 放行
5. scan_verdict(endpoint) == False → 拒绝
6. block_unscanned_for_schedule() && !allow_live_probe → 拒绝   ← 当前生效
7. allow_live_probe && probe_on_assign() → 活体探测
8. 默认拒绝
```

**⚠ 当前生产有后效**（§7 of `29`）：`webshare_cf_scan.enabled=false` + `block_unscanned_for_schedule=true`。现有 19 个账号靠步骤 2（`proxy_cf_ok` 缓存）短路放行不受影响，但**任何新加入的未扫描 endpoint 会被步骤 6 直接挡住**。引入住宅代理池前需确认活体探测通路（步骤 7）已接线。

### 绑定容量

| 配置键 | 值 | 语义 |
|--------|----|------|
| `proxy_binding_max_accounts` | 2 | 同 endpoint 最多 2 个账号 |
| `image_binding_inflight_max` | 1（A4-3 已修） | 同 binding 上**其他账号占用 ≤ 1** 时放行；与 `image_account_concurrency=2` 相乘得单出口 2 路并发 |
| `_active_proxy_egress_duplicate` | — | 换绑时按 egress IP 去重 |

绑定分配 16 个 distinct hash 覆盖 19 个账号，住宅 20 节点覆盖 19 个账号（每节点 ≤1 账号，余 1 空闲）。

---

## 四号池（额度）

权威源：`arch-quota` 完整报告（2026-07-27 00:45）

### 生命周期

| # | 转换 | 触发 | 效果 | 文件:行 |
|---|------|------|------|---------|
| Q1 | 上游读取 | `get_user_info()` → `limits_progress` | `image_gen.remaining` → `quota`; `reset_after` → `restore_at` | `openai_backend_api.py:774-785,909-936` |
| Q2 | 入库正规化 | `_normalize_account()` | 重推 quota+restore_at；清 `image_quota_unknown`。Pro/ProLite 跳过 | `account_service.py:1330-1347` |
| Q2b | 限流救治 | `_heal_hard_quota_limited_status` | `status=限流`→`正常`+`image_soft_capped` | `account_service.py:423-461` |
| Q3 | 本地递减 | `mark_image_result(success=True)` | `quota -= 1`，镜像写回 `limits_progress` | `account_service.py:3683-3710,3736` |
| Q4 | 碰零 | 同分支 `quota==0` | `image_soft_capped=True`（仅当 `restore_at` 可解析） | `account_service.py:3737-3753` |
| Q6 | 新鲜度戳 | `_record_refresh_success` | `last_quota_refresh_at = now` | `account_service.py:3626,3860` |
| Q7 | 懒刷新入池 | `quota==0` + `now >= restore_at + jitter` | 重进候选池，下次 acquire 强制 `fetch_remote_info` | `account_service.py:404-420` |
| Q9 | 失效 | 任何失败证据字段 | 从调度移除直到成功刷新清除 | `account_service.py:1073-1094` |

### 陈旧度窗口

| 站点 | 陈旧度 | 文件:行 | 风险 |
|------|--------|---------|------|
| `_is_image_account_schedulable` | `image_quota_freshness_hours=12` 不强制（需 `_image_quota_freshness_required()`） | `:1036-1042` | **未强制时无上限** |
| `_list_ready_candidate_tokens` | 同上 | `:2200-2206` | 陈旧候选入池 |
| `_can_skip_image_preflight` | `image_preflight_min_interval_sec=120` | `:1162-1182` | 跳过上限的仅 120s，新鲜度依赖无上限 |
| `available_image_quota_for_account` | `refreshed_at is not None` 守卫 | `:373-375` | **从未刷新过的账号报告全量陈旧数** |
| sticky preferred_email 路径 | **完全省略** lazy-refresh 检查 | `:2544-2548` vs `:2600` | lazy-due 账号跳过强制刷新 |

### Q6b 陷阱（`account_service.py:3340-3343`）

`update_account()` 在触碰 `quota`/`limits_progress`/`image_quota_unknown`/`restore_at`/`status` 时**自动戳 `last_quota_refresh_at=now`**，即使是本地写入而非上游获取。这使得 "fresh" 信号不可证伪。

### 新配额刷新服务（THROUGHPUT-10）

`services/image_quota_refresh_service.py`：
- 后台线程，60s loop
- `schedule_refresh(token)` 事件驱动 + 去重
- `/health` 暴露 `quota_refresh` 段（`pending_count` / `totals` / `last_ok_at` / `last_error`）
- `api/app.py` lifespan 已接线

---

## 五号池（探测与暖号）

权威源：`arch-quota` §3–§6

### 探测大盘

| 探测器 | 探测内容 | 间隔 | 并发 | 每次成本 | 后台？ |
|--------|---------|------|------|---------|--------|
| **account_warmup** | 每号 CF/auth（`depth=requirements`） | 60s tick | 串行，≤10 hot + ≤2 reprobe | 2+ 上游 HTTPS × 每号 | 是 |
| ↳ hot 刷新 | 已暖号重刷新 | ≥300s | — | — | — |
| ↳ CF 重探 | 被封号 | 300s / ≤2/tick | 2 | 同上 | 是（AUDIT-28 新增自愈） |
| **proxy CF probe** | 出口 IP + GET / + prepare | 按需 | 调用方控制 | 3 请求，45s | 否（库函数） |
| **webshare CF scan** | 批量代理 CF 探测 | 3600–14400s；**enabled=false** | workers=4 | 20×3 | 否（已关停） |
| **image preflight** | `/me`+`/conversation/init`+`/accounts/check` | 懒加载，120s 跳过 | 1/attempt | 3 上游请求 | 否（inline） |
| **pipeline watchdog** | 内部槽/对账 — **零上游流量** | 30s | 1 | CPU only | 是（AUDIT-28 改为独立线程） |

warmup 稳态上游负载: ~4 探测/min ≈ 8 上游请求/min（17 账号 / 10 hot / ≥300s 刷新间隔）。

### 扩容天花板

| 资源 | 当前 | 安全上限 | 原因 |
|------|------|---------|------|
| CF 面（每代理 IP） | ~8 上游 req/min 池级 | **≤1 探测/账号/300s** | `cf_fail_max_streak=2` 低于此阈值下封禁线性增长 |
| CPU | warmup tick 串行 | ≤8 并发探测 | 每探测是 `curl_cffi` 真 TLS 指纹 CPU |
| 内存 | 每探测约 16MB | ≤8 并发 ≈ 128MB | 与图片 base64 blob 共享 1.5GB |
| max_hot | 10 of 17 | ≤60% 池（保持 10） | 提升到 17 则每号持续 sentinel 流量 |
| cf_reprobe_max_per_tick | 2 | ≤3 | 目标节点已有 CF 压力 |

**高杠杆变更（≤10 行）**: 补 `excluded_by_warmup_block` bucket 到 `get_schedulable_breakdown()`（A2 可观测性缺口）。A1-6 补完 `resolve_binding_matrix` hash fallback。

---

## 六号池（带宽与可观测性）

### 每图字节流量

| 阶段 | 每图字节 | 并发数 | 文件:行 |
|------|---------|--------|---------|
| SSE 流 | ~0（仅元数据/URL） | 10（sS 池） | 流处理 |
| 下载 | **~2.5MB** | 8（下载信号量） | `orchestrator.py:451-466` |
| 存储写入 | ~2.5MB | 序列化 | SQLite json blob |
| 客户端响应 | ~3.3MB（base64 膨胀） | 序列化 | 轮询 |

**每图总字节**: ~5.8-8.3MB（启用存储）。10 concurrent、每图 60s 壁钟 → 持续 ~0.97 MB/s（7.7 Mbps），在 100Mbps 链路内。峰值（8 同时下载）~53 Mbps。

### 新带宽追踪器（THROUGHPUT-10）

`services/bandwidth_tracker.py`:
- 滚动 72h 窗口（`deque[tuple[ts, bytes]]`）
- `record_bytes(n)` / `snapshot()` → `last_24h_bytes` / `last_5m_bytes` / `current_mbps`
- `/health` 暴露 `bandwidth` 段

### 管线阶段耗时（历史 PROD conc10 数据）

| 阶段 | serial10 占墙钟 | conc10 占墙钟 | 说明 |
|------|----------------|-------------|------|
| account_queue | 6% | 0.1% | lease 预热已落地 |
| sS queue | 0% | 8.6%（仅 2/10 任务 ~25s） | conc10 时槽位排队 |
| SSE（含排队+流+轮询+下载） | **79%** | **~78%** | **占比最大的阶段** |
| task_queue | — | 9.7% | conc10 新增 |

---

## 七号池（部署链路）

权威源：`arch-deploy` 完整报告（2026-07-27 00:40）

### Panda 实际代码送达路径

Panda `/root/gptimage` 容器 `chatgpt2api-local` **100% 走 bind mount**（`compose` 挂载 `./api ./services ./utils ./scripts ./native ./web_dist` 为 `:ro`）：

```
本地改码 → git push → Panda git fetch → path-scoped checkout → docker restart
```

**本次 AUDIT-28 用的正是这条路径**（非 `git pull`，非 `scp`），已验证可行。

### 为什么 `git pull` 不能直接用

Panda HEAD 分支 `pre-audit28-snapshot-20260726` **无 upstream**（本地临时分支）。`origin` 指向 `basketikun/chatgpt2api`，本地 `deploy` 指向 `croppedtravelleralex/gptimage-deploy-artifacts`。两条历史根 commit 不同（`git merge-base` 返回空），不可合并。

### 本地远程仓库布局

| Remote | URL | 角色 |
|--------|-----|------|
| `deploy` | `github.com/croppedtravelleralex/gptimage-deploy-artifacts.git` | **唯一已配远程**：两个用法并存——`deploy/main` 是 1 文件 wiped 分支（发布后清空），`deploy/audit28-remediation` 是 668 文件全树（真实源码） |
| `upstream` | 残 refs | `v1.5.0` / `v1.6.0` 过期引用 |
| `origin` | **本地未配**（Panda 有 `basketikun/chatgpt2api`） | — |

### 本地分支状态

```
codex/img016-async-admission-hard-timeout  [ahead 22, behind 5] of deploy/main
```

`ahead 22`: 本地 22 个 commit 未推（含 AUDIT-28 `9b15453`）。`behind 5`: wipe commit。
`deploy/main` 只有 README.md —— 不是正常的上游。

### Panda 上两个风险

1. **AUDIT-28 悬在 index**：28 个文件 staged-not-committed。HEAD `2474d48` 还是旧代码。一次 `git reset --hard` 静默回滚全部 11864 行。
2. **19 个文件不在任何 commit 里**：16 个与本地未提交工作树相同（在 GitHub 上零副本），`domain_intel.py` 505 行孤儿死代码，`yumail_otp.py` 落后于已提交修复（缺西语 OTP 关键词），1 个 `.bak`。

### 两项非 Python 构建

| 构建物 | 本地构建 | 是否入 Git |
|--------|---------|-----------|
| `native/libimage_schedule_core.so` | WSL `cargo build --release` | **是**（496KB） |
| `native/libimage_schedule_trace.so` | 同上 | **是**（530KB） |
| `web_dist/` | `npm run build`（`scripts/build_static_frontend.ps1`） | `.gitignore` 当前屏蔽。57/152 个文件从未入 git |

### 部署操作手册（合规链路）

**Phase 0 — Panda 上封存漂移**（最高优先：防止 `git reset --hard` 静默回滚）

```bash
# Panda 本地 commit（不推）
ssh panda 'cd /root/gptimage && git commit -m "chore: land AUDIT-28 index (28 paths)"'
ssh panda 'cd /root/gptimage && git add -A && git commit -m "chore: capture prod-only drift"'
# 取证推送（防硬盘损坏）
ssh panda 'cd /root/gptimage && git push deploy HEAD:refs/heads/panda-drift-rescue-20260726'
```

**Phase 1 — 本地编译**

```bash
# Rust .so（在 WSL HermesUbuntu 内）
python scripts/build_schedule_trace_linux.py --target linux

# 前端
powershell -File scripts/build_static_frontend.ps1
```

**Phase 2 — 本地提交推送**

```bash
git add native/ api/ services/ utils/ scripts/ web/ web_dist/ docs/ test/
git commit -m "feat: <change>"
git push deploy HEAD:refs/heads/<branch-name>   # 推到全树分支，不要推 deploy/main
```

**Phase 3 — Panda 拉取**

```bash
ssh panda 'cd /root/gptimage && git fetch deploy <branch> && git checkout FETCH_HEAD -- <paths>'
# 完成后必须 record（否则 HEAD 再次过时）
ssh panda 'cd /root/gptimage && git commit -m "deploy: <branch> @ $(git rev-parse --short HEAD)"'
```

**Phase 4 — 重启**

```bash
ssh panda 'docker restart chatgpt2api-local gptimage-gateway-rs-helper'
# 禁止 docker compose up（compose.yml 含 build: 键，会在 Panda 编译）
```

**Phase 5 — 验证**

```bash
ssh panda 'sleep 8 && curl -sS "http://127.0.0.1:8012/health?format=json" | python3 -m json.tool | head -15'
ssh panda 'docker exec chatgpt2api-local /app/.venv/bin/python3 -c \
  "from services.image_pipeline.slot_ledger import slot_ledger; print(slot_ledger.stats())"'
# 确认 slot_ledger backend=rust, rust_load_error=null
```

---

## 八号池（测试基准）

| 范围 | 结果 | 日期 |
|------|------|------|
| AUDIT-28 9 回归套件（201 tests） | **全部通过** | 2026-07-27 00:42 |
| 全量 pytest（826 tests） | 58 failed — 全部 `ConnectionRefusedError`（需本地 localhost:8000 起服务），非代码缺陷 | 2026-07-26 |
| THROUGHPUT-10 新增代码 | import 通过 + `ast.parse` 通过 | 2026-07-27 |

---

## 九号池（已知风险与缺口）

| # | 风险 | 严重度 | 出处 |
|---|------|--------|------|
| 1 | AUDIT-28 11864 行悬在 Panda index — 一次 `reset --hard` 静默回滚 | CRITICAL | `29` §2 — **P29-1 部署脚本已含封存 commit** |
| 2 | 16 个活文件在 GitHub 零副本 | CRITICAL | `29` §3.1 — **本次 push 到 `deploy/codex/throughput10-20260727`** |
| 3 | AUDIT-28 零生图流量验证（B1/B2/B4/B9 仍只静态确证） | CRITICAL | **conc10 验收为部署后必跑项** |
| 4 | sS 池 10 硬上限 — 第一个并发闸门 | HIGH | `arch-slots` §2 |
| 5 | `yumail_otp.py` 西语 OTP 已补正则 + pool 默认 subject 放宽 | HIGH | 已修复，随本次部署 |
| 6 | 住宅/机房双池 + `probe_on_assign` 分配时活体探测 | HIGH | `proxy_pool_service` + `account_service` 已接线 |
| 7 | A1-6：`resolve_binding_matrix` hash fallback 限定周末安全预设 | HIGH | 已修复 + 单测 |
| 8 | ~~57/152 `web_dist` 文件 gitignored~~ → **已入 git**（152 文件 + `web_dist-manifest.json`） | RESOLVED | 2026-07-27 |
| 9 | `docker-compose.panda.yml` 含 `build:` 键 → 一次 `compose up` 在 Panda 编译 | HIGH | `arch-deploy` §3 |
| 10 | `get_schedulable_breakdown()` 无 warmup_block 桶 | MEDIUM | `arch-quota` §4 |
| 11 | `update_account` 伪造 `last_quota_refresh_at`（Q6b） | MEDIUM | `arch-quota` §2 |
| 12 | `image_tasks.db` 397MB / 0 rows，磁盘 79% | MEDIUM | `29` §4 |
| 13 | 本地 branch tracks `deploy/main`（1-file wiped 分支） | MEDIUM | `arch-deploy` §2 |
| 14 | 13 个 `_tmp_deploy_*.py` 用 `scp` 直接违背部署铁律 | MEDIUM | `29` §11 |
| 15 | `domain_intel.py` 505 行孤儿死代码仅在 prod 存在 | LOW | `29` §3.2 |

---

## 生产环境

| 项 | 值 |
|----|----|
| SSH 别名 | `panda` |
| 项目路径 | `/root/gptimage` |
| 容器名 | `chatgpt2api-local` |
| 辅助容器 | `gptimage-gateway-rs-helper`（运行 protocol_bridge.py，共享 bind mount） |
| Compose 文件 | `/root/gptimage/docker-compose.panda.yml` |
| 对外端口 | `8012 -> container 80` |
| 容器资源 | ~1.5 vCPU / 1.5GiB（`cpu.max=150000 100000`） |
| 代码送达 | bind mount（`:ro`）→ 改文件 + 重启即生效 |
| 公网域名 | `https://gptimage.relai.asia` |
| 镜像 | `chatgpt2api:local`（2026-06-19 构建，仅 Python runtime + site-packages；业务代码全走挂载） |

## 代码事实

- 默认存储: `STORAGE_BACKEND=sqlite`
- 账号库: `data/accounts.db`（WAL/NORMAL/MEMORY/busy_timeout=5000）
- 生图任务库: `data/image_tasks.db`（WAL/NORMAL）
- 图片资源库: `data/image_reference_assets.db`（WAL/NORMAL）
- 未启 SQLite mmap，无统一 `cache_size` checkpoint 策略

### Panda 同步

- `panda_sync.queue_on_failure=false`
- `delete_invalid=false`（强制，请求传 true 也会被降级）
- `/api/accounts/sync/panda` → `account_refresh_all_service.queue_available_accounts_for_panda()`
- `remove_local_on_success=true`

### 注册入口

- 正式: `scripts/outlook_camoufox_stable_register.py`（Camoufox + Webshare；`register` / `--mode relogin`）
- 恢复: 本机 Camoufox OTP 重登 → Panda 隔离导入 → `/me` 验证 → 替换旧 token → `reload_from_storage()`
- 注册机 UI/协议批量已停用

### 调度约束

- `status != 禁用/限流/异常`
- 有可用额度或真无限/未知额度
- 无失败证据（`invalid_count`/refresh error/quota refresh error/preflight backoff）
- Panda 接收态 `verified_ready`/`verified`/`local_verified` 或空
- 全局并发 `image_global_concurrency=10`（auto_scale 未启用时）
- 单账号并发 `image_account_concurrency=2`
- `image_token_max_attempts=24`

## 历史

长流水已归档到 `docs/logs/2026/2026-07.md` 与 `docs/archive/`。本文档 2026-07-27 整体重写，旧版保留在 git 历史中。
