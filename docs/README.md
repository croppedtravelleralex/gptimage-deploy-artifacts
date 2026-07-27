# 文档导航

最后更新：**2026-07-27 01:00**

**冲突优先级**：代码/命令结果 > `28`（调度/队列/槽位专项审计） > `29`（生产溯源审计） > `02-current-state.md` > `04-improvement-backlog.md` > `06-handoff.md` > `logs/`

## 三秒上手

| 你要… | 读 |
|--------|-----|
| **现在在干什么** | 根目录 [`plan.md`](../plan.md) |
| **生产池面事实**（10 并发全貌） | [`02-current-state.md`](02-current-state.md) |
| **接着上次干** | [`06-handoff.md`](06-handoff.md) |
| **待办池** | [`04-improvement-backlog.md`](04-improvement-backlog.md) |
| **每次部署前必读** | [`29-prod-provenance-audit-20260726.md`](29-prod-provenance-audit-20260726.md) |

## 按目录

| 目录 | 内容 |
|------|------|
| [`plans/`](plans/README.md) | 编号方案文档（08–24）：生图、协议、调度、运维 |
| [`reference/`](reference/README.md) | 部署、额度、CF 工具、SSE 语义等参考 |
| [`captures/`](captures/spa/README.md) | SPA 逆向 HAR 专页、验收与 benchmark 证据 |
| [`archive/`](archive/README.md) | 历史交接、已收尾 sprint、详细计划归档 |
| [`logs/`](logs/README.md) | 按月流水（只追加，非当前事实） |

## 高频专题（文件仍在 `docs/` 根目录）

| 专题 | 文件 |
|------|------|
| 新号 Camoufox 产出 | `16-camoufox-stable-pipeline.md` |
| CF 403 裁决 | `17-cf403-and-egress.md` |
| 纯 HTTP 生图红线 | `20-pure-http-image-sentinel-todo.md` |
| 用票链路 / 不做真 Chrome | `22-ticket-image-pipeline-and-go-spike.md` §9 |
| 调度与双槽 | `21-image-scheduling-and-pipeline.md` |
| **槽位释槽 / Rust 演进** | `26-slot-lifecycle-rust-roadmap.md` |
| 看门狗 / 监控矩阵 | `27-pipeline-watchdog-monitoring-matrix.md`（多处 ✅ 已被 `28` 推翻） |
| **调度/队列/槽位审计** | `28-scheduling-queue-slot-audit-20260726.md` ← **读这份再动调度代码** |
| **生产溯源审计** | `29-prod-provenance-audit-20260726.md` ← **每部署前必读** |
| **THROUGHPUT-10 架构** | `30-throughput-10-plan.md` ← 双池/双槽/实时额度/带宽设计 |
| **前端首访/图片加速** | `25-frontend-performance-plan.md` |
| 协议逆向 HAR 链 | `19-protocol-full-reverse-catalog.md` → `captures/spa/` |
| **架构报告**（2026-07-27） | 详细结论已汇入 `02-current-state.md` 一～九号池；原始分析见各 `arch-*` agent 报告 |

## 已归档 / 勿重复维护

| 原文件 | 去向 |
|--------|------|
| `23-image-observability-…` | 归档 → `archive/sprints/23-…`；根文件为 stub |
| `24-image-stability-…` | 执行 → **`plan.md`**；详情 → `archive/plans/24-…` |

## 更新纪律

1. 状态变：先改 `02` 摘要，再改 `plan.md` 勾选。
2. 证据：落 `captures/spa/`，文档只写结论+链接。
3. `logs/` 只追加，不改旧月。
