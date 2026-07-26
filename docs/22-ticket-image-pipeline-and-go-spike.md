# 22 — OpenAI 用票生图链路、验收与 Rust 数据面路线

最后更新：2026-07-23（Asia/Shanghai，验证通过）  
关联：`20` §0.1（纯 HTTP 真相源）、`14`（Rust 编排）、`13`（性能预估）、`21`（调度/暖号）

---

## 0. 术语

| 口语 | 本仓含义 |
|------|----------|
| **Chrome 开票** | 默认：**curl_cffi Chrome TLS 指纹** + 本地 **Turnstile VM**（`utils/turnstile.py`）解 `dx`，**非**启动 Chrome 浏览器 |
| **真浏览器开票** | 参考 monorepo `grokImage` 的 `chrometicket` 池（Chrome 预捕获 → Go/Python 消费）；gptimage **生产禁止**浏览器作数据面（`20`） |
| **票** | `chat-requirements` finalize 返回的 `token` + PoW + Turnstile；SSE 头 `OpenAI-Sentinel-*` |
| **用票生图** | 持票 `POST /backend-api/f/conversation` → SSE `image_gen` → poll → estuary 下载 |

---

## 1. 生产用票生图序列（Panda）

```text
账号 fp (impersonate chrome*) + sticky Webshare
  → bootstrap（home 软 403 可容忍；生图 hard fail）
  → ★ 开票：prepare → PoW + VM Turnstile → finalize → token
  → prepare（spa_tool：无 Sentinel 头）
  → ★ 用票：POST /f/conversation SSE（带 Sentinel 头）
  → poll conversation / tasks
  → estuary 下载 PNG
```

| 环节 | 代码 |
|------|------|
| 开票 | `openai_backend_api._get_chat_requirements_once()` |
| 用票 SSE | `_open_image_sse_with_cf_retry()` → `_start_image_generation()` |
| 协议形状 | `image_spa_tool_path=true`（默认 spa_tool）；`false` → picture_v2 |
| API 入口 | `POST /v1/images/generations`、`POST /api/image-tasks/*` |

证据：`captures/spa/H-*`～`O-*`、`acceptance-90s-picture_v2-20260723`。

---

## 2. 本机验证命令

### 2.1 仅 VM（无网络）

```bash
cd gptimage
python -m pytest test/test_turnstile_vm.py -q
```

### 2.2 纯 HTTP 单轮（对齐生产）

```bash
# 同步 Panda 账号 secret（需 ssh panda）
python scripts/_tmp_sync_qaflow_secret.py

# 经 Clash 或 Webshare
python scripts/_tmp_spa_image_bench3.py \
  --mode local_clash \
  --protocol spa_tool \
  --image-gen-deadline 90
```

### 2.3 Panda 编排验收（单轮）

```bash
python scripts/spa_image_panda_acceptance.py \
  --phase serial5 \
  --account-email qaflowakjewai6ps@proton.me \
  --protocol spa_tool \
  --image-gen-deadline 90 \
  --only-round 1
```

### 2.4 打 Panda API（走调度）

```bash
curl -s -X POST "https://gptimage.relai.asia/v1/images/generations" \
  -H "Authorization: Bearer $PANDA_AUTH_KEY" \
  -H "X-Preferred-Account-Email: qaflowakjewai6ps@proton.me" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-image-2","prompt":"a blue circle","n":1,"response_format":"b64_json"}'
```

前置：暖号（`account_warmup`）、sticky Webshare、非 paused。

---

## 3. 暖号与坏号熔断（2026-07-23）

`account_warmup` 配置（Panda 已部署）：

| 字段 | 值 | 说明 |
|------|-----|------|
| `rotate_per_tick` | **0** | 不再轮询打冷号 |
| `cf_fail_max_streak` | 2 | 连续 2 次 CF403 → block |
| `cf_block_sec` | 86400 | block 24h |
| `depth` | requirements | prepare+finalize 探活 |
| `max_hot` | 10 | 热池容量 |

监控：`GET /api/ops/warmup/status` → `hot`、`blocked_until`、`totals.skipped_blocked`。

备份：`/root/gptimage/backups/account-warmup-20260723-174851`。

---

## 4. Go 数据面 Spike（条件性路线）→ **改为 Rust 优先**

**前提**：用票生图链路在 Panda **已验证**（2026-07-23 `qaflow` `/v1/images` 200，35s，b64≈1.1MB；证据 `ticket-verify-20260723/`）。

**路线调整**：用户确认 **Rust 可替代 Go**；继续 `gptimage-gateway-rs` Phase B 生图运行时，**不**新建 Go 仓。grok2api 票池设计 **后置**（OpenAI 与 Grok 开票原理不同，且 grok 侧亦未跑通）。

### 4.1 决策门（Rust）

| 门 | 条件 | 失败则 |
|----|------|--------|
| **R0** | Python 开票+生图 E2E（本仓 bench3 或 `/v1/images`） | 不修 Rust |
| **R1** | `gptimage-gateway-rs` `IMAGE_ENABLED=1` + helper 单轮 200 | 保持 Python `:8012` |
| **R2** | 部分核心热路径 Rust 化（SSE/poll/download）RSS/CPU 达标 | 维持现状 |

### 4.2 票池（后置，参考 grok2api）

| 方案 | 说明 |
|------|------|
| **当前（R0 已通过）** | curl_cffi + 本地 Turnstile VM 现场开票（`openai_backend_api`） |
| **未来票池** | 可参考 `grokImage/chrometicket` **模式**；字段须适配 OpenAI `prepare_token/turnstile/proofofwork`；**grok2api 未跑通前不实施** |

### 4.3 永久留 Python

`account_service`、pipeline orchestrator、`web/`、运维环；Chrome minter（若未来做票池）。

---

## 4x. （归档）原 Go spike 草案

Go `gptimage-go-spike` 方案已搁置；见 git 历史或 `04` 旧版 **GO-SPIKE-001** 备注。性能对照仍见 `13`。

---

## 5. 相关 monorepo 资产

| 路径 | 用途 |
|------|------|
| `grokImage/backend/internal/application/chrometicket/` | 真浏览器票池参考 |
| `gptimage-gateway-rs/` | Rust 编排 + Python helper（`:8013`/`:19001`） |
| `docs/13-performance-and-rewrite-estimate.md` | Go vs Rust 性能列 |

---

## 6. 2026-07-23 验证结果（R0 ✅）

| 路径 | 账号 | 结果 | 耗时 | 证据 |
|------|------|------|------|------|
| `POST /v1/images/generations` (Python `:8012`) | `qaflowakjewai6ps@proton.me` | **200**，b64≈1.1MB | **35s** | `ticket-verify-20260723/summary.json` |
| bench3 `spa_tool` 原子链 | 同上 | **ok**，PNG 2.3MB | **47s** | `bench3_spa_tool.json` |

**Python 原型（冻结，后续不改）**：`scripts/prototype/openai_ticket_billing/v20260723/` + README。

开票阶段：`requirements_ok` @ 1768ms；SSE `has_image_gen=true`；poll+download 成功。

**结论**：OpenAI **现场 VM 开票 + 用票生图** 链路在 Panda **已跑通**；票池/grok 模式 **不阻塞** 当前主线。

## 7. 2026-07-23 Rust R1 验证（✅）

| 路径 | 账号 | 结果 | 耗时 | 证据 |
|------|------|------|------|------|
| `POST /v1/images/generations` (Rust `:8013` → helper `:19001`) | `qaflowakjewai6ps@proton.me` | **200**，b64≈1.17MB | **29.6s** | `/root/gptimage-gateway-rs/data/runlogs/rust-ticket-verify-20260723/summary.json` |

拓扑：Rust gateway 编排 OpenAI HTTP 形状 + `IMAGE_GLOBAL_CONCURRENCY` 信号量；Python `protocol_bridge` 侧车执行 curl_cffi / PoW / Turnstile VM / SSE / poll / download（与 Python R0 同链，非纯 Rust VM）。

Bringup：`IMAGE_ENABLED=1` + `panda_bringup_rust_face.sh`；helper 须 **rw** 挂载 `gptimage/data`（写 PNG）。生产 `:8012` **未切流**。

## 8. Sentinel 票生命周期专项实验（2026-07-23 启动）

> **动机**：生产默认「同 session 当场开票、当场用票」来自代码路径，**未经**跨请求/跨 IP/跨 session 复用与 TTL 上界标定。本节记录假设、实验矩阵与证据路径。

### 8.1 待验证假设

| ID | 假设 | 生产默认 | 专项实验 |
|----|------|----------|----------|
| H1 | 票可跨**同 IP 新 session**复用 | 未实现缓存 | `cross_session` |
| H2 | 票可**跨 IP**使用（无视开票出口） | 未实现 | `cross_ip` / `cross_both` |
| H3 | 同一张票可打**两次 SSE** | 每次 `_get_chat_requirements_once()` | `reuse` |
| H4 | 票有服务端 **TTL**；延迟用票超窗失败 | 代码未读 `exp` 字段 | `delay` + `ttl_sweep` |
| H5 | 高并发须**多浏览器多开票**（chrometicket 池） | 每请求 per-call finalize | `concurrent`（同账号并行 finalize+用票） |

### 8.2 分批实验矩阵（每批 2–10 分钟）

> **禁止**单次 `phase=all` + 全量 ttl_sweep（30–60 min）。按批执行，`--merge` 合并报告。

| Batch | 内容 | 目标耗时 |
|-------|------|----------|
| `batch1` | baseline + reuse + cross_session | ~5 min |
| `batch2` | cross_ip + cross_both | ~5 min |
| `batch3` | delay 30s | ~2 min |
| `batch4-60` | delay 60s（TTL 探针） | ~2 min |
| `batch4-120` | delay 120s | ~3 min |
| `batch4-300` | delay 300s | ~6 min |
| `batch5` | concurrent ×2 | ~5 min |

探针模式：SSE 仅读到 `conversation_id`（gate 10s / read 15s），**不等** `image_gen`。

```bash
# Panda（helper 容器 venv）
python scripts/_tmp_deploy_sentinel_ablation.py batch1
python scripts/_tmp_deploy_sentinel_ablation.py batch2 --merge
python scripts/_tmp_deploy_sentinel_ablation.py batch3 --merge
# ... batch4-* / batch5
python scripts/_tmp_deploy_sentinel_ablation.py batch5 --merge --fetch
```

脚本：`scripts/_tmp_sentinel_ticket_ablation.py`（`--batch` + `--merge`）  
部署：`scripts/_tmp_deploy_sentinel_ablation.py`  
证据：`data/runlogs/spa_repro/sentinel-ticket-ablation-20260723/ablation_report.json`

### 8.2x （归档）原单次全量矩阵

| Phase | 操作 | 成功判据 | 失败含义 |
|-------|------|----------|----------|
| `baseline` | finalize → 立即 SSE | HTTP 200 + `conversation_id` | 基线坏了 |
| `delay` | finalize 后 sleep N 再 SSE | 同上 | TTL 或绑定问题 |
| `reuse` | 同票连续两次 SSE | 第二次仍 200 | **单次票** |
| `cross_session` | session A 开票，session B（同 proxy）用票 | B 成功 | session 绑定 |
| `cross_ip` | proxy A 开票，proxy B 用票 | B 成功 | **IP 绑定可绕过** |
| `cross_both` | 新 session + 换 IP | 成功 | 双重绑定可绕过 |
| `ttl_sweep` | delay ∈ {0,5,15,30,60,120,180,300}s | 记录 last_ok / first_fail | **票存活时间上界** |
| `concurrent` | 同账号 N 路并行 finalize + 各自 SSE | N/N 成功 | 无需浏览器票池 |

脚本：`scripts/_tmp_sentinel_ticket_ablation.py`  
部署：`scripts/_tmp_deploy_sentinel_ablation.py`  
证据：`data/runlogs/spa_repro/sentinel-ticket-ablation-20260723/ablation_report.json`  
捕获：`docs/captures/spa/P-sentinel-ticket-ablation-20260723.{json,md}`

### 8.3 生产含义（实验前占位）

- 若 H3/H2 全失败 → **票池须绑 IP+session+单次消费**；当前 per-call 开票正确。
- 若 H5 通过（curl 并行 finalize）→ **不必**为多账号 HTTP 并发上浏览器开票；chrometicket 仅作 CF/真浏览器兜底参考。
- 若 H4 给出 TTL → 未来票池 `max_age` 建议 **<300s**（2026-07-23 实测下界 ≥300s，上界未触顶）；消费出口实测**可不绑**开票 IP（见 `P-sentinel`）。

**结果（2026-07-23 全批完成）**：见 `docs/captures/spa/P-sentinel-ticket-ablation-20260723.md`。

### 8.4 全链路验证套件（2026-07-23）

在 ablation（SSE 探针）之后，用 **全量生图 + TrafficMeter 带宽** 扩面：

| 套件 | 命令 | 证据 |
|------|------|------|
| reuse-gap | `sentinel_ticket_validation_suite.py reuse-gap` | `reuse_gap_report.json` |
| cross-serial ×5 | `cross-serial --round N` | `cross_serial_report.json` |
| cross-concurrent ×3 | `cross-concurrent --round N --workers 10` | `cross_concurrent_report.json` |
| longevity 7 档 | `longevity --tier 10m\|…\|12h`（nohup） | `longevity/<tier>/longevity_result.json` |

编排：`scripts/_tmp_run_sentinel_validation.py`（出错即停）。  
捕获：`docs/captures/spa/Q-sentinel-ticket-validation-20260723.md`。

**已确认（截至当晚）**：

- cross_session + cross_ip：**串行 10/10、并发 30/30** 全量生图成功。
- reuse-gap：60s 间隔同票复用成功；120s CF403（停）；与「立即连打 CF403」一致指向**频控**而非纯 TTL。
- longevity：7 档已后台挂起，结果待 sleep 结束后写入各 tier 目录。

### 8.5 Rust 票池（2026-07-23 启动）

| 组件 | 路径 | 说明 |
|------|------|------|
| crate | `gptimage-gateway-rs/crates/ticket_pool` | `deposit` / `acquire` / `refresh` |
| TTL | `DEFAULT_TICKET_TTL_SECS = 300` | 依据 reuse-gap + ablation delay≤300s |
| 复用策略 | `ReusePolicy::*Experimental` | **占位**，默认 `PerCallFinalize` |
| 测试 | `cargo test -p ticket_pool` | 入票/过期刷新/取票 |

网关 `AppState` 接线留 Phase C；Python `:8012` 仍 per-call finalize。

## 9. 真 Chrome 出票 + curl_cffi 用票：价值评估（2026-07-24）

> **术语**：本仓「Chrome 开票」（§0）= curl_cffi Chrome TLS + **VM Turnstile**，非启动 Chrome。本节讨论 **真浏览器 chrometicket 池**（参考 `grokImage/chrometicket`）。

### 9.1 假设链路

```text
Chrome/Camoufox 后台循环：prepare → 浏览器内过 Turnstile → finalize → 票入池
  → 消费侧 curl_cffi：prepare → SSE 用票 → poll → download
```

### 9.2 已有证据

| 证据 | 结论 |
|------|------|
| R0 / acceptance | VM 现场开票 + 用票：**serial5 5/5、conc10 30/30** |
| `P-sentinel-ticket-ablation` | 同账号 **curl 并行 finalize 2/2**；票**不绑**开票 IP；**不必**浏览器多开票 |
| `panda-socks-camoufox` | 难 IP 上 Camoufox 可过 prepare，但 **SSE 全链路未收口**；生产用 Webshare 已可全闭环 |
| bench3 阶段耗时 | requirements **~1–2s**；SSE **~28–78s** |
| `20` 红线 | 生产禁止浏览器作**数据面** |

### 9.3 价值判断：**生产主线价值低**

| 维度 | 真 Chrome 出票 | 当前 VM 现场开票 |
|------|----------------|------------------|
| 墙钟节省 | 约 **1–3s**（跳过 finalize） | 已占总量 **2–5%** |
| CF 通过率 | 难 IP prepare 可能更好 | 好 Webshare + 暖号已验收 |
| 工程成本 | Chrome 进程池、健康检查、SDK 漂移、票 TTL/禁双 SSE | 已部署、已 R0/R1 |
| 资源 | 每 minter ~数百 MB RSS | VM 解算，无浏览器 |
| 合规 | 触碰 `20` 数据面红线 | 符合纯 HTTP 裁决 |

**值得做的场景（非生产默认）**：

1. **诊断**：BENCH-004 跑 Camoufox 全链路，与 curl_cffi 对照（栈 vs 字段，`F-errors` 判别法）。
2. **实验**：Turnstile VM 完全失效时的 **临时** fallback（须单独 ADR，不能 silent 上线）。
3. **差 IP 暖机交接**（`A-warm-handoff`）：浏览器拿 cookie/过 sentinel → HTTP 跑生图（PoC 级，非票池）。

**不值得现在投入**：

- 生产 chrometicket 池替代 VM finalize（性价比低 + `P` 已否定必要性）。
- 真浏览器跑 SSE 数据面（违反 `20`，且 Playwright APIRequest 长流超时）。

### 9.4 若仍要做验证实验

最小实验（**对照，非上线**）：

```bash
# 1) 浏览器全链路（BENCH-004 规划）
python scripts/image_path_benchmark_suite.py browser \
  --script scripts/_tmp_spa_camoufox_via_panda_socks.py --runs 3

# 2) 与 pure_http 同账号同 prompt 比 wall/SSE/成功率
python scripts/image_path_benchmark_suite.py compare \
  --dirs data/runlogs/image-path-benchmark/20260724/pure_http,.../browser
```

验收门槛：成功率 **不高于** 当前 VM 路径，且墙钟 **无明显优势** → **归档，不产品化**。

## 10. 变更日志

| 日期 | 说明 |
|------|------|
| 2026-07-24 | **稳住出图计划**（`24`）：STAB-A～E；IMG-STABLE v1 验收；真 Chrome 开票明确不做 |
| 2026-07-24 | **真 Chrome 出票评估**（§9）：生产价值低；VM 开票已够；chrometicket 仅作诊断/fallback 实验 |
| 2026-07-23 | **Phase-2 验证**：quota_sync 写回 DB；reuse-gap v2；Rust `ticket_pool` TTL=300s |
| 2026-07-23 | **全链路验证套件**：reuse-gap / cross-serial×5 / conc10×3 / longevity 7 档；见 §8.4、`Q-sentinel-ticket-validation-20260723` |
| 2026-07-23 | **票生命周期专项实验**启动：delay/reuse/cross_session/cross_ip/ttl_sweep/concurrent；见 §8 |
| 2026-07-23 | **R1 Rust 单轮通过**：`:8013` 开票+生图 200 @ 29.6s；b64≈1.17MB；证据 `rust-ticket-verify-20260723/` |
| 2026-07-23 | **R0 验证通过**：`/v1/images` 35s + bench3 spa_tool 47s；路线改为 Rust 优先；grok 票池后置 |
| 2026-07-23 | 初版：用票链路、暖号熔断、Go spike 决策门 |
