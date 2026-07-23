# A — 差 IP 暖机重复（Webshare，2026-07-21）

对照基线：Clash 暖机交接 **OK**（`A-warm-handoff-20260721.md`）。

## 本批探测（本机 → 账号 Webshare `92.113.236.188`）

| 栈 | 结果 | 证据 |
|----|------|------|
| Camoufox page.goto chatgpt.com | **FAIL** `NS_ERROR_NET_RESET`（egress IP 可达） | `warm_webshare_curl_1784621915.json` / `warm_handoff_webshare_run2` |
| curl_cffi home/prepare | **FAIL** Connection reset（连续 5 次） | 同上 |
| 历史同代理 curl_cffi 生图 | **OK**（panda 容器内） | `bench3-20260721.md` panda Webshare |

## 结论

1. **差 IP 暖机不可当作 Clash 同款可重复**：本机经 Webshare 打开 chatgpt.com 当前被 reset；与「同 IP 上 curl 曾成功」并存 → **时间/路径/TLS 栈** 敏感，不是缺字段。
2. **可用路径**：生产仍押 **curl_cffi + sticky Webshare**（bench3）；Camoufox 暖机优先用 **Clash/好出口**，再交接（见 A-warm）。
3. **不宣称** Webshare 永久可用或协议绕过 CF。

## 验收

- [x] 差 IP 上重复暖机实验（记录失败面）
- [x] 与 Clash 成功对照写入本页
- [ ] panda 容器内再跑 Camoufox 暖机（可选 Later；禁 scp 部署）
