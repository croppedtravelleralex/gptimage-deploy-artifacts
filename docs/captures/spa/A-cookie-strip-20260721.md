# A — 逐 cookie 剥离（Clash，2026-07-21）

| 项 | 值 |
|----|-----|
| 脚本 | `scripts/_tmp_spa_cookie_strip.py` |
| 出口 | Clash `127.0.0.1:7897` |
| 鉴权 | **Bearer `access_token`**（各臂均带）；Camoufox 暖机另注入 session-token |
| JSON | `data/runlogs/spa_repro/bench3/cookie_strip_1784621109.json` |

## 暖机导出 cookie 名（6）

`__Secure-next-auth.session-token`、`oai-did`、`__cf_bm`、`__cflb`、`_cfuvid`、`oai-sc`

## 结果（prepare + 短 SSE）

| 臂 | prepare | SSE | ok |
|----|---------|-----|-----|
| baseline（全量暖 cookie） | 200 | 200 | ✓ |
| 逐个 drop：session / oai-did / `__cf_bm` / `__cflb` / `_cfuvid` / oai-sc | 200 | 200 | ✓ |
| 仅 session+oai-did | 200 | 200 | ✓ |
| 仅 CF 三件套 | 200 | 200 | ✓ |
| **空 Cookie** | 200 | 200 | ✓ |

## 结论（本出口 + Bearer）

1. **backend-api 短文本路径对 Cookie 非硬依赖**：空 Cookie 仍可 prepare+SSE（Bearer 足够）。
2. **最小 cookie 集（本场景）= ∅**（相对 Cookie 头）；生产仍建议保留 session + CF + oai-did（登录页、资源域、差 IP 栈不同）。
3. **不外推**：差 IP / 无 Bearer / 仅 cookie 会话 / 长生图 可能仍依赖 `__cf_bm` 等——见差 IP 暖机专页。

## 验收

- [x] 逐项剥离表（Clash）
- [ ] 差 IP 上重复（Webshare / panda）
