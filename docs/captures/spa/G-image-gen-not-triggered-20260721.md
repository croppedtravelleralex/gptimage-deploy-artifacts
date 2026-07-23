# G — 生图不触发 image_gen 全局面（2026-07-21）

Panda 生产实测：`picture_v2`（conduit + `system_hints=["picture_v2"]` + `X-Conduit-Token`）与 SPA 空 hints 文本 shape 均**不进 `image_gen`**，上游把生图参数当普通文本流回。跨 3 个出口 IP 段 × 多设备指纹复现，非单 IP/单设备风控。

## 实验设置

- 脚本：`scripts/_tmp_spa_image_bench3.py`（新增 `--protocol {picture_v2,spa_tool}` + `--image-gen-deadline`），经 `scripts/spa_image_load_test.py` 串行单轮驱动。
- 快失败：SSE 内 N 秒无 `image_gen` 立刻 `no_image_gen_within_Ns`，不再干等文本流跑满 chunks/超时。
- 冷却号：`cf7=0`、`fail` 低、`quota` 22–25。出口均为 sticky Webshare。

## 数据（picture_v2，deadline 25–30s）

| 出口 IP | 账号 | quota | home | conduit | has_image_gen | chunks | 结果 |
|---------|------|-------|------|---------|---------------|--------|------|
| 82.21.231.148 | qaflowyi59i282fx | 23 | 403 soft | true | false | 13 | no_image_gen_25s |
| 82.21.231.148 | qaflowgq5wyuxhe9 | 25 | 403 soft | true | false | 13 | no_image_gen_30s |
| 82.21.231.148 | qaflowxwy83tivv5 | 22 | 403 soft | true | false | 13 | no_image_gen_30s |
| 104.252.149.121 | dreamachristine11594 | 25 | 403 soft | true | false | 13 | no_image_gen_30s |
| 92.113.246.176 | qaflowakjewai6ps | 25 | 403 soft | true | false | 15 | no_image_gen_30s |

SSE 实际负载（picture_v2，仍走文本）：

```
{"p":"/message/content/text","o":"append","v":"{\"size\":\"1024x1024\",\"n\":1,\"prompt\":\"Create a medium-detail...\"}"}
{"p":"/message/status","o":"replace","v":"finished_successfully"}
```

即上游把 `{"size","n","prompt"}` 当**文本**念出，而不是触发生图工具。

## 生产 `/v1/images/generations`（gpt-image-2）

真实接口验收（走账号池调度 + failover）：

- 客户端：`HTTP 000`（curl `-m 240` 超时未拿到响应）。
- 服务端日志：多 conversation 并行 poll，`file_ids: []` / `sediment_ids: []` 全程为空，最终
  `image_poll_timeout timeout_secs=180 attempts_made=24 exhausted_reason="conversation_get_budget" last_task_error: null`。
- **`last_task_error: null`** → poll 全程 200，**无 CF 错误**；是 conversation 里根本没有图片文件。

## 结论

1. **第 4 点**：`home` 恒为 `403 soft`（走默认 pow 续跑）。续跑会加重不稳，但**home 是否 200 不是充分条件**——即便 `prepare` 拿到 `conduit_token`、SSE 带 `X-Conduit-Token` + `system_hints=["picture_v2"]`，上游仍可能不触发生图，纯文本流回。空 hints 文本 shape 更是必然不出图。
2. **第 5 点（设备 vs 代理）**：本批数据下**既不是单设备、也不是单 IP** 侧问题——3 个不同出口 IP 段 × 多设备指纹全部不出图，形态一致。说明当前失效在**上游协议层（f/conversation 生图工具不再被触发）**，换 IP / 换设备都救不了。风控在代理侧的假设不足以解释"全出口面失效"。
3. **非 CF**：poll 侧 `last_task_error=null`，与 `17` 的 CF 403 是两条独立故障线；本故障是协议漂移导致的 `image_gen` 不触发。

## 建议

- 归入 `04` **PROTO-REFACTOR**：对齐真实网页 Create Image 请求（抓一份现网 HAR 逐字段 diff `f/conversation` 生图 turn），确认触发 `image_gen` 所需的完整字段/头，而非停留在 `system_hints`。
- bench 快失败 + `--protocol` 已并入常规压测，后续回归可直接 `--protocol picture_v2 --image-gen-deadline 30`。

## Scripts

```bash
python scripts/_tmp_stage_and_run_loadtest_panda.py --email <acct> --mode serial --rounds 1 --protocol picture_v2 --image-gen-deadline 30
```
