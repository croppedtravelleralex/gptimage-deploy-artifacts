# 三路径生图性能对比 — 20260724

生成时间：2026-07-24T09:09:29.248476+00:00

## 环境
- 目标：Panda `127.0.0.1:8012`
- 账号：`qaflowakjewai6ps@proton.me`
- Prompt：`MEDIUM_PROMPT`（东京雨夜街景）
- 并发：串行 N=5；gap=30s
- pure_http：`panda_webshare` + picture_v2；deadline=90s
- ticket_pool：生产 `/v1/images/generations`（用票路径）
- browser：本轮未跑（后置 BENCH-004）

## 汇总表

| 路径 | N | 成功率 | P50/P90 wall | P50/P90 SSE | P50/P90 下载 | P50/P90 tokens/s | P50/P90 上行 | P50/P90 下行 |
|---|---:|---:|---|---|---|---|---|---|
| pure_http | 5 | 100% | 60.6s / 79.3s | 47.3s / 64.7s | 1.0s / 2.6s | - | 0.01/0.01 MB | 3.09/5.21 MB |
| ticket_pool | 5 | 60% | 76.5s / 240.1s | 22.0s / 56.5s | 0.6s / 1.5s | 33.0/75.2 | 0.00/0.00 MB | 0.00/0.00 MB |
| browser | 0 | - | - | - | - | - | - | - |

## 结论
- 公平对比（剔除超时）：**pure_http**（pure_http P50=60561ms vs ticket 成功 P50=69589ms）
- 全量 P50 wall：pure_http=60561ms，ticket_pool=76483ms（含 2 次超时）
- ticket_pool 成功子集 wall≈[37513, 69589, 76483]（P50=69589ms，n=3）
- ticket_pool 前 2 轮 240s 超时（疑似 pure_http 连跑后号忙/上游排队），后 3 轮成功。
- browser 路径本轮未实测。
- 生产推荐：继续用票路径 `/v1/images`；pure_http 作协议对照。
- 证据：`data/runlogs/image-path-benchmark/20260724/`
