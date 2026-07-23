# B — 续聊 N=3 + 字段消融（2026-07-21）

| 项 | 值 |
|----|-----|
| 脚本 | `scripts/_tmp_spa_text_continue_ablate.py` |
| 出口 | Clash `127.0.0.1:7897` |
| 账号 | `qaflow0ytb7bbp0z@proton.me` |
| 原始 JSON | `data/runlogs/spa_repro/bench3/text_continue_ablate_1784619179.json`（gitignore） |

## 续聊

| round | prepare | SSE | chunks | conversation_id |
|-------|---------|-----|--------|-----------------|
| 1（新） | 200 | 200 | 12 | `6a5f1fde-…9a4c` |
| 2（cid+parent） | 200 | 200 | 12 | 同 |
| 3 | 200 | 200 | 11 | 同 |

结论：N=3 续聊在 Clash + curl_cffi 下闭环。偶发 TLS(35) 可重试后成功（栈抖动，非字段缺口）。

## 消融（逐字段去掉，仍 prepare+SSE 200）

全部 **ok**：`supports_buffering`、`supported_encodings`、`enable_message_followups`、`force_parallel_switch`、`paragen_cot_summary_display_override`、`client_prepare_state`、`client_contextual_info`、`system_hints`、`timezone`、`timezone_offset_min`。

**口径**：在本出口上这些字段**非硬依赖**；生产仍建议对齐 SPA 完整集（防版本漂移），消融结果不作「可永久删字段」依据。

未测（Later）：临时聊天开关、regenerate、分支编辑、去掉 `Prepare-Token` / sentinel。
