# 生图可观测性与三路径性能对比 — 待办真相源

最后更新：2026-07-24（Asia/Shanghai，sprint 收尾）

> **本 sprint 已基本完成**；当前执行计划见 [`24-image-stability-plan.md`](./24-image-stability-plan.md) 与根目录 [`plan.md`](../plan.md)。  
> 施工总览见根目录 `plan.md`。调度与 `phase_timings` 字段语义见 [`21-image-scheduling-and-pipeline.md`](./21-image-scheduling-and-pipeline.md) §8。

---

## 背景与诉求来源

用户 2026-07-24 提出五项工程诉求 + 三路径横向对比实验：

1. 日志「调用耗时」展示各阶段拆分（排队、上游 SSE、上下行等）
2. 日志单页支持 **200 条/页**
3. 一张图两次调用日志 — 去重/合并
4. 前端图片加载慢（缩略图、大图预览）— 查因并优化
5. 统计输入/输出 token、按耗时算 t/s；对比 **批量出票+用票** / **纯 HTTP** / **纯浏览器** 三种生图路径的性能、流量、资源开销

---

## 总体进度（约 85%）

| 流 | 主题 | 完成度 | 阻塞 |
|----|------|--------|------|
| A | 日志 UI（前端） | **100%** | — |
| B | 日志后端 | **~95%** | `task_id` 精确查询、`sse_bytes` 未做 |
| C | 图片加载性能 | **~90%** | 已部署；历史图无批量缩略图 |
| D | 三路径基准 | **~70%** | pure_http + `/v1/images` 已测；browser 未跑 |

---

## 已完成（代码已落地，附文件）

### B1. 阶段耗时写入 call log

- [x] 成功路径：`pending_call_log` → finalize 后 `_emit_pending_call_log` **单条**写入
- [x] `detail.phase_timings_ms` 全量 + 扁平 `phase_*_ms` 字段
- [x] `total_wall_ms` / `task_queue_ms` / `worker_duration_ms` 优先展示墙钟
- 文件：`services/image_task_service.py`（`_apply_terminal_timing_fields`、`_log_call`、`_emit_pending_call_log`）

### B2. Token 与吞吐

- [x] `_call_log_usage_fields`：`prompt_tokens` / `completion_tokens`（及 input/output 别名）
- [x] `_tokens_per_sec_from_sources`：优先 `sse_stream_ms`，否则 `total_wall_ms`
- [x] `_call_log_traffic_fields`：`upload_bytes` / `download_bytes` / `traffic_bytes`
- 文件：`services/image_task_service.py`（约 L138–220、L1946–1970）
- 测试：`test/test_image_task_service.py` **22 passed**（子 agent 验证）

### B3. 日志 API limit

- [x] `GET /api/logs?limit=` 默认 200，范围 1–2000
- [x] `web/src/lib/api.ts` — `fetchSystemLogs({ limit, source, outcome })`
- 文件：`api/system.py`

### B4. 双日志合并（新生产任务）

- [x] 成功路径不再即时 `_log_call`，仅 finalize 后写一条「文生图调用完成」
- [x] 已去掉独立的「文生图阶段耗时」即时写入（历史记录仍在 logs.jsonl）
- 文件：`services/image_task_service.py`

### C1. 图片加载根因修复

- [x] `image_storage_service.save()` 保存后同步 `ensure_thumbnail(rel)`（320px）
- [x] 原图/缩略图 `Cache-Control: public, max-age=86400`
- [x] `image-lightbox.tsx`：加载态（blur/opacity）+ 打开时 `Image()` 预加载
- [x] `image-thumbnail.tsx`：`fetchPriority="low"`
- 文件：`services/image_storage_service.py`、`services/image_service.py`、`web/src/components/*`

### A0. 前端工具库（未接入页面）

- [x] `web/src/lib/image-log-phases.ts`：
  - `formatPhaseTimings` / `getInlinePhases` / `formatDurationMs` / `formatTokensPerSec`
  - `dedupeCallLogs`（同 `task_id` 隐藏历史「阶段耗时」行）
- [x] `logs/page.tsx` 内 `DurationCell` 组件已写（**表格未使用**）

### D0. 基准脚本框架

- [x] `scripts/image_path_benchmark_suite.py`
  - Schema：`image-path-benchmark/v1`
  - `pure_http`：复用 `_tmp_spa_image_bench3.run_once`
  - `browser` / `ticket_pool`：**桩命令**（列入口说明，不执行真实生图）
  - `compare`：三目录 P50/P90 → `compare-summary.json`
- 证据目录约定：`data/runlogs/image-path-benchmark/{YYYYMMDD}/`

### 文档

- [x] 根目录 `plan.md` 重写为本 sprint 施工计划（2026-07-24）

### 历史热修（已部署 Panda，早于本 sprint）

- [x] `conversation.py` poll 阶段 `token` NameError → `backend.access_token`（`token-hotfix-20260724`）
- [x] `image_binding_inflight_max` 默认对齐 `image_account_concurrency=2`
- [x] 2 并发阶段耗时证据：account_queue + sse_stream 为瓶颈（非 per_user 满槽）

---

## 未完成 / 进行中

### A1. 日志列表 UI — **已完成（2026-07-24）**

| 子项 | 状态 | 说明 |
|------|------|------|
| 调用耗时列阶段 chips | ✅ | `DurationCell` 已接入表格 |
| 分页 10/50/200 | ✅ | `useState pageSize` + Select |
| `limit: 2000` 拉取 | ✅ | `fetchSystemLogs({ limit: 2000 })` |
| 历史去重展示 | ✅ | `dedupeCallLogs` → `visibleItems`；并隐藏 LoggedCall 瘦日志 |
| 详情 Token/流量区块 | ✅ | Token / 流量卡片 |
| 详情图用缩略图 URL | ✅ | `ImageThumbnail` + lightbox 全图 |

**修复清单（估计 30–45 min）：**

1. 删除 `const pageSize = 10` 硬编码，保留 `useState`
2. `visibleItems = useMemo(() => dedupeCallLogs(items), …)` 替换 `items` 分页
3. 表格 `{isCallLog ? <DurationCell item={item} /> : null}`
4. 工具栏加 `Select`：10 / 50 / 200 条/页
5. `fetchSystemLogs({ …, limit: 2000 })`
6. 详情页增加 Token 用量 + 流量字段卡片
7. `npm run build` 通过

### B5. 后端收尾

| 子项 | 状态 | 说明 |
|------|------|------|
| 部署 Panda | ❌ | B2/B4/C1 均未上线 |
| `GET /api/logs?task_id=` | ❌ | 可选，按 task 精确查一条 |
| SSE 字节统计 | ❌ | protocol 层未统一上报 `sse_bytes` |
| 老日志迁移 | — | 不迁移；靠 UI 去重 + 新单条格式 |

### C2. 图片性能验证

| 子项 | 状态 | 说明 |
|------|------|------|
| 部署后首屏抽样 | ❌ | 目标：新图缩略图 <300ms |
| 历史图批量预生成缩略图 | ❌ | 可选脚本遍历 `data/images/` 调 `ensure_thumbnail` |
| lightbox 详情页缩略图 | ❌ | 日志详情仍直连全图 |
| `decoding="async"` 列表缩略图 | ❌ | 小优化，未做 |

### D1. 三路径实测 — **P1**

| 路径 | 代号 | 脚本状态 | 实测 |
|------|------|----------|------|
| 生产 `/v1/images` 全链路 | `ticket_pool` | ✅ 可调生产 API | ✅ 3/5（`R-image-path-benchmark-20260724`） |
| bench3 原子纯 HTTP | `pure_http` | ✅ 可跑 | ✅ 5/5 Panda |
| Camoufox 真浏览器 SSE | `browser` | 脚本有 | ❌ BENCH-004 未跑 |

**每条 run 必须采集（compare 验收）：**

- 耗时：`total_wall_ms`、`phase_timings_ms` 全字段、`task_queue_ms`
- 流量：`upload_bytes`、`download_bytes`、PNG 文件大小、SSE 字节（若可测）
- Token：`prompt_tokens`、`completion_tokens`、`tokens_per_sec`
- 资源：RSS/CPU（`psutil`）、磁盘写入、可选 `docker stats`
- 结果：success/fail、conversation_id、account_hash、proxy egress IP

---

## 待办事项（按优先级）

### P0 — 今日必须（阻塞可用性）

- [x] **LOG-UI-001** 修复 `web/src/app/logs/page.tsx` 构建错误并完成 A1 全部子项
- [x] **DEPLOY-001** `npm run build` → 同步 `web_dist`；后端文件 scp/artifact → Panda 重启
- [x] **VERIFY-001** Panda 单次 2 并发生图：仅 1 条 call log；列表可见阶段 chips；200 条/页可翻
  - 证据：`docs/captures/spa/verify-001-20260724T083730Z.json`（pass=true）
  - 附：去掉 `api/ai.py` LoggedCall 成功双写

### P1 — 本周（对比实验）

- [x] **BENCH-001** Panda 跑 `pure_http --runs 5 --gap-secs 30 --mode panda_webshare`，证据写入 `data/runlogs/image-path-benchmark/20260724/pure_http/`（5/5 ok）
- [x] **BENCH-002** 同环境跑生产 `/v1/images`（ticket_pool 路径）5 轮，目录 `ticket_pool/`（3/5 ok；前 2 轮 240s 超时）
- [x] **BENCH-003** `image_path_benchmark_suite.py compare` 生成 `compare-summary.json` + `docs/captures/spa/R-image-path-benchmark-20260724.md`
- [ ] **BENCH-004** 实现 `browser` 子命令：包装 `scripts/_tmp_spa_camoufox_via_panda_socks.py` 或等价，输出同 schema JSON
- [ ] **BENCH-005** 实现 `ticket_pool` 子命令：对接 `gptimage-gateway-rs` helper `:19001` 或 Python 开票链
  - 注：生产 `/v1/images` 路径已可跑；helper 桩仍可选

### P2 — 体验与数据质量

- [ ] **IMG-PERF-001** 脚本批量 `ensure_thumbnail` 历史 `data/images/*`（一次性）
- [ ] **IMG-PERF-002** 日志详情大图改 `getImageThumbnailUrl` + lightbox 点开再拉全图
- [ ] **LOG-API-001** 可选 `GET /api/logs?task_id=` 精确查询
- [ ] **LOG-PROTO-001** protocol 层上报 `sse_bytes` 写入 `phase_timings` 或 traffic 字段
- [ ] **OPS-001** Ops 页增加 `image_gen_ttft_p50/p95`（关联 backlog **PROTO-UPSTREAM-LATENCY**）

### P3 — 文档与归档

- [x] **DOC-001** 本文件勾选状态随部署更新（2026-07-24）
- [ ] **DOC-002** `21` §8 补充 call log 字段表（`tokens_per_sec`、`traffic_bytes`）
- [ ] **DOC-003** `CHANGELOG.md` 记录本 sprint 用户可见变更

---

## 三路径对比实验设计（详细）

### 路径定义

| 路径 | 执行方式 | 入口命令（规划） |
|------|----------|------------------|
| **ticket_pool** | 生产 `POST /v1/images`（现场 VM 开票+编排；**非** Rust 预开票池） | `python scripts/image_path_benchmark_suite.py ticket_pool --base-url …` |
| **pure_http** | curl_cffi 严格纯 HTTP，无浏览器 | `python scripts/image_path_benchmark_suite.py pure_http --mode panda_webshare --runs 5` |
| **browser** | Camoufox 真浏览器 SSE | `python scripts/image_path_benchmark_suite.py browser --script scripts/_tmp_spa_camoufox_via_panda_socks.py` |

### 对照变量（固定）

- Prompt：`image_path_benchmark_suite.py` 内 `MEDIUM_PROMPT`（与 bench3 一致）
- Deadline：90s（与当前验收 gate 一致）
- 账号：优先 `qaflow` 或单账号 sticky Webshare（记录 `account_hash`、egress IP）
- 并发：先 **串行 5**，再 **conc2**（与 2 并发观测对齐）

### 输出报告模板（`docs/captures/spa/R-image-path-benchmark-{date}.md`）

```markdown
# 三路径生图性能对比 — {date}

## 环境
- Panda 版本 / artifact
- 账号 / binding / egress IP
- prompt 摘要

## 汇总表
| 路径 | N | 成功率 | P50 wall | P50 SSE | P50 下载 | P50 tokens/s | 上行 MB | 下行 MB | RSS peak |

## 结论
- 墙钟瓶颈：…
- 流量差异：…
- 推荐生产路径：…
```

---

## 验收标准（本 sprint 关闭条件）

1. **日志**：单次 2 并发生图 **仅 1 条** call log；列表 200 条/页；耗时列示例：`69.6s` + chips `排队 1.4s · SSE 56.5s · 下载 0.6s`
2. **图片**：新生图缩略图首屏 **<300ms**；lightbox 同域缓存后 **<1s**
3. **Token**：成功 call log 含 `completion_tokens` + `tokens_per_sec`
4. **对比**：三路径（至少 pure_http + ticket_pool 生产路径）各 N≥5，`compare-summary.json` 含 P50/P90

---

## 关键文件索引

| 文件 | 用途 |
|------|------|
| `plan.md` | Sprint 总览与流 A–D 勾选 |
| `web/src/lib/image-log-phases.ts` | 阶段展示 + 去重逻辑 |
| `web/src/app/logs/page.tsx` | 日志页（LOG-UI-001 已完成） |
| `services/image_task_service.py` | call log 写入、timing、token |
| `api/system.py` | `/api/logs` limit |
| `scripts/image_path_benchmark_suite.py` | 三路径基准 |
| `docs/21-image-scheduling-and-pipeline.md` | phase_timings 语义 |
| `docs/22-ticket-image-pipeline-and-go-spike.md` | 用票链路背景 |

---

## 风险与依赖

| 风险 | 缓解 |
|------|------|
| 日志页 build 失败无法发布前端 | P0 先修 `pageSize` 重复与 `formatDuration` |
| 老图无缩略图仍慢 | 部署后新图生效；可选批量 `ensure_thumbnail` |
| browser 路径难自动化 | 先完成 pure_http vs ticket_pool 二路径对比 |
| Panda 未部署本轮后端 | DEPLOY-001 与 LOG-UI 同步上线 |
