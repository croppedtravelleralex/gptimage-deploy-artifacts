# P — Sentinel 票生命周期专项实验（2026-07-23）

| 项 | 值 |
|----|-----|
| 账号 | `qaflowakjewai6ps@proton.me` / `92.113.246.176` |
| alt 出口 | `philliphicks336926@outlook.com` / `82.29.223.111` |
| 证据 | `docs/captures/spa/P-sentinel-ticket-ablation-20260723.json` |

**全部 7 批已跑完**（分批合计 ~22 min，非 30–60 min 单次长跑）。

## 结果总表

| Batch | 结果 | 关键发现 |
|-------|------|----------|
| batch1 | ✅ | baseline OK；cross_session（同 IP 新 session）**OK**；同票连打 2×SSE → 第 2 次 **403 CF** |
| batch2 | ✅ | **cross_ip OK**（176→111）；**cross_session+cross_ip OK** |
| batch3 | ✅ | delay **30s** OK |
| batch4-60 | ✅ | delay **60s** OK |
| batch4-120 | ✅ | delay **120s** OK |
| batch4-300 | ✅ | delay **300s** OK |
| batch5 | ✅ | 同账号 **并行 finalize 2/2** + **并行用票 2/2** |

## 结论（实测）

1. **跨 IP**：**可以**。开票 `92.113.246.176`，用票 `82.29.223.111` 仍 200 + `conversation_id`。
2. **跨 session**：**可以**（同 IP 或换 IP 均可）。
3. **同票复用**：同 session **立即**第二次 SSE → CF403（更像连打风控，不能单独证明「票作废」）。
4. **TTL**：本探针下 **≥300s** 仍可用；上界未测到（`first_fail` 为空）。token 非 JWT（`jwt_exp_unix=null`）。
5. **浏览器多开票**：**不需要**。同账号 curl_cffi **并行 2 路** finalize + 用票均成功；HTTP 并发不必多开浏览器 chrometicket 池。

## 生产建议

- 当前 per-call 开票仍是最稳默认（避免连打 CF、简化归因）。
- 若做票池：须记录开票出口，但实测**不强制**消费同 IP；`max_age` 建议 **<300s**（本批下界），并禁止同票双 SSE。
- 多账号并发：扩 **finalize 并行度** 即可，无需浏览器票池（纯 HTTP 路径）。
