# A — Camoufox→curl_cffi 暖机交接 PoC（2026-07-21）

| 项 | 值 |
|----|-----|
| 脚本 | `scripts/_tmp_spa_warm_handoff_poc.py` |
| 出口 | Clash `127.0.0.1:7897` · egress IP `62.164.143.250` |
| 账号 | `qaflow0ytb7bbp0z@proton.me`（Bearer + session 在 curl 臂；Camoufox 臂未注入 session-token） |
| JSON | `data/runlogs/spa_repro/bench3/warm_handoff_1784619292.json` |

## Camoufox 暖机导出 cookie **名**（无值落盘）

`__Host-next-auth.csrf-token`、`__Secure-next-auth.callback-url`、`__cf_bm`、`__cflb`、`_cfuvid`、`oai-did`、`oai-sc`（共 7；**无** `__Secure-next-auth.session-token` — 本 PoC 浏览器臂仅过 sentinel，鉴权靠 curl 臂 Bearer）

## 对照

| 臂 | 结果 |
|----|------|
| cold（无暖 cookie） | 最终 `ok=false`：多次 TLS(35)；中间曾出现 prepare/conversation prepare 200，但臂未稳定收口到 SSE |
| warm（带 Camoufox cookie） | home/req prepare·finalize / conversation prepare / **SSE 200**（6 chunks） |

## 结论

1. **暖机交接可行（Clash）**：浏览器过 A 层 cookie → curl_cffi 同出口跑短文本 SSE 成功。
2. **最小 cookie 集**：本批未做逐项剥离；候选集即上表 7 名 + 生产必需的 `session-token`（若走 cookie 鉴权）。Later：逐个去掉 `__cf_bm`/`_cfuvid` 复测。
3. **不宣称**暖 cookie 可根治差 DC IP 上的 CF（见 `17` / panda SOCKS 对照）。

## 验收

- [x] PoC 脚本 + JSON
- [ ] 逐 cookie 剥离表
- [ ] 差 IP（panda）上重复本 PoC
