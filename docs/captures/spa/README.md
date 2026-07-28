# SPA captures (ChatGPT Web reverse)

## Camoufox 全链路抓包（主证据链）

**2026-07-21 批次**：用 Camoufox 登录 → 浏览器内发消息/生图 → HAR 落盘 → `curl_cffi` HTTP 复现 → bench3 下载闭环。这是协议改造的**第一手来源**（不是验收记录专页）。

| 步骤 | 脚本 | 产物 |
|------|------|------|
| 登录 + 文本聊天 HAR | `scripts/_tmp_spa_camoufox_har.py` | `spa-camoufox-20260721T044906Z.har` + `.meta.json` |
| Create Image UI HAR | `scripts/_tmp_spa_image_har.py` | `spa-image-20260721T074733Z.har` 等 |
| 字段 diff（HAR→代码） | 手工 + `field-diff-20260721.md` | 对齐 `/f/conversation`、OAI 版本号、sentinel 头 |
| HTTP 复现生图+下载 | `scripts/_tmp_spa_camoufox_image_http_repro.py` | `data/runlogs/spa_repro/` |
| 三轮出口对照 | `_tmp_spa_image_bench3.py` | `bench3-20260721.md` |
| 同 IP 栈对照 | `scripts/_tmp_spa_camoufox_via_panda_socks.py` | `panda-socks-camoufox-20260721.md` |

```text
Camoufox 登录/发 prompt（抓 HAR）
  → field-diff 提取 prepare/sentinel/SSE/下载链
  → curl_cffi HTTP repro（同账号同出口）
  → bench3 计时+流量+PNG 魔数校验
```

账号固定：`qaflow0ytb7bbp0z@proton.me`（Clash `127.0.0.1:7897`）。HAR 含 cookie，**gitignore**；可提交的是 `.meta.json` 与专页 md。

任务书与分层目录：[`docs/19-protocol-full-reverse-catalog.md`](../../19-protocol-full-reverse-catalog.md)。

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
| [`O-panda-serial5-quota-account-postfix-20260723.md`](./O-panda-serial5-quota-account-postfix-20260723.md) | P4-7 后串行 5 + 额度扣减；验收账号改邮箱展示 |
| [`acceptance-90s-picture_v2-20260723.md`](./acceptance-90s-picture_v2-20260723.md) | 90s gate + `picture_v2`：serial5 5/5 + 单账号 conc10 3×10/10 |
| [`acceptance-90s-multiacct-20260723.md`](./acceptance-90s-multiacct-20260723.md) | 同上 gate；`preferred_account_email` 多账号轮询 conc10 4/10（CF403 @ init） |
| [`field-diff-20260721.md`](./field-diff-20260721.md) / [`bench3-20260721.md`](./bench3-20260721.md) | 字段 diff / 三轮 bench |
| [`STAB-serial5-20260724T103023Z.md`](./STAB-serial5-20260724T103023Z.md) | STAB-A1 公平 API serial5 **5/5** |
| [`STAB-conc10-20260724T110344Z.md`](./STAB-conc10-20260724T110344Z.md) | multiacct conc10 **10/10**（热号-only 后） |
| [`PROD-serial10-20260724T143921Z.md`](./PROD-serial10-20260724T143921Z.md) | 同步 API serial10 **10/10**；阶段分解 sS 0% / SSE 79% |
| [`PROD-conc10-20260724T150152Z.md`](./PROD-conc10-20260724T150152Z.md) | 同步 API conc10 **10/10**；与 serial10 占墙钟比对比 |
| [`BASELINE-pre-slotledger-20260725T072257Z.md`](./BASELINE-pre-slotledger-20260725T072257Z.md) | **SlotLedger 前横评基线**（RSS / inflight / dispatchable / conc10 参考）；`source=static_docs_26` |
| [`S-cf-ok-spare-webshare-20260725.json`](./S-cf-ok-spare-webshare-20260725.json) | 100 池 CF 探活：空闲 CF-ok 节点盘点（换绑前） |
| [`T-cf-fail-rebind-shared-ip-20260725.json`](./T-cf-fail-rebind-shared-ip-20260725.json) | 9 个 cf_fail 号换绑 + 单 IP 2 号分配表 |
| [`PROD-conc20-20260728T080723Z-gantt.md`](./PROD-conc20-20260728T080723Z-gantt.md) | conc20 **19/20** @ 10 SS 槽；批完成 ~126s；754s 尾等待甘特 |
| [`PROD-conc20-idx18-poll-diag-20260728.md`](./PROD-conc20-idx18-poll-diag-20260728.md) | idx18 上游 Instant limit 诊断；单号复测 success @75s |

## SlotLedger 横评基线

采集脚本：`scripts/capture_performance_baseline.py`

- 在线：拉 `/health?format=json` +（有 auth 时）`/api/ops/image-pipeline/snapshot`
- 离线：回退 `docs/26` 静态值
- 产物：`BASELINE-pre-slotledger-*.{json,md}`；Layer 1 完成后对照 `BASELINE-post-slotledger-*`

```bash
python scripts/capture_performance_baseline.py
PANDA_BASE_URL=http://127.0.0.1:8012 python scripts/capture_performance_baseline.py
```

## Scripts

```bash
python scripts/_tmp_spa_cookie_strip.py
python scripts/_tmp_spa_next_de_har.py
python scripts/_tmp_spa_search_har.py
python scripts/_tmp_spa_http_search_smoke.py
python scripts/_tmp_spa_webshare_stack_probe.py
python scripts/capture_performance_baseline.py
```
