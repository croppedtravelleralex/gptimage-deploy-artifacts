# O — Panda 串行 5（有额度账号，P4-7 修复后）

日期：2026-07-23（UTC）

## 账号

| 字段 | 值 |
|------|-----|
| email | `qaflowakjewai6ps@proton.me` |
| 出口 | `92.113.246.176` |
| gate | 65s image_gen deadline |
| 协议 | `spa_tool` |

## 结果

| 指标 | 值 |
|------|-----|
| planned | 5 |
| ok | **5** |
| failed | 0 |
| no_image_gen | 0 |
| cf403_propagated | 0 |
| serial5_passed | true（门禁 bug 已修：`spa_acceptance_gates.serial5_passed`） |

## 轮次摘要

| round | ok | sse_image_gen_ms | poll_resolve_ms | total_ms |
|-------|-----|------------------|-----------------|----------|
| 1 | ✅ | 28663 | 7754 | 45953 |
| 2 | ✅ | 44788 | 6620 | 56546 |
| 3 | ✅ | 24070 | 6883 | 39551 |
| 4 | ✅ | 64688 | 9703 | 82861 |
| 5 | ✅ | 45995 | 8390 | 59719 |

每轮均有 `sediment_ids`，无 poll 429。

## 证据路径（Panda）

`/app/data/runlogs/spa_repro/staged/out/serial5-65s-quota-account-postfix-20260723/`

## 关联修复（P4-7）

- SSE 等 `sediment://` 再结束
- poll 连续 3×429 熔断
- `mark_image_result` 同步扣减 `limits_progress`（修复 bench 多轮后 UI 额度不变）
