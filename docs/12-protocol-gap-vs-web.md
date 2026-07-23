# 12 — 逆向协议相对 ChatGPT Web 的差距

最后更新：2026-07-22（纯 HTTP 生图待办入 `20`）

## 证据边界

- SPA HAR（gitignore）含 Create Image UI、`spa-next-de` 上传/图生图、search 尝试；专页见 `docs/captures/spa/`。
- 出口：Clash + 固定号；差 IP Webshare 本机暖机现 NET_RESET（`A-badip-warm`）；**不**推翻 `17`。
- 生图双路径：NL=`[]`/`image_gen`；UI=`picture_v2`+conduit（生产保持）。
- 上传：SPA 现用 `process_upload_stream` + 附图 `sediment://`；仓内部分仍 `/uploaded` + `file-service://`。

## 文本 / 生图 / 搜索

| | 文本 | Create Image UI | 搜索 ON | 反代现网 |
|--|------|-----------------|--------|----------|
| hints | `[]` | `["picture_v2"]` | `["search"]`（HTTP 已证） | 图=`picture_v2`；搜=`search` |
| 上传指针 | — | 附图 `sediment://` | — | 多路径 `file-service`/`sediment` |

## 已验证（摘）

- 续聊/消融、暖机 Clash、cookie 剥离、Create Image UI、上传/sediment、HTTP search 开/关、错误码 v1、bench3、差 IP 失败面

## 生图不触发 image_gen（2026-07-21 实测，见 `captures/spa/G-*`）

- Panda 冷却号跨 3 个出口 IP 段（`82.21.x`/`104.252.x`/`92.113.x`）× 多设备指纹，`picture_v2`（conduit+`["picture_v2"]`+`X-Conduit-Token`）与空 hints 文本 shape **均不进 `image_gen`**，上游把 `{"size","n","prompt"}` 当文本流回。
- 生产 `/v1/images/generations` 同步超时（`HTTP 000`）；服务端 `image_poll_timeout ... last_task_error: null`（**非 CF**，是 conversation 无图）。
- 历史定性：主缺口曾在 **Sentinel 凭证层**（现网 finalize=`proofofwork`/`turnstile`；SSE 必带 Turnstile 头；当时本仓 Turnstile VM 对现网 `dx` 空串）。Body top-level 与 HAR 基本一致。
- **当前结果**：纯 HTTP 已正式发布并完成单单元；串行 5 在 2/5 止损。新旧 IP 同账号/同 fp/同 shape A/B：新 IP `2/2` 无 CF 成功，旧 IP 本次无 CF但下载 `503 ServerBusy`。严格旧 IP 归因门槛未满足，CF403 更像间歇性 edge/endpoint/timing，IP 不是已证唯一变量。证据 `captures/spa/{J,K,L}-*.json`。

## 仍存差距

1. **P0 生图**：正式发布与单单元验收已完成；剩余 CF 降压观察及人工放行后的串行 5 / 并发 4 → `20` / **PROTO-PURE-HTTP**
2. **生产改造**（`04` **PROTO-REFACTOR**）：上传仍偏旧链（`/uploaded` / 部分 `file-service://`），SPA 已是 `process_upload_stream` + `sediment://`
3. SPA Search UI 点选未稳定落到 `["search"]`（菜单「Look something up」）；HTTP 已够用
4. Temporary Chat HAR / SSE 事件字典 / 栈 ADR / Arkose 拆分（锦上添花）
5. Clash curl_cffi TLS 抖动；差 IP 浏览器暖机不稳；CF 见 `17`

## 相关

- `20-pure-http-image-sentinel-todo.md`（**纯 HTTP 生图待办真相源**）
- `04-improvement-backlog.md`（**PROTO-PURE-HTTP** / **PROTO-REFACTOR**）
- `19-protocol-full-reverse-catalog.md`
- `docs/captures/spa/{A,B,C,D,E,F,G}-*-20260721.md`
