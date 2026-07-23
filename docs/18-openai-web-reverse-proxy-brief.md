# 18 — 新对话任务书：逆向 ChatGPT Web → 对话/生图 API 反代

用途：粘贴到 **新开的 Cursor Agent 对话** 作为首条用户消息（本工具无法代开带 prompt 的 Agent 窗）。

仓库：`D:\SelfMadeTool\AutoRegister\gptimage`  
相关：`docs/12-protocol-gap-vs-web.md`、`docs/17-cf403-and-egress.md`、`docs/19-protocol-full-reverse-catalog.md`、`services/openai_backend_api.py`、`services/protocol/`

---

## 目标

逆向 **chatgpt.com 登录后 SPA** 的真实聊天与生图流量，把 **对话 + 生图** 以稳定 HTTP API 反代出来（OpenAI 兼容或本仓既有 `/v1/chat/completions` + `/v1/images/*` 形状），尽量对齐 Web 行为，降低相对 SPA 的协议差距与 CF 误伤。

全量挖矿分层与看板见 **`19`**（机房 IP 前提；挖全 ≠ 绕过 CF）。

## 硬约束

1. **禁止**宣称「协议绕过 Cloudflare」；出口多为机房 IP（含 Webshare），靠 sticky/调度/客户端栈，不赌「永远高质量代理」。见 `17`/`19`。
2. **禁止**在 panda 上 build / scp 正式发布；走既有 artifacts。
3. 先 **HAR/抓包证据**，再改请求体；同出口对照 Camoufox vs curl_cffi（栈 vs 字段）。见 `19`。
4. 不把 FlareSolverr 当根方案；可用 Camoufox **登录态**抓包补证据。
5. 中文简报；改动同步 `CHANGELOG` + `12`/`02`/`19`。

## 建议步骤

1. 用 Camoufox（或手工 DevTools）登录观察号，抓一条 **纯文本聊天** + 一条 **生图** 完整请求链（含 prepare/sentinel/conversation/SSE）。
2. 对照 `chatgpt_web_request.py` / `openai_backend_api.py` 做字段 diff 表。
3. 最小补齐反代路径：文本续聊字段、生图 conduit、下载鉴权；单测 + 本地 smoke。
4. 再谈 Panda canary；验收贴真实日志（禁止编造）。

## 成功标准

- [x] 有可复查的 HAR/抓包落盘路径（`docs/captures/spa/`）  
- [x] 文本与生图各至少 1 条与 Web 对齐的可复现请求（见 `19` / bench3）  
- [x] 文档写明与 Web 仍存差距（`12`）  
- [x] CF 策略仍按 `17`（出口+换号），不承诺绕过  

**挖矿阶段完成。** 工程改造跟踪：`04` **PROTO-REFACTOR**（按逆向结果改造生产路径）。

## 首条消息（可直接复制）

```text
按 docs/04-improvement-backlog.md 的 PROTO-REFACTOR 改造生产路径。
依据 docs/19 + docs/captures/spa/（尤其 D-upload-sediment）。
优先：上传链对齐 process_upload_stream + sediment://；生图默认仍 picture_v2。
禁止宣称协议绕过 CF；禁 panda build/scp。同步 CHANGELOG/12/02。
```

（旧「从零挖 HAR」提示已过时；全量目录见 `19`。）
