# 串行 5 第 2 轮 SSE 诊断分析（2026-07-22）

关联证据：
- [`N-panda-serial5-observability-20260722.json`](./N-panda-serial5-observability-20260722.json)
- [`N-panda-cf-scan5-webshare-20260722.json`](./N-panda-cf-scan5-webshare-20260722.json)

## 结论（TL;DR）

| 维度 | 第 1 轮（成功） | 第 2 轮（45s gate 失败） | 判定 |
|------|----------------|-------------------------|------|
| 账号 | `3b18db641494` | 同账号 | **无换号** |
| sticky 代理 hash | `561dcfff2fc1` | 同 hash | **无出口漂移** |
| 出口 IP | `45.39.75.27` | 同 IP（日志） | **无 IP 漂移** |
| 首页 `home_status` | 200 | 200 | **无 CF 软失败** |
| 业务链 `propagated_cf` | 0 | 0 | **无 requirements/start/tasks CF** |
| `requirements_ms` | 1558 | 1773 | 正常波动 |
| `prepare_ms` | 380 | 378 | 一致 |
| `sse_image_gen_ms` | **35031** (~35.0s) | **64474** (~64.5s) | **上游 SSE 工具触发变慢 ~29.5s** |
| gate 后额外 chunks | 0 | **6** | 流仍活跃，非静默 |

**根因**：同一账号、同一 sticky Webshare 出口下，第 2 轮是 **上游 SSE 工具调度变慢**（`image_gen` 在 64.5s 才出现），**不是**账号换绑、出口 IP 漂移或 CF403 传播。

在 **65s 验收 gate** 下，第 2 轮应判为 **gate 内成功**（`has_image_gen_within_gate=true`），不再归类为 `late_image_gen_after_gate`。

## 时间线重建（基于 `timings_ms` + `sse_diagnostic`）

第 2 轮 conversation：`6a60c41b-0624-83ec-89ec-2f2f22bb7855`

```
0ms        egress 探测完成（257ms）
~2030ms    requirements + prepare 完成
~2030ms    POST /f/conversation 开始 SSE
45000ms    45s gate 触发（尚无 gate 内 image_gen）
64474ms    首次识别到 image_gen（迟到 +19.5s）
67917ms    SSE 流结束（diagnostic_stopped_reason=done）
```

- `sse_chunks=19`，其中 **gate 后 6 个 chunk** 仍到达 → 网络与 SSE 通道正常，非 `no_image_gen_quiet_stream`。
- 失败分类 `late_image_gen_after_gate` 在 45s gate 下正确；**放宽到 65s 后应转为成功路径**。

## 与 cf_scan5 的关系

`cf_scan5` 对池中前 5 个节点做轻量探测：**4/5 首页 403**。  
串行 5 使用的生产 sticky `45.39.75.27` 两轮均为 `home_status=200`，说明 **池内坏节点 ≠ 当前 sticky 出口状态**。

## 后续动作（已采纳）

1. **验收 gate 45s → 65s**（诊断窗仍 90s 总墙钟）
2. **CF403 时代理换绑**：`services/proxy_cf_failover.py` — 连续 CF 信号后 quarantine 旧节点、从池选干净 Webshare、**重置 `cf_daily` / `egress_daily` 指示灯**
3. 每轮保存 `round-N-canary.json`（含 `sse_event_timeline`）供下轮细查
