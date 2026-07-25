# 交接摘要

日期：2026-07-25

**执行计划**：根目录 [`plan.md`](../plan.md)（**SLOT-RUST Layer 1**）  
**文档导航**：[`docs/README.md`](README.md)  
**槽位 / Rust**：[`26-slot-lifecycle-rust-roadmap.md`](26-slot-lifecycle-rust-roadmap.md)  
**看门狗矩阵**：[`27-pipeline-watchdog-monitoring-matrix.md`](27-pipeline-watchdog-monitoring-matrix.md)  
历史流水：[`docs/logs/2026/2026-07.md`](logs/2026/2026-07.md)

## 当前生产（摘要）

- **号池**：`total=19`；**`image_schedulable=16`**；**1IP1号**（`unique_egress=19`）；`dispatch_hot_only=false`
- **Pipeline 槽**：**账号 inflight（Python）在 sS 槽之前占用**；Rust `.so` 仅 trace/dispatch gate，**不持槽**。见 `26` §2
- **conc10（2026-07-25）**：最佳历史 10/10；当日回归 4/10（CF 出口）+ 曾 0/10（inflight 泄漏，已修 Python 释槽路径）
- **取号**：`account_queue` ~0.1% 占墙钟；`dispatchable=6` = humanlike `image_next_ok_ts` 冷却（16−10 参与号）

- **新号**：`16` + `outlook_camoufox_stable_register.py` → `identity_isolated` → 成熟后 `verified_ready`
- **生图栈**：curl_cffi + VM Turnstile + sticky Webshare；**不做**真 Chrome 开票（`22` §9）
- **验收**：STAB-A1 API serial5 **5/5**；multiacct conc10 **10/10**（`STAB-conc10-20260724T110344Z`）；**PROD serial10 10/10** + **PROD conc10 10/10**（`PROD-conc10-20260724T150152Z`；对比：conc10 新增 sS 8.6% + task_queue 9.7%，SSE 仍 ~78%）
- **CF**：`17` — 换出口/暖号/探活；非 FlareSolverr 根方案
- **暖号**：`account_warmup` 已部署；`GET /api/ops/warmup/status`
- **可观测性**：VERIFY-001 pass；call log 阶段 chips + token/traffic

## 本周（见 `plan.md`）

| 优先级 | 任务 |
|--------|------|
| **P0** | SLOT-RUST Layer 1：SlotLedger + sS 75s + watchdog + 基线横评 |
| P1 | `failure_retry` API；pre_ticket 全路径；Linux `.so` 部署 Panda |
| **P2** | 账号/基础设施 Rust（**N>50 后**） |
| 不做 | 真 Chrome 票池 · 全量 SSE Rust |

## 禁止

Panda build/scp 发业务码 · Panda IP 作 backend 出口 · 宣称协议绕过 CF · 浏览器生图数据面

## 专题索引

| 主题 | 文件 |
|------|------|
| 池面事实 | `02-current-state.md` |
| 待办池 | `04-improvement-backlog.md` |
| 协议 HAR | `captures/spa/README.md` §Camoufox |
| 调度 | `21-image-scheduling-and-pipeline.md` |
| **槽位 / 释槽 / Rust 路线** | `26-slot-lifecycle-rust-roadmap.md` |

> 07-20～07-24 详细时间线已迁入 `logs/2026/2026-07.md`，本文件不再重复。
