# 参考文档（reference）

运维与语义参考；**不含**执行计划（见根目录 `plan.md`）。

| 文件 | 用途 |
|------|------|
| `deployment.md` | Panda 部署（artifacts；禁 scp 业务码） |
| `quota-semantics.md` | 额度三态 + `restore_at`/核对时间 |
| `32-quota-refresh-window-prime-plan.md` | 四段 limits 刷新、窗口预热、Rust/架构（**实现待做**） |
| `flaresolverr-cloudflare.md` | FlareSolverr — **仅注册 clearance** |
| `upstream-sse-conversation.md` | SSE 事件语义 |
| `sync-strategy.md` | 号池同步策略（偏历史） |
| `performance-acceptance-test-plan.md` | 性能验收（偏历史） |
| `review.md` | 评审记录 |
| `feature-status.en.md` | 功能状态（英文） |

生图 CF 裁决见 **`17-cf403-and-egress.md`**（在 `docs/` 根目录，属 plans 类）。
