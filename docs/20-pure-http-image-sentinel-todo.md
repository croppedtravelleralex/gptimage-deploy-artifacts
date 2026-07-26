# 20 — 严格纯 HTTP 生图：Sentinel / Turnstile 修复全量待办

最后校准：2026-07-23（Asia/Shanghai）

> **目标**：生产生图数据面 **100% curl_cffi HTTP**，触发上游 `image_gen` 并完成出图；**禁止**浏览器/Camoufox/FlareSolverr/外部 Turnstile solver 作数据面或暖机兜底。  
> **工程入口**：`04` 项 **PROTO-PURE-HTTP**（本文件为详细真相源）。  
> **关联证据**：`docs/captures/spa/G-image-gen-not-triggered-20260721.md`、`field-diff-picture_v2-live-vs-bench.json`、`spa-image-20260721T144019Z.har`（HAR 不入库）。

> **2026-07-23**：串行 5 **5/5**（`qaflowakjewai6ps@proton.me`）；单账号 conc10 **30/30**；多账号轮询 conc10 **4/10**（冷号突发 CF403 @ init）。验收 gate 临时 **90s**。

---

## 0.1 纯 HTTP 生图链路速查（Chrome TLS 指纹 + curl_cffi）

> 用户口语「Chrome 开票/开图」在本仓指 **curl_cffi Chrome impersonate**，不是 Camoufox/Playwright 数据面。

```text
账号 fp（impersonate chrome120/124/131 + UA + device-id）
  → sticky Webshare 代理
  → bootstrap（可选，home 软 403 可容忍）
  → GET chat-requirements + POST finalize（proofofwork + turnstile，VM 纯 HTTP 求解）
  → prepare（无 Sentinel 头）
  → POST /backend-api/f/conversation（start，带 Sentinel/Turnstile）
  → SSE 消费至 image_gen / sediment
  → poll conversation 或 /tasks（连续 CF → cf_abort 快失败）
  → estuary 下载 PNG
```

| 环节 | 代码入口 | 配置/开关 |
|------|----------|-----------|
| TLS 指纹 | `services/account_fingerprint.py` | 每号 `impersonate` |
| requirements/finalize | `services/openai_backend_api.py` | `_get_chat_requirements_once` |
| prepare/start body | `services/protocol/chatgpt_web_request.py` | `image_spa_tool_path`（默认 `true`=auto-tool；`false`→`picture_v2`） |
| Turnstile VM | `utils/turnstile.py` | 禁止外部 solver |
| 对话/SSE | `services/protocol/conversation.py` | `image_stream_cf_failover` |
| OpenAI 后端封装 | `services/openai_backend_api.py` | CF 单次失败即上层换号 |
| 压测/验收 | `scripts/_tmp_spa_image_bench3.py`、`scripts/spa_image_panda_acceptance.py` | `--protocol picture_v2\|spa_tool`、`--image-gen-deadline` |
| Pipeline 验收 | `scripts/_tmp_acceptance_serial5_conc10_suite.py` | `preferred_account_email` 多账号轮询 |

**证据链（按时间）**：`H-pure-http-sentinel-fix-20260722.md`（突破）→ `J-*`（生产单单元）→ `O-*`（串行 5 + 额度）→ `acceptance-90s-picture_v2-20260723`（单账号 conc10 30/30）→ `acceptance-90s-multiacct-20260723`（多账号 conc10 4/10）。

**禁止**：Camoufox/Playwright 作生图数据面；FlareSolverr；空 Turnstile 软降级。

**票生命周期实验**（2026-07-23）：`22` §8、`scripts/_tmp_sentinel_ticket_ablation.py` — 验证 delay/reuse/cross-IP/TTL/并发开票，非生产默认路径。

---

## 0. 问题定性（已证实）

| 观察 | 证据 | 结论 |
|------|------|------|
| SSE 建 cid 后 poll `file_ids=[]`，`last_task_error=null` | Panda 生产日志 / G 专页 | **非 CF**，是生图工具未触发 |
| 跨 3 出口 IP 段 × 多设备指纹仍无 `image_gen` | G 专页 | **非单 IP / 非单设备** 风控 |
| Body top-level 与现网 Create Image HAR 基本一致 | field-diff JSON | 主缺口在 **Sentinel 凭证层**，不是 hints/conduit 形状 alone |
| 现网 HAR finalize 键为 `prepare_token` / `proofofwork` / `turnstile` | `spa-image-20260721T144019Z.har` | 旧键 `proof_token`/`turnstile_token` 已漂移 |
| 现网 SSE **必带** `OpenAI-Sentinel-Turnstile-Token` | 同上 HAR | 无 Turnstile 头则不触发生图（与生产 `request_shape` 缺 turnstile 头一致） |
| 本仓 `utils/turnstile.py` 曾对现网 `dx` 离线求解为空；2026-07-22 修复后真实 finalize 接受 | 真实 canary + `test_turnstile_vm.py` | **VM 阻塞已解除** |
| `basketikun/chatgpt2api` 与本仓 `utils/turnstile.py`/`pow.py` 字节级相同（turnstile 最后改 2026-04-23） | 对比 | **拉上游等于拉自己，过不了** |
| `lanqian528/chat2api` 依赖 `turnstile_solver_url`，失败则忽略 Turnstile | 外部仓库 | **不符合严格纯 HTTP**，禁止作根方案 |

生产默认协议现为：纯 HTTP auto-tool（`image_spa_tool_path=true`，无 conduit）；显式 `false` 才回退 `picture_v2` canary。Panda 已部署并完成单单元出图/下载验收。

---

## 1. 总览看板

| Phase | 名称 | 状态 | 说明 |
|-------|------|------|------|
| P0 | 诊断与压测基建 | **已完成** | G 专页、bench 立刻失败、field-diff |
| P1 | finalize 字段对齐 + 空 Turnstile 硬失败 | **已完成** | 生产 + 单测 + 多数 `_tmp_spa_*` |
| P2 | Turnstile VM 复活（纯 HTTP） | **已完成** | 真实 finalize 接受，required token 约 2.6k |
| P3 | auto-tool prepare/start 形状 | **生产完成** | 已剥离 conduit、空 metadata/create_time/trace；生产已触发 `image_gen` 并下载 |
| P4 | SSE 门禁 + Panda 验收 + 收口文档 | **诊断观测修复中** | 正式发布与单单元下载完成；新 IP 串行 5 在 4/5 因 `no_image_gen` 止损；先修 deadline/事件/CF 分类，并发未做 |
| — | 号池 CF 被动灯 / poll CF 快失败 | **已完成** | 旁路能力，不替代 Sentinel 修复 |
| — | 从 GitHub 拉 chat2api / chatgpt2api 直接过 | **已否决** | 见 §0 |

---

## 2. 已完成（Done）— 详细清单

### 2.1 诊断与证据（P0）

- [x] 定性「SSE 干等不出图」= 无 `image_gen`，非 CF / 非空挂 poll  
- [x] 专页 `docs/captures/spa/G-image-gen-not-triggered-20260721.md`  
- [x] `field-diff-picture_v2-live-vs-bench.json`（live HAR vs bench/生产）  
- [x] 刷新 `docs/12`「生图不触发 image_gen」段  
- [x] 更新 `docs/captures/spa/README.md` 索引 G 专页  
- [x] CHANGELOG 记录压测与定性结论  

### 2.2 压测 / Bench 基建（P0）

- [x] `scripts/_tmp_spa_image_bench3.py`：`--protocol {picture_v2,spa_tool}`  
- [x] 同上：`--image-gen-deadline`（N 秒无 `image_gen` **立刻失败**，不再干等 poll）  
- [x] `scripts/spa_image_load_test.py`：透传上述参数；summary 增 `no_image_gen`  
- [x] `scripts/_tmp_stage_and_run_loadtest_panda.py`：经 ssh stdin/base64 写入 RW data 挂载再 `docker exec`（**非正式部署**；正式仍走 Git/artifacts）  

### 2.3 Phase 1 — finalize 对齐（已落地）

生产 `services/openai_backend_api.py` `_get_chat_requirements_once`：

- [x] finalize body 使用现网键：`proofofwork` / `turnstile`（不再发 `proof_token`/`turnstile_token`）  
- [x] `turnstile` required 但求解为空 → 硬失败 `chat_requirements_turnstile_required_but_unsolved`  
- [x] 结构化日志 `chat_requirements_finalize_shape`（含 `finalize_keys`）  
- [x] 单测 `test/test_chat_requirements_finalize.py`（本地已通过）  
- [x] 多数诊断脚本 finalize JSON 已改为 `proofofwork`/`turnstile`：  
  - `_tmp_spa_http_repro_aligned.py`  
  - `_tmp_spa_image_bench3.py`  
  - `_tmp_spa_warm_handoff_poc.py`  
  - `_tmp_spa_sse_diag.py`（finalize 体已改；内部变量名可仍叫 `proof_token`）  
  - `_tmp_spa_camoufox_image_http_repro.py`  
  - `_tmp_spa_camoufox_via_panda_socks.py`  
  - `_tmp_spa_cookie_strip.py`  
  - `_tmp_spa_text_continue_ablate.py`  
- [x] 辅助脚本：`scripts/_tmp_patch_finalize_keys.py`、`scripts/_tmp_debug_turnstile_vm.py`（调试用）  

### 2.4 旁路：CF 观测与 poll 快失败（已落地，非本主线根因）

- [x] UI：`CfStatusLight.tsx`；号池代理区 provider 灰字移到 IP 右侧  
- [x] `api.ts` / 账号模型暴露被动 `cf_daily`  
- [x] `account_service.record_cf_sample` / `mark_image_result`  
- [x] `image_poll_cf_abort_streak` + `_poll_image_results` 连续 CF 快失败  
- [x] 单测 `test_cf_daily_sample.py`、`test_image_poll_cf_abort.py`  
- [x] `docs/17` / CHANGELOG / handoff 已记  

### 2.5 外部方案评估（已否决，记入避免重踩）

- [x] 对比 `basketikun/chatgpt2api`：与本仓 Turnstile/PoW **相同**，不能「拉过来就过」  
- [x] 评估 chat2api `turnstile_solver_url`：**外部浏览器求解**，违反严格纯 HTTP → **禁止作根方案**  

### 2.6 Phase 2 研究碎片（历史，已收口）

- [x] 确认现网 `p` 配置形状线索：`config[2]=null`、jsd script、`prod-773467…` 等（对齐工作仍待做）  
- [x] 浮点 opcode（如 `81.2`）初步结论：多为 opcode 8 拷贝函数后的**寄存器别名**，不是全新指令集（VM 复活仍未完成）  
- [x] `_tmp_debug_turnstile_vm.py` 已恢复为与生产 VM 一致；夹具断言见 `test_turnstile_vm.py`  

### 2.7 2026-07-22 真实突破与收口（最新）

- [x] 本地 Turnstile VM 生成 token 被真实 finalize 接受；required 时不再为空。
- [x] 纯 HTTP 真出图：conversation `6a602f69-54a0-83ec-8312-945864ce7e52`，下载 PNG `841,139` bytes。
- [x] auto-tool body：原始 prompt、top-level `system_hints=[]`、旧 web-push contextual、无 conduit。
- [x] start 带 requirements/proof/Turnstile；prepare 不带 Sentinel；使用已证旧 image client version/build。
- [x] CF403 单次失败，不在同账号 requirements 内三连重试；TLS/socket 重建保留 proxy、verify、impersonate、timeout。
- [x] canary 不再把 conversation_id 当成功；必须出现 image tool/file。
- [x] 本地相关回归 `85 passed`。
- [x] Panda staging 单 canary 触发 `image_gen`，收到 sediment ID（脱敏证据 `I-panda-webshare-pure-http-canary-20260722.json`）。
- [x] poll 连续 CF403 达阈值立即 abort，resolver 不再继续附件请求；本地定向 `34 passed`、扩展受影响回归 `92 passed`。
- [x] 正式 Git/artifacts 发布到 Panda；单单元 canary 已完成下载（证据 `J-panda-production-pure-http-canary-20260722.json`）。
- [ ] Panda 串行 5、并发 4（新 IP 串行已尝试 4/5：`3 ok / 1 no_image_gen` 后止损；并发未开始）。
- [x] 同账号/同 fp/同请求形状只换 Webshare IP 的最小 A/B；新 IP `2/2` 无 CF 成功，旧 IP 单次无 CF但下载 503，严格 IP 归因未成立。
- [x] 同账号保持原 fp/session 生产换绑到新 IP，并按 `300s/request`、并发 1、轮间冷却执行；第 4 轮异常后未补第 5 轮、未整单重试。
- [x] 第 4 轮复盘：requirements/finalize/prepare/conversation 成功；SSE 收到 13 chunks、约 8 KiB，出现类似生图参数 JSON，但 45 秒内无明确 `image_gen`。
- [x] CF 口径复核：首页软失败 403 为 `4/4`；业务链传播 CF 为 0；前三轮 `/tasks` 为 0，第四轮未进入 poll。原 `cf403=0` 仅代表 harness 业务异常分类。
- [x] 发现 deadline 边界风险：bench 当前在解析 SSE 行前先判超时，45 秒后刚到达的工具事件可能被丢弃；生产代码另有 JSON tool-call 停在 STREAMING、不产图的已知相似模式。

---

## 3. 未完成（Todo）— 详细清单

### 3.1 Phase 2 — Turnstile VM 复活【已完成】

目标：`solve_turnstile_token(dx, p)` 对 **现网 HAR 夹具** 产出非空 token；生产 finalize 后 SSE 可带 `OpenAI-Sentinel-Turnstile-Token`。

| ID | 待办 | 验收 | 状态 |
|----|------|------|------|
| P2-1 | 抽取真实 `dx`/`p` 对照 | `test/fixtures/turnstile_dx_20260721.json` | 完成 |
| P2-2 | 跑通 `scripts/_tmp_debug_turnstile_vm.py` 并定位 VM 偏差 | 调试脚本与生产逻辑一致 | 完成 |
| P2-3 | 对齐现网 `p` 配置与 VM 输入 | 同夹具解析通过 | 完成 |
| P2-4 | 修复 VM，使现网 `dx` 求解非空 | 单测通过，真实 token 约 2.6k | 完成 |
| P2-5 | required 时必须带非空 token；空值硬失败 | 真实 finalize 接受 | 完成 |
| P2-6 | 写纯 HTTP 修复证据页 | `captures/spa/H-pure-http-sentinel-fix-20260722.md` | 完成 |

**禁止（P2）**：

- Camoufox / Playwright 暖机拿 Turnstile 再交给 HTTP  
- FlareSolverr / 任意 `turnstile_solver_url` 浏览器服务当根方案  
- 「先空 Turnstile 碰运气」软降级回生产默认  

### 3.2 Phase 3 — 纯 HTTP auto-tool 请求形状【生产已完成】

`picture_v2` HAR 路线不能稳定触发工具；当前采用已有真实出图证据的旧 auto-tool envelope，显式 `image_spa_tool_path=false` 才回退 picture_v2 canary。

| ID | 待办 | 文件 / 点 | 状态 |
|----|------|-----------|------|
| P3-1 | prepare：原始 prompt、空 hints、旧 web-push contextual、无 Sentinel | `services/protocol/chatgpt_web_request.py` / backend headers | 生产完成 |
| P3-2 | start：原始 prompt、无 user metadata/create_time、无 conduit/trace | 同上 | 生产触发 `image_gen` 并下载成功 |
| P3-3 | 默认 `image_spa_tool_path=true`；`false` 回退 picture_v2 | `services/config.py` | 完成 |
| P3-4 | 单测固定 strict envelope | `test_chatgpt_web_request.py` / transport isolation | 完成 |

### 3.3 Phase 4 — 门禁 + Panda 验收【P2+P3 之后】

| ID | 待办 | 验收 | 状态 |
|----|------|------|------|
| P4-1 | required 缺 Turnstile → **硬失败** | 错误码可观测 | 完成 |
| P4-2 | 生产/压测统一 `image_gen` deadline；canary 禁止 cid 假成功 | tool/file 才成功 | 完成；生产已记录 sediment 并下载 PNG |
| P4-3 | **先** Git 正式部署 Panda（禁 scp/远程 build），再压测 | 部署说明含仓库/分支/commit/unit；健康检查贴真实输出 | 完成：artifact `650e899084c3`，备份 `pure-http-prod-20260722-144517`，健康 `10/10` |
| P4-D1 | SSE 每行先解析，再检查 45 秒 deadline | 临界工具事件不会被 gate 前置丢弃；有回归测试 | **完成** |
| P4-D2 | 脱敏保存 SSE 事件时间线 | 有 `arrival_ms`、author/name/recipient、content_type、event/type/status；无 token/cookie | **完成** |
| P4-D3 | 细分失败分类 | `tool_args_as_text` / `late_image_gen_after_gate` / `no_image_gen_quiet_stream` 可区分 | **完成** |
| P4-D4 | 细分 CF 观测 | `home_403_soft_fail` / `requirements_cf403` / `start_cf403` / `tasks_cf403` / `propagated_cf` 分列 | **完成** |
| P4-D5 | 45 秒 gate fail 后只读监听同一 SSE 流至 **90 秒** | 不重提 conversation；迟到工具事件归类可见 | **完成** |
| P4-6b | Webshare **5 节点并发** CF403 预扫 + 串行5→并发4 门禁编排 | `scripts/spa_image_panda_acceptance.py --phase cf_scan5`；`serial5_passed` 后才允许 `concurrent4` | **完成（脚本）** |
| P4-7 | poll 429 熔断 + SSE 等 sediment + 额度本地扣减 + cf_probe 全链 requirements | 连续 3×429 快失败；bench/生产 success 同步 `limits_progress`；验收用 **邮箱** 标识账号 | **完成（2026-07-23）** |
| P4-4 | Panda sticky Webshare：**串行 5** | `no_image_gen=0`，出图成功 | **通过**（历史 65s gate）；2026-07-23 复验 **68–74s image_gen**，gate 临时 **90s**，上游延迟待办 **PROTO-UPSTREAM-LATENCY** |
| P4-5 | Panda sticky Webshare：**并发 4** | `no_image_gen=0`，出图成功 | 待办；串行已通过，可启动 |
| P4-6 | 收口：`H-pure-http-sentinel-fix-*.md` + 刷新 `12`/`04`/`06`/`CHANGELOG` | 文档与代码一致 | 完成（2026-07-22） |

### 3.4 仍属 PROTO-REFACTOR、但非本主线阻塞（并行可选）

这些在 `04` **PROTO-REFACTOR**；**不挡** P2–P4，但生图全链路长期要对齐：

| ID | 项 | 状态 |
|----|-----|------|
| R-1 | 上传链：`POST /files` → PUT oaiusercontent → `process_upload_stream`；附图默认 `sediment://` | 待办（见 D 专页） |
| R-2 | 搜索：生产继续 `system_hints:["search"]`；SPA UI HAR 可选 | HTTP 已证；UI 可选 |
| R-3 | 错误分类挂钩 `F-errors-20260721.md` | 待办 |

---

## 4. 验收定义（Definition of Done）

严格纯 HTTP 生图 **整条主线完成** 当且仅当：

1. **无浏览器数据面**：生产路径不启动 Camoufox/Playwright 取 Sentinel。  
2. **Turnstile**：required 时 finalize + SSE 均有非空 token。  
3. **触发**：SSE 流内出现 `image_gen`（或等价工具事件）。  
4. **出图**：`file_ids` 非空并完成下载/回传。  
5. **Panda**：串行 5 + 并发 4，`no_image_gen=0`。  
6. **文档**：本文件勾选与 `H-*` 专页、`04`/`12`/`CHANGELOG` 一致。  

---

## 5. 关键路径速查

| 角色 | 路径 |
|------|------|
| chat-requirements / finalize | `services/openai_backend_api.py`（`_get_chat_requirements_once`） |
| 生图 body / prepare | `services/protocol/chatgpt_web_request.py` |
| Turnstile VM | `utils/turnstile.py` |
| PoW | `utils/pow.py` |
| 压测 | `scripts/_tmp_spa_image_bench3.py`、`scripts/spa_image_load_test.py`、`scripts/spa_image_panda_acceptance.py`（`--account-email`） |
| finalize 单测 | `test/test_chat_requirements_finalize.py` |
| 现网 HAR（本地） | `docs/captures/spa/spa-image-20260721T144019Z.har` |
| 部署纪律 | `docs/deployment.md`；用户规则 `@panda-deploy` |

---

## 6. 与其它 backlog 的关系

- **PROTO-PURE-HTTP**（本文件）：当前 P0 主线；VM、strict envelope、正式发布与单单元下载已通；新 IP 串行 5 在 4/5 因 `no_image_gen` 止损，当前先做 deadline/事件观测修复，并发 4 未执行。  
- **CF 归因**：A/B 时新 IP 两次首页正常，同一 IP 续验时首页软失败 403 又达到 `4/4`，但业务链均未传播 CF；edge 状态会随时间/行为变化，现阶段不把任一 IP 判为确定性永久好/坏。  
- **PROTO-REFACTOR**：上传/sediment 等中长期改造；纯 HTTP auto-tool 主线优先于上传链。  
- **PROTO-ALIGN / SPA dig**：挖矿已完成；本文件是挖矿结论上的 **生产修复执行单**。  

---

## 7. 变更日志（本文件）

| 日期 | 说明 |
|------|------|
| 2026-07-23 | 串行 5 5/5 + 单账号 conc10 30/30；多账号 conc10 4/10；新增 §0.1 链路速查 |
| 2026-07-22 | 第 4 轮诊断纠正：首页软失败 403 为 4/4、业务链传播 CF 为 0；SSE 活跃且有类工具参数 JSON，发现 deadline 先于行解析的边界假阴性风险；新增 P4-D1～D5，观测修复前不补第 5 轮/并发 4 |
| 2026-07-22 | 同账号保持原 fp/session 换绑到新 IP `45.39.75.27`；串行 5 执行 4/5，前三轮成功、第四轮 `no_image_gen_within_45s` 后止损；当时仅记录业务 CF 分类为 0，首页软失败 403 见上方纠正；第 5 轮与并发 4 未执行 |
| 2026-07-22 | 同账号/同 fp/同 shape 新旧 IP A/B：新 IP `45.39.75.27` 2/2 无 CF 成功；旧 IP `82.29.223.111` 无 CF但下载 503；严格 IP 归因未成立 |
| 2026-07-22 | 固定账号/Webshare 串行 5 执行 2/5：第 1 轮 `no_image_gen`，第 2 轮成功；连续两轮有 CF403 信号，按门禁在第 3 轮前停止；未换号/未重试 |
| 2026-07-22 | artifact `650e899084c3` 正式部署 Panda；生产单账号 Webshare canary 出图/下载成功，`/tasks` 1 次 CF403 后恢复；串行 5 / 并发 4 按门禁暂缓 |
| 2026-07-22 | VM 真实 finalize 接受 + 一次纯 HTTP 真出图；Panda staging 单 canary 触发 `image_gen` 但 poll/download 遇 CF403；poll abort 修复定向 `34 passed`、扩展 `92 passed` |
| 2026-07-22 | 初版：汇总 P0–P4 已做/未做、否决项、验收与文件地图 |
| 2026-07-21 | （前序）G 专页 + bench deadline + Phase1 finalize 落地；P2 当时仍待推进，已于次日完成 |
