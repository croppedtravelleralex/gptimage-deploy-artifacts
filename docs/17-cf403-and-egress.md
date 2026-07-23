# 17 — CF 403 / 边缘拦截：成因、历史、可做与不可做

最后更新：2026-07-21（Asia/Shanghai）

## 一句话

`cloudflare_or_edge_html_block` / HTTP 403 HTML challenge 是 **Cloudflare 边缘对出口 IP + TLS/行为** 的拦截，不是本仓缺某个 Header 就能「协议绕过」。可持续策略是 **好出口 + 限同 IP 并发 + 撞 CF 立刻换号**；协议层只能逼近 Web，不能根除。

## 现象与识别

| 信号 | 含义 |
|------|------|
| `cloudflare_or_edge_html_block` | 上游返回 HTML challenge / 拦截页 |
| `cf_edge_block:` / `is_cf_edge_chat_error` | 代码侧归类为边缘拦（≠ token 失效） |
| 用户文案曾误报 `upstream image connection failed` | 旧逻辑把含 `proxy` 的 CF 文案误判为 TLS；已改为优先 CF 文案 |
| 生图空挂 ~300s 后 `ImageStreamCancelledError` | 常为 soft-bootstrap / 同号空转拖到任务硬超时，而非「全局并发坏了」 |

常见触发点：`bootstrap`（首页）、`chat_requirements_finalize`、`/backend-api/f/conversation` SSE。

## 根因（按证据强度）

1. **出口 IP 信誉**（最强）：共享 Webshare / 机房 IP 被标；同 `proxy_binding` 多号突发更容易撞。
2. **Panda 宿主机直连必炸**（已验证）：`43.156.233.219` 可过 Camoufox 注册页，空代理刷 `/backend-api`/`/me` → CF 403。见 `02`/`16`（2026-07-20 canary）。
3. **协议栈 ≠ 真浏览器**：`curl_cffi` impersonate 仍间歇 403；并发放大。
4. **历史软失败拖死**：IMAGE 路径 bootstrap CF 曾 soft 回退默认 PoW，不换号，烧到硬超时。

## 以前有过吗？

**有，长期反复。**

- 2026-07-20：Panda IP canary → 直连 backend CF 403（`02`/`06`/`16`）。
- Olivia `/me`：全头间歇 403，轻量头更稳——同类边缘拦。
- 文本路径早有 `text_stream_cf_failover`；生图此前缺对等换号，表现为暴死/空挂。
- 近 24h（部署缓解后采样）：仍可见少量 `bootstrap_soft_failed` / `cloudflare_or_edge_html_block`（偶发，非清零）。

## 「协议绕过」裁决

| 手段 | 结论 |
|------|------|
| 改 Header / 对齐请求序 | 降误伤；IP 已被标时无效 |
| `curl_cffi` Chrome 指纹 | 已在用；仍间歇 CF |
| soft-bootstrap / 默认 PoW | **不能**过 challenge；IMAGE 已改为 `soft_fail=False` 硬失败以便换号 |
| FlareSolverr 解挑战 | 脆弱，**不纳入**生产根方案 / Rust 重写 |
| Camoufox 真浏览器拿会话 | 注册/验号更稳；API 反代仍受出口限制 |
| **协议层稳定绕过 CF** | **不可行 / 不可承诺** |

## 已落地缓解（2026-07-21）

- IMAGE：`image_stream_cf_failover`（demote + 换号，`pre_conversation_max_attempts`↑）。
- IMAGE bootstrap：`_ensure_bootstrap(soft_fail=False)`，避免空转至 ~300s。
- 同 `proxy_binding` 同时生图默认 ≤1（`image_binding_inflight_max`）。
- `submit_start_min_interval_ms`≈1500；硬超时组件收紧（pre≈45、poll≈120）。
- 文案：CF 优先于 connection failed。
- **Poll 快失败（同日补）**：`_poll_image_results` 对 conversation/tasks 连续 CF（默认 `image_poll_cf_abort_streak=2`）立刻抛 `cloudflare_or_edge_html_block`（`cf_abort`），纳入现有 `image_stream_cf_failover`；tasks 遇 CF 后本轮跳过后续 tasks。目标是**偶发 + 快失败/换号**，不是 0 次 403。
- **号池 CF 灯（被动）**：账号 `cf_daily` 仅在真实业务成功/CF/生图失败路径累计；**不做**定时主动刷 `/me`/`/tasks` 探 CF。

## 根除？

**不能 100% 根除**（策略在 CF/OpenAI 侧）。业务目标应改为：发生率低到「偶发 + 自动换号恢复」，用户无感。

继续提高：独立高信誉出口数量、坏 IP 黑名单、浏览器 bootstrap 与 sticky 同出口绑定（见 `09`/`12`）。

## 相关

- `12-protocol-gap-vs-web.md`（协议差距）
- `16-camoufox-stable-pipeline.md`（注册出口）
- `02-current-state.md` / `06-handoff.md`
- 代码：`services/protocol/conversation.py`、`services/openai_backend_api.py`、`services/account_service.py`
