# Q — Sentinel 票全链路验证（longevity / reuse-gap / cross 矩阵）

日期：2026-07-23（Asia/Shanghai）  
环境：Panda `gptimage-gateway-rs-helper` venv，`/app/data/accounts.db`  
脚本：`scripts/sentinel_ticket_validation_suite.py`  
编排：`scripts/_tmp_run_sentinel_validation.py`（出错即停）  
证据根：`/root/gptimage/data/runlogs/spa_repro/sentinel-ticket-validation-20260723/`

---

## 1. 执行摘要

| 阶段 | 计划 | 结果 | 备注 |
|------|------|------|------|
| **reuse-gap** | 同票首图 + 间隔 60/120/300/600s 复用 | **部分通过** | 首图 ✅；60s ✅；**120s CF403 停** |
| **cross-serial** | 5 轮 ×（cross_session + cross_ip）全量生图 | **10/10 ✅** | 每轮 ~2 图，带宽 ~3.5MB/轮 |
| **cross-concurrent** | 3 轮 × 10 并发全量生图 | **30/30 ✅** | 单轮总带宽 ~17–20MB |
| **longevity** | 10m/15m/30m/60m/3h/6h/12h 各独立票 | **进行中** | `longevity-launch` nohup 已挂 |

主测账号：`qaflowxho1z6hynk@proton.me`（quota=25）。旧主号 `qaflowakjewai6ps` 当日 poll 超时已弃用。

---

## 2. reuse-gap（同票间隔复用）

账号：`qaflowxho1z6hynk@proton.me`，开票 IP `92.113.236.188`

| 步骤 | 间隔 | 结果 | 耗时 | 出口 IP | 总带宽 | 图片 |
|------|------|------|------|---------|--------|------|
| first | — | ✅ | 34.5s | 92.113.236.188 | 1.73 MB | 855 KB PNG |
| after_60s | 60s | ✅ | 30.7s | 92.113.236.188 | 1.75 MB | 866 KB PNG |
| after_120s | 120s | ❌ | 1.4s | — | — | CF403 on `/f/conversation` |

**解读**：

- **立即连打**（ablation `reuse`）：第二次 SSE CF403 → 疑连打风控，非票过期。
- **间隔 60s**：同票第二次全量生图成功 → 票在 ≥60s 内仍有效。
- **间隔 120s**（累计约 3 分钟内第 3 次用同票）：CF403 → 更可能是 **同账号短时多图 + 连打风控**，而非票 TTL 到点；需与 longevity 长延迟结果对照。

报告：`reuse_gap_report.json`

---

## 3. cross-serial（5 轮串行）

主号：`qaflowxho1z6hynk@proton.me`；跨 IP 出口：`104.252.149.121`（dreamachristine 绑定）

| 轮次 | cross_session（同 IP） | cross_ip（换 IP） | 轮带宽合计 |
|------|------------------------|-------------------|------------|
| 1 | ✅ 1.79 MB @ 92.113.236.188 | ✅ 1.77 MB @ 104.252.149.121 | 3.56 MB |
| 2 | ✅ 1.81 MB | ✅ 1.63 MB | 3.44 MB |
| 3 | ✅ | ✅ | 3.46 MB |
| 4 | ✅ | ✅ | 3.57 MB |
| 5 | ✅ | ✅ | 3.53 MB |

典型单图：`elapsed_ms` 28–52s；`image_bytes` ~0.82–0.89 MB；`http_calls` 14。

报告：`cross_serial_report.json`

---

## 4. cross-concurrent（3 轮 × 10 工）

每轮 10 账号（quota≥1，含历史 CF 标记号），交替 `cross_ip` / `cross_session_same_ip`。

| 轮次 | 成功 | 总带宽 | 图片带宽合计 | elapsed_ms 范围 |
|------|------|--------|--------------|-----------------|
| 1 | 10/10 | 17.37 MB | 8.53 MB | 27–91s |
| 2 | 10/10 | 20.49 MB | 9.66 MB | 29–72s |
| 3 | 10/10 | 17.76 MB | 8.72 MB | 27–50s |

**带宽观察**：

- 单次全量生图（开票+SSE+poll+下载）约 **1.6–1.8 MB** 下行；图片本体 **~0.82–0.90 MB**。
- 10 并发单轮峰值总带宽 **~20 MB**（3 分钟内完成），瓶颈在代理出口带宽而非账号额度（池内 15 个有额度号）。
- 并发未触发 CF403（与 07-23 早期冷号 conc10 不同）；本轮均为有额度暖号 + 不同 binding。

报告：`cross_concurrent_report.json`

---

## 5. longevity（后台挂起）

```bash
python scripts/_tmp_run_sentinel_validation.py longevity-launch
```

| 档位 | sleep | 日志 | 状态（启动时） |
|------|-------|------|----------------|
| 10m | 600s | `longevity/10m.nohup.log` | 已开票，sleep 中 |
| 15m | 900s | `longevity/15m.nohup.log` | 已开票，sleep 中 |
| 30m | 1800s | … | 已开票，sleep 中 |
| 60m | 3600s | … | 已开票，sleep 中 |
| 3h | 10800s | … | 已开票，sleep 中 |
| 6h | 21600s | … | 已开票，sleep 中 |
| 12h | 43200s | … | **按 60m+ 结论直接验收，不等待**（见下） |

每档独立 `ticket_snapshot.json` + sleep 后全量 `longevity_result.json`。  
**注意**：7 档并行 sleep 共用同一 secret 账号开票，仅验证 TTL；不用于生产票池设计。

### 5.1 验收结论（2026-07-24）

| 档位 | 结果 | 失败点 |
|------|------|--------|
| 10m | ✅ | — |
| 15m | ✅ | — |
| 30m | ✅ | — |
| 60m | ❌ | sleep 后 `f/conversation` CF403 |
| 3h | ❌ | 同上 |
| 6h | ❌ | 同上 |
| **12h** | **❌（推断验收）** | 与 60m+ 同模式；**不等待** sleep 结束，直接按失败档归档 |

**生产票龄决策**：**上限 30m**（对齐 longevity ✅ 档）。Rust `ticket_pool` 默认 TTL=300s 为池内刷新周期，与「最长可用票龄 30m」不矛盾——应用层须在 30m 内消费或重开。

检查：

```bash
ssh panda 'tail -5 /root/gptimage/data/runlogs/spa_repro/sentinel-ticket-validation-20260723/longevity/10m.nohup.log'
ssh panda 'cat .../longevity/10m/longevity_result.json'
```

---

## 6. 对 CF403 / IP 漂移 / 封号叙事的修正

结合 `P-sentinel-ticket-ablation-20260723` 与本验证：

| 旧叙事 | 新证据 | 工程调整 |
|--------|--------|----------|
| 票必须同出口开票+用票 | cross_ip 5 轮 + conc30 全过 | per-call 开票仍最稳；**不强制**消费绑开票 IP |
| CF403 = IP 坏了 | 同 IP 多账号表现不同；cross_ip 用票成功 | CF 仍按 **账号×路径×并发** 判定 |
| 同票可缓存省 finalize | 立即复用 CF403；60s 可过；120s 本 run CF403 | **Rust 票池 TTL=300s**；单票复用仅实验占位 |
| 高并发必须浏览器票池 | 同账号 parallel finalize×2 OK；HTTP conc10×3 OK | chrometicket **后置** |

详见 `docs/17-cf403-and-egress.md` §「票与出口关系」与 §「poll CF403：每号独立 IP 仍会被打」。

### 9. own-proxy conc10（2026-07-24）

```bash
python scripts/_tmp_run_sentinel_validation.py cross-concurrent-own-proxy --from-round 1 --to-round 1
```

| 配置 | 值 |
|------|-----|
| 每号 proxy | 自有（开票 + 用票 + poll 全程） |
| egress | `--unique-egress`，10 路各不相同 |
| preflight | **默认关**（`--preflight` 可选） |

| 指标 | mixed conc10（历史） | own-proxy conc10 R1 |
|------|---------------------|---------------------|
| 典型失败 | 同段 egress 叠加 + poll CF | 仍有个别 IP poll CF（2/10） |
| enricoalfred | R4 mixed 失败 | 本号 IP 成功 |

结论：**每号一 IP 必要不充分**；见 `17` §poll 专节。

### 10. poll swap + 错峰稳定性（2026-07-24 晚）

落地：`image_poll_cf_swap_retry_max=5`、验证套件接 `_resolve_image_urls_with_poll_cf_swap_retry`、`_upload_image` 对 Azure 503 退避。

| 形态 | 成功率 | 说明 |
|------|--------|------|
| 无错峰连续 conc10 | 1/10 | 图生图 **Azure Blob ingress** `ServerBusy`（仍是 OpenAI `/backend-api/files` 链路） |
| 错峰 2s + 冷却 90s + swap≤5 | **10/10** | poll CF 经 swap 救回；无 Azure 503 |

编排：

```bash
python scripts/_tmp_run_sentinel_validation.py cross-concurrent-own-proxy --from-round 1 --to-round 1
python scripts/_tmp_run_sentinel_validation.py summarize
```

证据：`sentinel-ticket-validation-20260723-production/`（`cross_concurrent_report.json`、`events.jsonl`、`production_summary.json`）。

### 11. 稳定性复测 R2（2026-07-24 上午，conc9）

号池仅 **9** 个 `image_schedulable` 且 `proxy_egress_ip` 不重复（conc10 选号失败 `got 9`），以 **workers=9** 复跑 own-proxy。

| 指标 | R2 conc9 |
|------|----------|
| 成功率 | **9/9** |
| 错峰 | 2s（`stagger_ms=i*2000`） |
| poll CF / Azure 503 | **0** |
| 耗时 P50 | ~43s |
| 耗时 P95 | ~101s（`qaflowud630wbo2a` 尾延迟） |
| 耗时范围 | 20.5s – 101.3s |

此前 poll CF 的 `felicitypamela` @ `92.113.231.203` 本轮 **文生图成功**。

### 12. 六号 dup_binding 换绑 + conc10 R3（2026-07-24 中午）

**问题**：6 账号共用 `82.21.231.148:7462` → `image_schedulable` 11/17（`dup_proxy_binding`）。

**处置**：`scripts/_tmp_rebind_emails_unique.py` 各换独立 Webshare → **生图可用 17/17**。

| 账号 | 新 egress |
|------|-----------|
| qaflowgq5wyuxhe9 | 92.113.246.215 |
| qaflow0ytb7bbp0z | 92.113.241.215 |
| qaflowyi59i282fx | 104.252.149.121 |
| qaflowxwy83tivv5 | 45.39.75.27 |
| barnettregina | 92.113.241.223 |
| qaflowfbdb3ovksr | 92.113.236.206 |

**conc10 R3**（换绑后）：**10/10**；unique egress 选号成功；错峰 2s；P50 ~44s / P95 ~53s；`qaflow0ytb7bbp0z` poll CF swap 1 轮后成功。

---

## 8. Phase-2（2026-07-23 晚）

### 8.1 额度为何 dashboard 不掉？

验证脚本直连 HTTP **绕过** `AccountService.mark_image_result()`，本地 `quota` 缓存未递减也未 `fetch_remote_info`。

**修复**：每次生图成功后 `mark_image_result(True)` + `fetch_remote_info("sentinel_validation_post_image")`，日志事件 `quota_sync`。

`verify-quota` 样例（`qaflowxho1z6hynk`）：

| 字段 | 值 |
|------|-----|
| quota_before（本地缓存） | 24 |
| quota_after_mark | 23 |
| quota_remote（`/me`+init 真值） | **4** |

说明：本地缓存与远程严重漂移；**必须以 remote 为准**写回 DB，dashboard 才会对齐。

证据：`sentinel-ticket-validation-20260723-phase2/verify_quota_report.json`

### 8.2 reuse-gap v2

- 3 轮 × 新账号；轮间冷却 300s
- 间隔档位：60/120/180/240/300/360s
- 每步同步额度；出错即停
- 证据目录：`.../sentinel-ticket-validation-20260723-phase2/`

### 8.3 串行/并发复测（待 reuse-gap 完成后）

- 5 轮串行：cross_session + cross_ip，交替 `text_large` / `image_edit`
- 3×10 并发：偶数 worker `text_large`，奇数 `image_edit`
- 全部带 `quota_sync`

### 8.4 Rust 票池

`gptimage-gateway-rs/crates/ticket_pool`：`deposit` / `acquire` / `refresh`，默认 **TTL=300s**；`ReusePolicy` 占位（立即/间隔复用未启用）。

---

## 7. 复现命令

```bash
python scripts/_tmp_run_sentinel_validation.py verify-quota
python scripts/_tmp_run_sentinel_validation.py reuse-gap --gaps 60,120,180,240,300,360
python scripts/_tmp_run_sentinel_validation.py cross-serial --from-round 1
python scripts/_tmp_run_sentinel_validation.py cross-concurrent --from-round 1
```
