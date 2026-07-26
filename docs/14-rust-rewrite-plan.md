# 14 — Rust 重写索引（RUST-001）

最后更新：2026-07-25  
状态：**Phase A 已接线** + **鉴权/UI/简易后端** + **Phase B 契约层** + **本仓 Layer 1 SlotLedger 待做**；生图运行时默认关（`IMAGE_ENABLED=0`）

> **本仓槽位与释槽**：`image_inflight` / sS 槽均在 **Python**；Rust `.so` 仅 trace + dispatch gate。详见 [`26-slot-lifecycle-rust-roadmap.md`](26-slot-lifecycle-rust-roadmap.md)。

## 权威位置

施工、进度与 Phase A→E 路线以独立仓为准：

**`D:\SelfMadeTool\AutoRegister\gptimage-gateway-rs`**

| 文档 | 用途 |
|------|------|
| `plan.md` | **进度快照 + 重写路线（权威）** |
| `HANDOFF.md` | 接手入口 |
| `docs/00-contract.md` | 协议契约 / session 表 / error_class |
| `docs/17-operator-guide.md` | 拓扑 / 故障树 / bringup |
| `docs/18-test-matrix.md` | 测试矩阵与签字栏 |
| `docs/13-perf-baseline-compare.md` | 相对本仓基线的预估对照 |
| `docs/21-auth-and-ui.md` | 鉴权 + Web UI + `IMAGE_ENABLED` |

本仓生产基线与预估表：[`13-performance-and-rewrite-estimate.md`](13-performance-and-rewrite-estimate.md)。  
CF403 / 出口：[`17-cf403-and-egress.md`](17-cf403-and-egress.md)（**号池侧**；不阻塞 Rust 接线认定）。

## 路线摘要

```text
A    Rust 编排 + curl_cffi helper   ✅ 已接线 :8013
A+   鉴权 + Web UI + 简易后端        ✅
B    协议契约（fixtures/edits/estuary） ✅ 契约层；生图运行时 ⏸️
C    选号 / inflight / admission          ← 与本仓 `26` Layer 1 SlotLedger 对齐
D    RCA / llm_ops / error_class 对齐
E    R2 生产 canary（另立项；公网仍 8012 直至立项）
```

## 摘要

- **新项目**，非原地替换 `chatgpt2api-local`。
- Phase A+：鉴权 + `web/` dashboard + 简易后端（对话/管理）；生图默认 `IMAGE_ENABLED=0`。
- Phase B 契约：fixtures 全量 + `image_contract`；生图/edits/estuary **运行时后置**。
- 全量：选号/admission + RCA；**永久不做**注册机与 FlareSolverr。
- 红线：禁 Panda `cargo build`；estuary Bearer；SSE 禁止 `post_ready=15s`；正式发布 git push→pull。

## 旧文

2026-07-19 单仓 sidecar 草案已由上述独立仓 + Phase A→E 取代；细节以 `gptimage-gateway-rs/plan.md` 为准。
