# D — 上传 / 图生图 / sediment 对照（2026-07-21）

| 项 | 值 |
|----|-----|
| HAR | `spa-next-de-20260721T080534Z.har`（gitignore） |
| 脚本 | `scripts/_tmp_spa_next_de_har.py` |
| 出口 | Clash `127.0.0.1:7897` |

## 上传链（SPA 2026）

| 步骤 | 方法 / URL | 状态 |
|------|------------|------|
| 元数据 | `POST /backend-api/files`（`use_case=multimodal`，`supports_direct_azure_multipart=true`） | 200 |
| 字节 | `PUT https://sdmntpr….oaiusercontent.com/files/…/raw` | 201 |
| 收尾 | `POST /backend-api/files/process_upload_stream`（**非** 旧 `/files/{id}/uploaded`） | 200 |
| 其它 | `GET …/files/{id}/simple`、`…/download/…`、`my/recent/uploaded_images` | 200 |

本 HAR：**`post_uploaded=0`**（经典 `…/uploaded` 未出现）。生产代码仍有 `_upload_image` → `/uploaded` 路径 —— **与现网 SPA 有漂移**，Later 对齐。

## sediment vs file-service

| 场景 | asset_pointer | 证据 |
|------|---------------|------|
| SPA 聊天附图 / 图生图（本 HAR i2i） | `sediment://file_0000…` + `content_type=image_asset_pointer` | CONV body：`make a redder version…` + sediment |
| 仓内 multimodal / search 上传 | 常 `file-service://…` | `openai_backend_api._upload_image` / search builders |
| 仓内 editable | `sediment://…` | `_run_editable_conversation` |

**对照结论**：SPA 作曲器附图当前倾向 **sediment://**；反代部分路径仍写 **file-service://**。生图 UI 开 `picture_v2` 时可与附图同会话（本 HAR：`system_hints_picture_v2=1`；i2i CONV 最终 hints=`[]` 但仍带 sediment 图）。

## 图生图

- 已 attach + Create image UI + 提示 `make a redder version of this image`
- 请求含 multimodal sediment 指针；完整出图下载未作本批 AC（协议形状优先）

## 验收

- [x] 上传 HAR + 新链（files → PUT oaiusercontent → process_upload_stream）
- [x] sediment/file-service 对照表
- [x] 图生图尽力 HAR（形状）
- [ ] 生产 `/uploaded` → `process_upload_stream` 对齐（代码 Later）
