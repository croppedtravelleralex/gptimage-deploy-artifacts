# SPA captures (ChatGPT Web reverse)

## Layout

| Path | Commit? | Notes |
|------|---------|-------|
| `*.har` | **no** | Raw HAR（含 cookie） |
| `A|B|C|D|E|F-*.md` / `field-diff-*.md` | yes | 分层专页 |
| `README.md` | yes | This file |

任务书：[`docs/19-protocol-full-reverse-catalog.md`](../../19-protocol-full-reverse-catalog.md)。  
**生图纯 HTTP / Sentinel 待办（P0）**：[`docs/20-pure-http-image-sentinel-todo.md`](../../20-pure-http-image-sentinel-todo.md) → `04` **PROTO-PURE-HTTP**。  
**改造待办（上传等）**：[`docs/04-improvement-backlog.md`](../../04-improvement-backlog.md) → **PROTO-REFACTOR**。

## Document index (2026-07-22)

| 文档 | 内容 |
|------|------|
| [`A-warm-handoff-20260721.md`](./A-warm-handoff-20260721.md) | Clash 暖机交接 |
| [`A-cookie-strip-20260721.md`](./A-cookie-strip-20260721.md) | 逐 cookie 剥离 |
| [`A-badip-warm-20260721.md`](./A-badip-warm-20260721.md) | 差 IP Webshare 暖机 |
| [`B-continue-ablate-20260721.md`](./B-continue-ablate-20260721.md) | 续聊+消融 |
| [`C-image-path-decision-20260721.md`](./C-image-path-decision-20260721.md) | 生图双路径决策 |
| [`C-panda-socks-sse-conclusion-20260721.md`](./C-panda-socks-sse-conclusion-20260721.md) | SSE 工程结论 |
| [`D-upload-sediment-20260721.md`](./D-upload-sediment-20260721.md) | 上传链 + sediment |
| [`E-search-20260721.md`](./E-search-20260721.md) | 联网开/关 |
| [`F-errors-20260721.md`](./F-errors-20260721.md) | 错误码字典 |
| [`G-image-gen-not-triggered-20260721.md`](./G-image-gen-not-triggered-20260721.md) | 生图不触发 image_gen 全出口面（picture_v2+空hints 均不出图） |
| [`H-pure-http-sentinel-fix-20260722.md`](./H-pure-http-sentinel-fix-20260722.md) | Turnstile VM 真接受、纯 HTTP 真出图与正式链路收口 |
| [`I-panda-webshare-pure-http-canary-20260722.json`](./I-panda-webshare-pure-http-canary-20260722.json) | Panda Webshare staging 单 canary 原子脱敏证据：已触发 image_gen，poll/download CF403 |
| [`J-panda-production-pure-http-canary-20260722.json`](./J-panda-production-pure-http-canary-20260722.json) | Panda 正式部署后单账号 Webshare 生产 canary：纯 HTTP 出图/下载成功，`/tasks` 1 次 CF403 后恢复 |
| [`K-panda-production-pure-http-serial5-20260722.json`](./K-panda-production-pure-http-serial5-20260722.json) | 固定账号/Webshare 串行 5：执行 2/5，`1 ok / 1 no_image_gen`，连续两轮 CF 信号后止损 |
| [`M-panda-new-ip-serial5-20260722.json`](./M-panda-new-ip-serial5-20260722.json) | 新 IP 换绑后串行 5：`4/5`，第 4 轮 `no_image_gen` 止损 |
| [`N-panda-serial5-observability-20260722.json`](./N-panda-serial5-observability-20260722.json) | P4-D 观测 harness 串行 5：`2/5`，第 2 轮 `late_image_gen_after_gate` @64.5s |
| [`N-panda-cf-scan5-webshare-20260722.json`](./N-panda-cf-scan5-webshare-20260722.json) | Webshare 池前 5 节点 CF 预扫：`1 ok / 4 cf403` |
| [`N-panda-serial5-round2-analysis-20260722.md`](./N-panda-serial5-round2-analysis-20260722.md) | 第 2 轮 SSE 慢速归因：非出口漂移，建议 gate 65s |
| [`field-diff-20260721.md`](./field-diff-20260721.md) / [`bench3-20260721.md`](./bench3-20260721.md) | 字段 diff / 三轮 bench |
| → 修复执行单 | [`docs/20-pure-http-image-sentinel-todo.md`](../../20-pure-http-image-sentinel-todo.md)（P1–P3、P4 正式发布/单单元下载已完成；串行 5 在 2/5 止损，并发未做） |

## Scripts

```bash
python scripts/_tmp_spa_cookie_strip.py
python scripts/_tmp_spa_next_de_har.py
python scripts/_tmp_spa_search_har.py
python scripts/_tmp_spa_http_search_smoke.py
python scripts/_tmp_spa_webshare_stack_probe.py
```
