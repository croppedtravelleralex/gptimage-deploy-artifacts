# 交接摘要

日期：2026-07-28

**执行计划**：根目录 [`plan.md`](../plan.md)（**THROUGHPUT-10**）  
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
- **验收**：mixed conc10 **10/10** + conc20 **20/20**；`b64_json` vs `url` 对照见 `02` §六号池（同步回包 1.8MB vs 0.7KB）
- **监控**：`/health` 三件套正常；call log phase 竞态已修并部署

## 本周（见 `plan.md` / `30-throughput-10-plan.md`）

| 优先级 | 任务 | 状态 |
|--------|------|------|
| **P0** | **THROUGHPUT-10**：submit_workers=10、sS=10、队列背压 requeue、带宽追踪 | 代码已落地 |
| **P0** | **住宅 20 + 机房 100 双池** + 分配时 CF live probe | secret 已装；**20/20 Panda 探活通过**（`captures/infra/webshare20-panda-probe-bandwidth-20260727.md`） |
| **P0** | **溯源修复** P29-1/P29-2 + conc10/15/20 验收 | **conc10/15/20 已通过** |
| **P0** | call log phase 竞态 + `prompt_enhance` 同步透传 | **已部署** `deploy/codex/phase-log-fix-20260727` |
| **P0** | 续轮询 trace 终态 / `delivered` 语义修复 | **已部署** `deploy/codex/resume-trace-fix-20260728` |
| **P0** | 额度 60s 循环 + `mark_image_result` 事件刷新 | 已接线 |
| P1 | Ops health：`slot_topology` / `proxy_pool` / `bandwidth` | 已暴露 |
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
| **Webshare20 探活 / Panda 带宽** | `captures/infra/webshare20-panda-probe-bandwidth-20260727.md` |
| 待办池 | `04-improvement-backlog.md` |
| 协议 HAR | `captures/spa/README.md` §Camoufox |
| 调度 | `21-image-scheduling-and-pipeline.md` |
| **槽位 / 释槽 / Rust 路线** | `26-slot-lifecycle-rust-roadmap.md` |

> 07-20～07-24 详细时间线已迁入 `logs/2026/2026-07.md`，本文件不再重复。
