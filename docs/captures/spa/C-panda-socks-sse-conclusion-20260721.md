# C — panda SOCKS SSE 工程结论（2026-07-21）

依据：`panda-socks-camoufox-20260721.md`（本批不重开隧道）。

## 事实

| 栈 / 步骤 | 结果 |
|-----------|------|
| 同 IP `43.156.233.219`（panda 经本地 `ssh -D`） | — |
| Camoufox：home / sentinel prepare·finalize / conversation prepare | **200** |
| Camoufox Playwright：长 SSE | 缓冲超时（工程问题，非立即 CF） |
| curl_cffi 经 SOCKS：SSE | **403**（CF HTML） |

## 工程结论（非 CF 绕过）

1. **栈差异成立**：同 DC IP 上 Camoufox 可过 A 层；curl_cffi 直打易 CF —— 记为 **栈×声誉**，不记为缺字段。
2. **SSE 收口**：Playwright `APIRequest`/页面缓冲不适合长图流；若要坚持浏览器栈，需 **真正流式读**（chunk callback / 独立 fetch），或 **暖机交接** 把 cookie/clearance 交给 curl_cffi（见 A 层 PoC）。
3. **本批不做**：不再为「收口」重开 panda SOCKS 长实验；优先 Clash 上完成 Create Image UI HAR + 暖机 PoC。

## Later

- Camoufox 流式 SSE 专用脚本（短超时冒烟 + 图下载魔数）
- 暖机 cookie TTL 与 `__cf_bm` 剥离表（A）
