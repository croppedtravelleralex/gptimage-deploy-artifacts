# C — 生图双路径决策（2026-07-21）

状态：**已定稿（短期）** — Create Image UI HAR 已确认 `picture_v2` 仍存在  
账号/出口：`qaflow…@proton.me` · Clash `127.0.0.1:7897`

## 1. 两条可工作（且 SPA 均在用）的形状

| 路径 | 触发 | 请求特征 | 证据 |
|------|------|----------|------|
| **NL / 工具链** | 文本「Create an image…」 | `system_hints: []`；SSE 侧 `image_gen` 元数据；conduit **可有可无** | `spa-image-20260721T073505Z.har`；`bench3`；`field-diff` |
| **Create Image UI** | Plus →「Create image」 | `system_hints: ["picture_v2"]`；用户内容常带 `@Create image …`；**带** `X-Conduit-Token` | `spa-image-20260721T074733Z.har`（`picture_v2_in_conversation_req=true`；hints 序列 `[]`→`["picture_v2"]`） |

`GET /backend-api/system_hints` 将 UI「Create image」登记为 `system_hint: picture_v2`（目录项，不等于每次请求都带）。

## 2. 决策（短期 / Now）

1. **生产 `/v1/images` 保持** `picture_v2` + `X-Conduit-Token`（与 Create Image UI 同族；历史稳定）。
2. **NL/`image_gen` 形** 继续作对照与 bench；不默认切换生产。
3. **不**因「SPA 文本提示可生图」删掉 `picture_v2`。

## 3. 中期（可选）

配置开关 `image_protocol=picture_v2|spa_tool`（默认 `picture_v2`）；灰度前须同出口成功率与 F 错误面可比。

## 4. 验收

- [x] Create Image UI HAR：确认仍发 `picture_v2`
- [x] 决策：生产保持 picture_v2；双路径并存
- [ ] 配置开关（Later，非本批必做）
- [x] 同步 `docs/12` / `19` 看板
