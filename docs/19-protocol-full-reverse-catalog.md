# 19 — ChatGPT Web 协议全量逆向目录（可执行）

最后更新：2026-07-24  
状态：挖矿 Now/Next **已完成**；工程改造见 `04` **PROTO-REFACTOR**

## 0. 定位与硬边界

| 项 | 口径 |
|----|------|
| 目标 | 把登录后 SPA 的 **对话 / 生图 / 附件 / 工具** 协议挖成可复查目录 + HTTP 复现 + 反代可切换实现 |
| 非目标 | **不**宣称协议根除 Cloudflare；不把「买到神代理」当成功条件 |
| 前提 | Webshare 等同机房 IP 一类；Clash/panda 也是 DC。成功率押在 **协议完整度 × 客户端栈 × sticky/调度** |
| 证据法 | HAR → 字段表 → HTTP 复现（同出口上 **Camoufox vs curl_cffi**）→ 最小改代码 → 文档 |
| 成功标准 | **覆盖率 + 复现率**（见 §8）；CF 通过率只作观测，不作「挖全」验收 |

相关：`12`（差距）、`17`（CF）、`18`（反代任务书）、`docs/captures/spa/`（HAR + 专页）。

### 0.1 主证据链（Camoufox 抓包 → HTTP 复现）

> 读逆向应从此链入手；`acceptance-*` / `P-sentinel-*` 等是**验收/实验记录**，不是协议第一手来源。

| 层 | 证据 | 说明 |
|----|------|------|
| 抓包 | `spa-camoufox-20260721T044906Z.har`（文本）、`spa-image-20260721T*.har`（生图 UI） | Camoufox `record_har`；脚本见 `captures/spa/README.md` §Camoufox |
| 字段表 | `field-diff-20260721.md` | HAR vs 本地协议 diff；驱动 `conversation.py` / OAI 版本号对齐 |
| HTTP 复现 | `_tmp_spa_camoufox_image_http_repro.py` | 同账号 curl_cffi 闭环 |
| 出口矩阵 | `bench3-20260721.md` | Clash OK / panda 直连 CF403 / Webshare OK |
| 栈矩阵 | `panda-socks-camoufox-20260721.md` | 同 IP：Camoufox 过 prepare，curl_cffi 不过 |
| 分层专页 | `A-*` … `G-*` | 暖机、上传 sediment、错误面等 |

---

## 1. 分层目录（A–F）

每层固定四列：要挖清什么 / 已有证据 / 缺口 / 下一步验收。

### A — 传输与会话（sentinel / PoW / cookie）

| 要挖清 | 已有 | 缺口 | 下一步验收 |
|--------|------|------|------------|
| `sentinel/chat-requirements/prepare`→`finalize` 状态机 | SPA HAR 有链；HTTP 复现有 | Arkose / `__cf_bm` / clearance 各自贡献未拆 | 同出口 Camoufox vs curl_cffi：缺 cookie 时失败面对照表 |
| PoW seed/difficulty、turnstile `dx` | `utils/pow` / `turnstile` 已用 | SPA 新 sdk 版本漂移 | HAR 抽 `OAI-Client-Version` 与 sdk URL，版本漂移告警脚本 |
| Cookie：`session-token`、`oai-did`、`__cf_bm`、`_cfuvid` | OTP 注 cookie 可登录 | 哪些 cookie 对 backend-api 必需 | 逐项剥离复现：最小 cookie 集 |
| requirements token TTL / 是否可复用 | 经验：短生命周期 | 精确 TTL、跨 prepare 复用边界 | 计时实验 + 写入本目录 |

**落盘约定**：`docs/captures/spa/A-sentinel-YYYYMMDD.md` + 可选 HAR（gitignore）。

### B — 纯文本对话

| 要挖清 | 已有 | 缺口 | 下一步验收 |
|--------|------|------|------------|
| `POST /f/conversation/prepare` + `/f/conversation` | HAR + HTTP 文本 OK（Clash） | 临时聊天、分支编辑、regenerate | 各 1 条 HAR + HTTP 复现 |
| `parent_message_id=client-created-root`；续聊 cid/parent | 已部分对齐 builder | 多轮续聊与 SPA 完全一致 | N≥3 轮续聊 diff |
| `supported_encodings`/`supports_buffering`/followups | 已写入 chat body | 字段删减最小集 | 消融：逐字段去掉仍 200 |
| SSE 事件类型全集 | 部分消费 | `delta_encoding`、tool、title 等完整枚举 | 事件字典 + 解析单测 |
| Temporary Chat：`history_and_training_disabled` | API 仅 True 时带字段；SPA 常省略 | SPA Temporary 开关实抓 | HAR 证明开关行为 |

**脚本**：`scripts/_tmp_spa_http_repro_aligned.py`（扩续聊/消融）。

### C — 生图（双路径）

| 要挖清 | 已有 | 缺口 | 下一步验收 |
|--------|------|------|------------|
| SPA：`system_hints=[]` → 工具 `image_gen` | HAR + HTTP（Clash/Camoufox）；bench3 下载 OK | 与 `picture_v2` 生产路径统一策略 | 决策记录：并存 / 切 SPA / 按模型分流 |
| `picture_v2` + `X-Conduit-Token` | 历史生产路径 | SPA Create Image UI 是否仍发 picture_v2 | 点 UI「创建图片」专用 HAR（非纯文本提示） |
| conduit：prepare 有、文本 SSE 常不发；生图 API 仍发 | field-diff 已记 | 何时 **必须** 带 conduit | 同会话消融实验 |
| estuary / attachment 下载鉴权 | bench3 已闭环 | 多图、失败重试、404 settle | 失败矩阵 + 超时预算文档化 |
| 机房 IP + 浏览器栈 | panda SOCKS+Camoufox 过 prepare | SSE 流式收口 | Camoufox 流式 SSE 或暖机交接 curl_cffi |

**报告**：`bench3-20260721.md`、`panda-socks-camoufox-20260721.md`。

### D — 附件与多模态

| 要挖清 | 已有 | 缺口 | 下一步验收 |
|--------|------|------|------------|
| 上传 `files` → `uploaded` | 代码有编辑/参考图路径 | SPA 上传完整 HAR | 上传+引用生图各 1 条 |
| `sediment://` vs `file-service://` | 解析已有 | 何时用哪一种 | HAR 对照表 |
| 图生图 / 蒙版编辑 | 部分 editable | 与 Web 编辑器对齐 | 专项 HAR |

### E — 搜索与其它工具

| 要挖清 | 已有 | 缺口 | 下一步验收 |
|--------|------|------|------------|
| `system_hints: ["search"]` | 仓内 search 路径 | 与 2026 SPA 字段差 | 联网开/关各 1 HAR |
| 其它 tools（代码、画布等） | 未系统抓 | 范围外可 Later | backlog 条目即可 |

### F — 错误面与观测（指导调度，非绕过）

| 要挖清 | 已有 | 缺口 | 下一步验收 |
|--------|------|------|------------|
| CF HTML 403 vs 协议 4xx vs quota vs moderation | 部分分类 | 统一错误码字典 | `docs/captures/spa/F-errors.md` + 日志字段 |
| 同 IP 不同栈 | Camoufox 过 / curl_cffi 不过 | 生产是否引入暖机栈 | 实验纪要 → 架构决策 |
| DC 出口声誉 | Webshare sticky 可工作 | 声誉分/冷却策略 | 与号池调度字段挂钩（不新买「神代理」假设） |

---

## 2. 客户端栈矩阵（机房 IP 前提）

假定出口长期是普通 DC（Webshare/Clash/VPS 同类）。

| 栈 | 适用 | 已知 |
|----|------|------|
| curl_cffi + Chrome impersonate | 好声誉 sticky DC 上的批量文本/生图 | Clash/Webshare bench 可闭环；差 IP 上 prepare 易 CF |
| Camoufox / 真浏览器 TLS | 难 IP、过 sentinel、抓 HAR | panda IP SOCKS 上 prepare 可过；长 SSE 需流式工程化 |
| 暖机交接 | 浏览器过 A 层 → HTTP 跑 B/C | **待做**（目录优先项） |

实验纪律：凡声称「协议缺口」，须在 **同出口** 上跑通 Camoufox 对照；若仅 curl_cffi 失败而 Camoufox 成功 → 记为 **栈问题**，不记为字段缺口。

---

## 3. 工作流（每挖一项）

```text
1. 定层（A–F）与 AC（可勾选）
2. 登录态抓 HAR → docs/captures/spa/（HAR gitignore；diff md 可提交）
3. 字段表更新 field-diff 或本层专页
4. HTTP 复现脚本（固定账号；记录出口 IP + 栈）
5. 最小改 chatgpt_web_request / openai_backend_api + 单测
6. 更新 12 差距表 + CHANGELOG；本目录勾选状态
```

禁止：无 HAR 改生产请求体；无同出口对照就断言「协议绕过/失败」。

---

## 4. 近端优先级（Now）

**2026-07-21 本批已执行**（证据见 `docs/captures/spa/` A/B/C/F 专页）：

1. [x] **C**：Create Image UI HAR → **仍发** `system_hints:["picture_v2"]`；决策：生产保持 picture_v2，与 NL/`image_gen` 并存  
2. [x] **A**：暖机交接 PoC（Clash）+ 最小 cookie 剥离（见 Next）  
3. [x] **B**：续聊 N=3 + 字段消融（10 字段均可去掉仍 200）  
4. [x] **F**：错误码字典 v1 `F-errors-20260721.md`  
5. [x] **C**：panda SOCKS SSE → **工程结论**（不重开隧道；流式/暖机 Later）

Next：~~D 上传/图生图；E 搜索对齐；A 逐 cookie 剥离；差 IP 上重复暖机。~~ **本批已执行**（见专页）。

**本批 Next 落地（2026-07-21）**：

1. [x] **D**：上传链 HAR + sediment 对照 + 图生图形状 — `D-upload-sediment-20260721.md`
2. [x] **E**：HTTP search 开/关 200；SPA UI 文案「Look something up」— `E-search-20260721.md`
3. [x] **A**：逐 cookie 剥离（Clash+Bearer 下 Cookie 可空）— `A-cookie-strip-20260721.md`
4. [x] **A**：差 IP Webshare 暖机重复（本机 NET_RESET；对照 Clash/bench3）— `A-badip-warm-20260721.md`

Later：冷门工具、全站埋点、SPA Search UI 稳定点选、panda 内 Camoufox 暖机、Temporary Chat HAR、SSE 事件字典、栈 ADR。

## 工程待办（改造，非继续挖矿）

挖矿 Now/Next **已完成**。下一步进 **`docs/04-improvement-backlog.md` → PROTO-REFACTOR（按逆向结果改造生产路径）**：

1. 上传链对齐 SPA（`process_upload_stream` + `sediment://`）  
2. 生图保持 `picture_v2` 默认；`image_spa_tool_path` 仅 canary  
3. 搜索保持 `["search"]`；错误面挂钩 F 字典  

验收与禁止见 `04` 该节。

---

## 5. 证据与产物布局

| 路径 | 内容 | Git |
|------|------|-----|
| `docs/captures/spa/*.har` | 原始 HAR | 否 |
| `docs/captures/spa/field-diff-*.md` | 字段 diff | 是 |
| `docs/captures/spa/bench3-*.md` | 出口对照 | 是 |
| `docs/captures/spa/panda-socks-camoufox-*.md` | 同 IP 不同栈 | 是 |
| `docs/captures/spa/A-*.md` … `F-*.md` | 分层专页（本目录催生） | 是 |
| `data/runlogs/spa_repro/` | secret、JSON、PNG | 否 |
| `scripts/_tmp_spa_*.py` | 复现/bench | 可提交（临时） |

---

## 6. 与反代 / 调度的衔接

- 协议挖全的代码落点：`services/protocol/chatgpt_web_request.py`、`openai_backend_api.py`、`conversation.py`  
- 调度不替代协议：CF/声誉仍按 `17` + 号池；协议完整降低「误伤式」失败，不消灭边缘 403  
- 部署仍走 artifacts（禁 panda build/scp）

---

## 7. 状态看板（勾选）

### A 传输会话

- [x] prepare/finalize 主链 HAR + HTTP（部分）
- [x] 最小 cookie 集（Clash+Bearer：可空；见 `A-cookie-strip-20260721.md`）
- [ ] Arkose/clearance 贡献拆分
- [x] 暖机交接 PoC（Clash；`A-warm-handoff-20260721.md`）
- [x] 差 IP 暖机重复（`A-badip-warm-20260721.md`）

### B 文本

- [x] 新会话 SPA 形状 + HTTP 文本
- [x] 续聊 N≥3（`B-continue-ablate-20260721.md`）
- [x] 字段消融最小集（同页）
- [ ] Temporary Chat HAR
- [ ] SSE 事件字典

### C 生图

- [x] SPA 文本提示 → image_gen HAR + HTTP + 下载（Clash/Webshare）
- [x] 双路径认知（image_gen vs picture_v2）
- [x] Create Image UI HAR（`spa-image-20260721T074733Z.har` → `picture_v2`）
- [x] 生产路径决策：保持 picture_v2（`C-image-path-decision-20260721.md`）
- [x] 机房 IP SSE：工程结论（`C-panda-socks-sse-conclusion-20260721.md`；真流式 Later）

### D 附件

- [x] 上传 HAR（`D-upload-sediment-20260721.md`；链为 process_upload_stream）
- [x] 图生图 HAR（形状；sediment 附图）
- [x] sediment/file-service 对照表（同页）

### E 搜索/工具

- [x] 联网开关（HTTP 开/关；SPA UI Later）— `E-search-20260721.md`
- [ ] （Later）其它工具

### F 错误与栈

- [x] 同 IP Camoufox vs curl_cffi 对照（prepare）
- [x] 错误码字典 v1（`F-errors-20260721.md`）
- [ ] 栈分级接入生产的 ADR

---

## 8. 「挖全」验收定义

全部 A–F 的 Now 项勾选，且满足：

1. 每层至少 1 份可提交 diff/专页 + 可复现脚本路径  
2. 文本与生图主路径 HTTP 复现可重复（同账号、记录出口 IP 与栈）  
3. `docs/12` 差距表与本看板一致  
4. 文档明确：CF 仍可能发生；DC 代理无质量保证  

---

## 9. 新开对话可复制提示

```text
按 docs/19-protocol-full-reverse-catalog.md 执行协议全量逆向。
前提：出口按普通机房 IP 假设；禁止宣称协议绕过 CF。
本轮优先：【填写 A–F 编号，如 C-CreateImage UI HAR + A-暖机交接 PoC】。
证据落 docs/captures/spa/；改代码前先 HAR + 同出口 Camoufox/curl_cffi 对照；同步 12 与 CHANGELOG。
```

## 相关

- `12-protocol-gap-vs-web.md`
- `17-cf403-and-egress.md`
- `18-openai-web-reverse-proxy-brief.md`
- `docs/captures/spa/README.md`
- `docs/captures/spa/bench3-20260721.md`
- `docs/captures/spa/panda-socks-camoufox-20260721.md`
