# panda IP 链式代理 + Camoufox 复测 — 2026-07-21

对照上一轮：panda 容器内 `curl_cffi` **直连**时，`chat-requirements/prepare` 连续 CF HTML 403。

## 拓扑

```text
本机 Camoufox / curl_cffi
  → socks5://127.0.0.1:18080
  → ssh -D 18080 panda
  → 出口 IP 43.156.233.219（与 panda 直连相同）
```

建隧道：

```bash
ssh -D 18080 -N -o ExitOnForwardFailure=yes panda
```

脚本：`scripts/_tmp_spa_camoufox_via_panda_socks.py`  
产物：`data/runlogs/spa_repro/bench3/result_panda_socks_camoufox_*.json`（gitignore）

## 结论（简）

| 步骤 | panda 容器 curl_cffi 直连 | 本机 Camoufox → panda SOCKS |
|------|---------------------------|-----------------------------|
| 出口 IP | `43.156.233.219` | `43.156.233.219`（一致） |
| `GET chatgpt.com/` | CF HTML **403** | **200**（~513 KB HTML） |
| `sentinel/.../prepare` | CF HTML **403** | **200** |
| `sentinel/.../finalize` | 未到达 | **200** |
| `/f/conversation/prepare` | 未到达 | **200** + `conduit_token` |
| `/f/conversation` SSE | 未到达 | Camoufox APIRequest 曾见 **200** `text/event-stream`（随后缓冲超时）；curl_cffi 同 SOCKS+暖 cookie → **403** |
| 生图+下载闭环 | FAIL | **未完成**（SSE 客户端限制，非 prepare 再 CF） |

**核心发现**：同一 panda 公网 IP 上，**Camoufox（Firefox TLS/指纹）可以过掉 curl_cffi 撞死的 CF `prepare` 关**；说明上一轮 panda 直连失败主要是 **客户端栈/指纹 × CF 边缘**，不是「该 IP 绝对不可达」。

**未证明**：在该链式路径上稳定跑完「SSE 收齐 → poll → PNG 下载」（Playwright APIRequest 对长 SSE 硬超时；curl_cffi 过 SOCKS 后 SSE 仍 403）。

## 证据摘录

### 出口确认

```text
socks5h://127.0.0.1:18080 → api.ipify.org → {"ip":"43.156.233.219"}
```

### Camoufox 成功越过 CF（多次可复现）

日志样例（`panda_socks_camoufox_console*.log`）：

```json
{"phase":"egress","ok":true,"ip":"43.156.233.219"}
{"phase":"home","status":200,"bytes":513280}
{"phase":"req_prepare","status":200}
{"phase":"req_finalize","status":200}
{"phase":"conversation_prepare","status":200,"conduit":true}
```

### SSE：Camoufox APIRequest 曾返回 200 流

首次长超时失败的 call log 中明确：

- `POST /backend-api/f/conversation` → **200 OK**
- `content-type: text/event-stream`
- `server: cloudflare` / `cf-ray: …-SIN`
- 随后 Playwright `Request timed out after 30000ms`（缓冲整段 SSE，生图流常 >30s）

### SSE：curl_cffi 经同 SOCKS

暖 cookie 后仍：

```json
{"phase":"sse_done","status":403,"mode":"curl_cffi_socks","has_image_gen":false}
```

原始：`data/runlogs/spa_repro/bench3/result_panda_socks_camoufox_1784616689.json`

## 与三轮对照的关系

| 场景 | 结果 |
|------|------|
| 本地 Clash + curl_cffi | 生图+下载 OK（见 `bench3-20260721.md`） |
| panda 直连 + curl_cffi | prepare CF403 FAIL |
| panda Webshare + curl_cffi | 生图+下载 OK |
| **本机 Camoufox + panda SOCKS** | **prepare 级 CF 可过**；全链路下载未收口 |

## 启示

1. 不要把「panda IP + curl_cffi = CF403」等价成「panda IP 永久黑名单」——浏览器栈同 IP 可过 prepare。
2. 生产仍应优先 **账号 sticky Webshare**（已证明全链路 OK），不把机房 IP 当默认出口。
3. 若要坚持机房 IP：需要 **Camoufox/真浏览器 TLS** 或等价指纹，而不是裸 curl_cffi；长 SSE 需流式客户端（勿用 Playwright APIRequest 整包缓冲）。

## 相关

- 三轮对照：`docs/captures/spa/bench3-20260721.md`
- CF 裁决：`docs/17-cf403-and-egress.md`（本实验不推翻「勿依赖协议绕过」；只细化「同 IP 不同客户端」）
