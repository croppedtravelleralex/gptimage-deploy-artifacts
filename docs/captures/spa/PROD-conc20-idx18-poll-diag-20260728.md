# idx18 上游 Poll 诊断 — conc20 `pipe-conc10-20260728T080723Z`

- **账号**: `qaflowud630wbo2a@proton.me`
- **conversation_id**: `6a686369-d350-83ec-93a1-ac8f29a24068`
- **任务**: `pipe-conc10-20260728T080723Z-conc10-18`
- **诊断时间**: 2026-07-28（Panda 容器内复现探针）

## 结论（一句话）

**上游 Instant 生图额度已耗尽，对话文档里只有拒绝文案、没有任何 `file_id`/`sediment_id`；poll 空转 121s + 续轮询 3×180s 是必然结果，不是 SS 槽或网络故障。**

## 上游对话文档（当前 GET）

| 字段 | 值 |
|------|-----|
| title | **`Image Creation Limit`** |
| mapping_nodes | 5 |
| image_tool_records | **`[]`（空）** |
| policy_error | 未命中（非内容政策类文案） |
| assistant 文本 | *Image creation will be available again when your Instant limit resets. Do you want to try something else for now?* |

## 账号池状态（压测后）

| 字段 | 值 |
|------|-----|
| status | 正常 |
| quota | 5 |
| image_fail_streak | 1 |
| panda_sync_state | ready |
| panda_receive_state | verified_ready |

本地 `quota=5` 与上游 **Instant limit** 不同步：号池仍认为可用，但 ChatGPT 侧已拒绝生图。

## 首次尝试 Poll 时间线（容器日志）

```
image_sse_conversation_id_captured  → conv 已捕获
image_poll_start                    → budget 120s (generation mode)
image_poll_timeout                  → 32 GET, file_ids=[], wall_time @ 120s
image_poll_timeout_pending          → 进入 timeout_pending
```

trace 段：**SSE 24.5s → poll 121.3s（无 poll_resolve_end）→ pipeline_finish @ 190s**

## 续轮询（resume ×3）

每次 `image_poll_start` **180s**，约 **49 次** conversation GET，始终 `file_ids=[]`，`last_task_error=null`，以 `wall_time` 结束。  
3 次合计 ~540s + 首次 ~190s ≈ **730s**，与任务 `lifecycle_s=751s` 一致。

## 现场复现探针（诊断脚本）

| 探针 | 结果 |
|------|------|
| `_get_conversation` | 0.98s，确认 Limit 文案 |
| `_poll_image_results(30s)` | `ImagePollTimeoutError`，8 GET，无图 |
| `_poll_image_results(120s)` | `ImagePollTimeoutError`，33 GET，无图 |

## 根因归类

| 层级 | 说明 |
|------|------|
| **上游业务** | Instant 限额触发，生图请求未产出 asset |
| **检测缺口** | `_find_content_policy_error_in_conversation` 不识别 “Instant limit / Image Creation Limit” |
| **行为后果** | 无图时仍走满 poll 墙钟 + 480s resume_wall，拖垮 conc20 验收墙钟 |
| **非根因** | CF/429（日志无 cf_edge / 429 abort）、token 失效（GET 正常）、SS 槽泄漏 |

## 建议动作

### P0 — 快速止血（运维）

1. **暂停调度** `qaflowud630wbo2a@proton.me` 直至 Instant 窗口重置。
2. conc20 压测避开 **quota 余量偏低** 的账号（该号压测时 quota=5）。

### P1 — 代码（fail-fast）

在 `_poll_image_results` 中，当 `file_ids`/`sediment_ids` 为空时，除 content policy 外增加 **Instant limit / Image Creation Limit** 文案检测，抛出明确错误（如 `image_instant_limit`），**禁止进入 timeout_pending 续轮询**。

### P2 — 号池门禁

额度刷新/准入时对齐上游 Instant 状态，或在 `image_fail_streak` + Limit 文案后写入 `last_image_error` 并降权。

---

诊断脚本：`scripts/_tmp_idx18_poll_diag.py`（只读，Panda 容器内执行）
