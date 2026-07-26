# 24 — 稳住出图计划（2026-07-24 起）

最后更新：2026-07-24（Asia/Shanghai）  
状态：**当前主线**（替代可观测性 sprint 为「执行计划」）

关联：`06-handoff.md`、`04` **IMG-STABLE**、`20`（纯 HTTP 红线）、`17`（CF）、`21`（调度）、`22` §9（不做真 Chrome 开票）、`23`（可观测性 sprint 已收尾）

---

## 0. 目标与已锁定决策

### 0.1 什么叫「稳住出图」

| 层级 | SLO（暖号 + 好出口前提下） | 证据基线 |
|------|---------------------------|----------|
| **单账号串行** | serial5 **≥5/5**，gate **90s**，无换号止损 | `acceptance-90s-picture_v2-20260723` |
| **单账号并发** | conc10 **≥30/30**（3 轮×10） | 同上 |
| **生产 API** | `/v1/images` 单轮 **200**，墙钟 **≤90s**（P50 目标 ≤60s） | `ticket-verify-20260723`、VERIFY-001 |
| **多账号轮询** | conc10 **≥8/10**（短期）；长期 **≥9/10** | 当前 **4/10**（冷号 CF @ init） |
| **失败可解释** | 每条失败 call log 有 `phase_timings` + CF 分层字段 | VERIFY-001 已 pass |

**接受**：偶发 CF403、上游 `no_image_gen` 空窗——靠 **换号/换出口/暖号 block**，不追求 100%。

### 0.2 已锁定（不再讨论）

| 决策 | 理由 |
|------|------|
| **不做真 Chrome 开票** | `22` §9；VM 开票 R0；`P` 实验否定浏览器票池必要性 |
| **不做浏览器生图数据面** | `20` 红线 |
| **FlareSolverr 不进生图根方案** | 仅注册 clearance；`17` |
| **不大规模继续协议挖矿** | `19` Now/Next 完成；改走 PROTO-REFACTOR（上传链，文生图不挡） |
| **不做 Chrome 一次性 HAR** | Camoufox HAR 已够字段对齐；Chrome 仅栈 ADR，非解锁项 |
| **Camoufox 保留** | 注册/重登 + SDK 漂移时补抓 HAR |

### 0.3 技术栈（不变）

```text
curl_cffi Chrome impersonate + sticky Webshare
  → VM Turnstile 现场开票
  → spa_tool（默认）/ picture_v2（canary）
  → SSE → poll → estuary 下载
```

---

## 1. 失败面归因（稳住要先认账）

| 失败类 | 占比/场景 | 杠杆 | 文档 |
|--------|-----------|------|------|
| **CF403 @ init** | 多账号 conc10 6/10 失败；冷号+差 IP | 暖号、调度前 CF 探活、换出口 | `17`、`acceptance-90s-multiacct` |
| **上游 SSE 慢/空窗** | P50 SSE 47–78s；偶发 `no_image_gen` | 非 CF；压 TTFT 或维持 90s gate | `PROTO-UPSTREAM-LATENCY` |
| **编排/超时假失败** | BENCH ticket_pool 2/5 @ 240s 客户端超时 | 对齐 540s；公平复测 | `R-image-path-benchmark-20260724` |
| **额度/坏号** | quota=0、deactivated | 调度闸门、删号隔离 IP | `21` §2.1 |
| **poll/download CF** | 偶发；与票无关 | poll 快失败+换出口 | `Q-sentinel-ticket-validation` |

**非主因**：开票（~1–2s）、真 Chrome 票池、协议字段缺口（暖号路径已验收）。

---

## 2. 执行阶段

### Phase A — 基线复测（P0，1–2 天）

> 在**可信 SLO 数字**上再谈优化；消除 BENCH 方法学噪声。

| ID | 任务 | 做法 | 验收 |
|----|------|------|------|
| **STAB-A1** | 公平 serial5（仅 `/v1/images`） | 号闲后单独跑 5 轮；**不**先跑 bench3；客户端 wait **540s** | `5/5` @ 90s；证据 `captures/spa/STAB-serial5-*.md` |
| **STAB-A2** | 对齐 bench 脚本超时 | `image_path_benchmark_suite.py` ticket_pool 默认 timeout=540 | 无 `http_code=0` 假失败 |
| **STAB-A3** | 固化对照命令 | 文档化：`spa_image_panda_acceptance.py` + `image_path_benchmark_suite.py` 参数 | `24` §附录 |

**出口**：一份 **STAB 基线报告**（serial5 + 可选 conc2），作为后续改动的 before 快照。

---

### Phase B — CF 与号池（P0，贯穿）

> 多账号 4/10 → 8/10 的主杠杆；不碰协议。

| ID | 任务 | 做法 | 验收 |
|----|------|------|------|
| **STAB-B1** | 调度前 CF 探活 | 扩展现有 `probe_proxy_cf`：进 `verified_ready` 前或 dispatch 前对 binding 探活；CF block 24h（已有 warmup 逻辑对齐） | multiacct conc10 **≥8/10** |
| **STAB-B2** | ~~新号观察期~~ | **已取消**（2026-07-24）：不单独设新号纪律，沿用 B1 热池-only | — |
| **STAB-B3** | ~~坏 IP 隔离纪律~~ | **已取消**（2026-07-24）：沿用现有 CF 探活 / quarantine 机制 | — |
| **STAB-B4** | 同 binding 并发 | 保持 **≤1** 同 binding 同时生图（生产默认）；conc10 用**不同 binding** 账号 | 无同 IP 突发 10 连打 |
| **STAB-B5** | 暖号热池监控 | 每日看 `GET /api/ops/warmup/status`：`hot`≥可调度 conc 需求 | `hot` 不足时先暖再放量 |

**Camoufox 在本阶段**：仅 **注册/重登**（`16`）；不为 CF 开浏览器数据面。

---

### Phase C — 编排与超时（P1，2–3 天）

> 消灭「能出图但客户端/队列判失败」。

| ID | 任务 | 做法 | 验收 |
|----|------|------|------|
| **STAB-C1** | 生产等待与 BENCH 一致 | 确认 `image_task` 默认 max wait **540s**；日志可见 `task_queue_ms` | A1 与生产行为一致 |
| **STAB-C2** | 排队可观测 | Ops/日志：queued 阶段 >30s 打标；account_queue 瓶颈账号可识别 | VERIFY 类问题可复盘 |
| **STAB-C3** | IMAGE failover | CF @ requirements/start → 换号 1 次；poll CF×2 abort（已有，回归单测） | `test_image_poll_cf_abort` 绿 |
| **STAB-C4** | quota 闸门 | dispatch 前 `image_quota_state=ready`；quota=0 不抢槽 | 无 quota 空转占并发 |

---

### Phase D — 上游延迟（P1，并行/后续）

> 稳定性不依赖此项，但降 wall 钟、可收回 65s gate。

| ID | 任务 | 做法 | 验收 |
|----|------|------|------|
| **STAB-D1** | SSE TTFT 分布 | 从 call log 聚合 `sse_stream_ms` 到首个 `image_gen`；Ops `image_gen_ttft_p50/p95` | 可见 p50/p95 趋势 |
| **STAB-D2** | 路径对比 | `spa_tool` vs `picture_v2` 同账号各 serial5 | 选 P50 更低者为默认 |
| **STAB-D3** | 冗余 prepare 审计 | 对照 `phase_timings`：是否可省一轮 prepare | 有/无节省的实测结论 |

目标：p50 TTFT **≤45s**（长期）；gate 从 90s → 65s。

---

### Phase E — 持续运维（P2）

| ID | 任务 | 频率 |
|----|------|------|
| **STAB-E1** | serial5 冒烟 | 每周 1 次，固定 `qaflow` 或轮换暖号 |
| **STAB-E2** | multiacct conc10 | 每次扩池或换绑批次后 |
| **STAB-E3** | Turnstile/SDK 漂移 | `OAI-Client-Version` 变更时 Camoufox 补 1 条生图 HAR + VM 回归 |
| **STAB-E4** | 图生图需求出现时 | PROTO-REFACTOR 上传链（sediment）；**不挡文生图稳定** |

---

## 3. 明确不做（本计划周期）

- 真 Chrome / chrometicket 票池
- Rust 票池生产接线（`ticket_pool` crate 保持实验）
- BENCH-004 browser 全链路（除非 STAB-B 后仍无法解释栈差失败）
- Chrome DevTools 全量重抓
- 用 FlareSolverr 洗生图 CF403
- 为压延迟上浏览器 SSE

---

## 4. 优先级总览

```text
本周必做：STAB-A1 → STAB-A2 → STAB-B1/B4/B5
并行跟进：STAB-C1/C3、STAB-D1
达标后：  STAB-A1 5/5 + multiacct ≥8/10 → 宣告「稳住」v1
后续：    STAB-D2/D3、PROTO-REFACTOR（有图生图需求时）
```

---

## 5. 验收关闭条件（IMG-STABLE v1）

1. **STAB-A1**：`/v1/images` serial5 **5/5**（540s wait，单账号暖号）。
2. **STAB-B 后**：`preferred_account_email` 多账号 conc10 **≥8/10** @ 90s。
3. **7 天运维**：无 P0 回滚；`GET /api/ops/warmup/status` 热池覆盖日常并发。
4. 文档：`06`/`02`/`04` 同步；证据落 `captures/spa/STAB-*.md`。

---

## 附录 A — 推荐命令

```bash
# 1) 生产 API 公平 serial5（号闲后，勿先跑 bench3）
python scripts/spa_image_panda_acceptance.py \
  --phase serial5 \
  --account-email qaflowakjewai6ps@proton.me \
  --protocol spa_tool \
  --image-gen-deadline 90

# 2) 仅 /v1/images 路径 benchmark（timeout 540）
python scripts/image_path_benchmark_suite.py ticket_pool \
  --base-url https://gptimage.relai.asia \
  --runs 5 --gap-secs 60 --timeout 540

# 3) 多账号 conc10（调度前确认各号已暖）
python scripts/spa_image_panda_acceptance.py \
  --phase conc10 \
  --image-gen-deadline 90

# 4) 暖号状态
curl -s -H "Authorization: Bearer $PANDA_AUTH_KEY" \
  https://gptimage.relai.asia/api/ops/warmup/status | jq .
```

## 附录 B — 文档索引

| 主题 | 文件 |
|------|------|
| 本计划 | `24-image-stability-plan.md` |
| CF | `17-cf403-and-egress.md` |
| 纯 HTTP 红线 | `20-pure-http-image-sentinel-todo.md` |
| 调度/双槽 | `21-image-scheduling-and-pipeline.md` |
| 协议 HAR 链 | `captures/spa/README.md` §Camoufox |
| 可观测性 sprint | `23-image-observability-and-benchmark-todo.md` |
