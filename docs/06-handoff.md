# 交接摘要

日期：2026-07-26

**执行计划**：根目录 [`plan.md`](../plan.md)（**SLOT-RUST Layer 1**）  
**文档导航**：[`docs/README.md`](README.md)  
**槽位 / Rust**：[`26-slot-lifecycle-rust-roadmap.md`](26-slot-lifecycle-rust-roadmap.md)  
**看门狗矩阵**：[`27-pipeline-watchdog-monitoring-matrix.md`](27-pipeline-watchdog-monitoring-matrix.md)  
**代码审计**：[`28-scheduling-queue-slot-audit-20260726.md`](28-scheduling-queue-slot-audit-20260726.md)  
**溯源审计**：[`29-prod-provenance-audit-20260726.md`](29-prod-provenance-audit-20260726.md)  
历史流水：[`docs/logs/2026/2026-07.md`](logs/2026/2026-07.md)

## 当前生产（摘要）

- **号池**：`total=19`；**进调度 17** / **生图可用 17**；`proxy_cf_ok` **19/19**；`proxy_binding_max_accounts=**2**`（单 egress 最多 2 号）
- **未进调度**：`philliphicks`（status=异常 quota=0）；`enrico`（`identity_isolated` 已出调度）
- **Pipeline 槽**：**账号 inflight（Python）在 sS 槽之前占用**；Rust `.so` 仅 trace/dispatch gate，**不持槽**。见 `26` §2
- **AUDIT-28（2026-07-26 21:07 上线）**：11864 行修复已上线，运行时确认生效（watchdog 独立线程 + `force_release_expired=true`、`ss` 池计数恢复、`cpu_budget_source=cgroup_v2`）。**但上线后零生图流量验证**，B 类问题仍只有静态确证。见 `29` §1/§4
- **⚠ 溯源断裂（`29`）**：① AUDIT-28 的 28 个文件在 Panda 上**悬在 index**，HEAD 停在快照分支 —— 一次 `git reset --hard` 即静默回滚；② **19 个文件不在任何 commit 里**（16 个与本地未提交工作树相同、`domain_intel.py` 505 行本地不存在、`yumail_otp.py` prod 落后于已提交修复）
- **CF 隔离事故（2026-07-26）**：`webshare_cf_scan` `auto_quarantine` 将池内 100 节点标 `cf403_scan` → 生图可用曾跌至 **0**；已修 `proxy_cf_eligibility`（账号 `proxy_cf_ok` 缓存优先于批量隔离）+ 扫描跳过已绑定 endpoint。见 `17` §「批量 scan 隔离」
- **⚠ cfscan 已关停**：`enabled=false` + `auto_quarantine=false` + `block_unscanned_for_schedule=true` —— 现有号靠 `proxy_cf_ok` 缓存短路不受影响，但**新代理池入池时会被直接挡住**。见 `29` §7
- **⚠ 养号停摆**：`resolve_binding_matrix` hash fallback 撞到无周末档预设，**17/19 账号 `slot_allowed=False`**（24h 407 次），A1-6 未修完。见 `29` §5

- **新号**：`16` + `outlook_camoufox_stable_register.py` → `identity_isolated` → 成熟后 `verified_ready`
- **生图栈**：curl_cffi + VM Turnstile + sticky Webshare；**不做**真 Chrome 开票（`22` §9）
- **验收**：STAB-A1 API serial5 **5/5**；multiacct conc10 **10/10**（`STAB-conc10-20260724T110344Z`）；**PROD serial10 10/10** + **PROD conc10 10/10**（`PROD-conc10-20260724T150152Z`；对比：conc10 新增 sS 8.6% + task_queue 9.7%，SSE 仍 ~78%）
- **CF**：`17` — 换出口/暖号/探活；非 FlareSolverr 根方案
- **暖号**：`account_warmup` 已部署；`GET /api/ops/warmup/status`
- **可观测性**：VERIFY-001 pass；call log 阶段 chips + token/traffic

## 本周（见 `plan.md`）

| 优先级 | 任务 |
|--------|------|
| **P0** | **THROUGHPUT-10**：双槽/调度/队列重构，跑满 10 并发，>10 进完整排队；瓶颈收敛到 Panda 带宽 |
| **P0** | **住宅代理入池**：20 个 Webshare `staticresidential`（IP 白名单 43.156.233.219，无带宽限制）作生图优先层；100 机房节点质量升级后复评 |
| **P0** | **溯源修复**（`29`）：Panda staged→commit；本地 22 commit + 16 漂移文件推源码远端 |
| **P0** | 额度实时化 + 探针扩容（调度池额度准确性） |
| P1 | A1-6 补完（`resolve_binding_matrix` 周末档）；`yumail_otp.py` 补发；`domain_intel.py` 定性 |
| **P2** | 账号/基础设施 Rust（**N>50 后**） |
| 不做 | 真 Chrome 票池 · 全量 SSE Rust |

## 禁止

Panda build/scp 发业务码 · Panda IP 作 backend 出口 · 宣称协议绕过 CF · 浏览器生图数据面

> **部署链路（唯一合法）**：本机 Windows/WSL 编译 → `git push` GitHub → Panda `git pull` → 重启。
> 禁止在 Panda 编译；禁止 `scp` / `docker cp` 上传代码或二进制。

## 专题索引

| 主题 | 文件 |
|------|------|
| 池面事实 | `02-current-state.md` |
| 待办池 | `04-improvement-backlog.md` |
| 协议 HAR | `captures/spa/README.md` §Camoufox |
| 调度 | `21-image-scheduling-and-pipeline.md` |
| **槽位 / 释槽 / Rust 路线** | `26-slot-lifecycle-rust-roadmap.md` |

> 07-20～07-24 详细时间线已迁入 `logs/2026/2026-07.md`，本文件不再重复。
