# H — 纯 HTTP Sentinel / Turnstile 突破与正式链路收口（2026-07-22）

## 结论

严格纯 HTTP 生图已正式上线并通过单单元。串行 5 在 2/5 触发 CF 门禁后，新旧 IP 同条件 A/B 显示新 IP 两次无 CF 成功，旧 IP 本次没有 CF但下载 503。故 CF403 不是旧 IP 的确定性复现故障，更可能是间歇性 edge/endpoint/timing；IP 可能影响概率，但尚未证实为唯一主因。批量稳定性仍未通过，并发 4 禁止启动。

## 已证成功

- 数据面：`curl_cffi` HTTP；未启动 Camoufox、Playwright、FlareSolverr 或外部 solver。
- Turnstile：本地 VM token 被 `/sentinel/chat-requirements/finalize` 接受。
- conversation：`6a602f69-54a0-83ec-8312-945864ce7e52`。
- SSE：出现 image tool，随后 conversation 中存在 tool multimodal message。
- 下载：PNG `841,139` bytes，证据文件 `data/runlogs/spa_repro/bench3/local_clash_1784688517_0.png`（runlogs 不入库）。

已证 envelope 的关键约束：

- 原始 prompt，不加 `@Create image`。
- top-level `system_hints=[]`。
- `client_contextual_info` 使用旧字段 `app_name` / `is_web_push_capable` / `is_web_push_enabled`。
- prepare 不带 Sentinel；start 带 requirements/proof/Turnstile。
- 无 `X-Conduit-Token`。

## 正式链路本轮结果

- canary `6a6032b1-9318-83ec-97d9-723991ab0d1b`：requirements/finalize/prepare/start 均成功，无 CF403/TLS35；只产生 code message，24 次 conversation poll 后无 tool/附件。
- 剥离 user-message 空 metadata 后，canary `6a60348d-5c38-83ec-b908-ad6f21691afe` 仍只到 code。
- 随后一次只读 conversation 查询返回 CF403，按降风控原则立即停止探针。
- 离线继续发现并修正：SPA user message 不应带 `create_time`；start 不应带 `X-Oai-Turn-Trace-Id`；OAI-Language 应按时区推导，不沿用账号 `Accept-Language` 主标签。
- 上述两次结果仅保留为 envelope 收敛历史；后续 Panda staging 已用最终严格 envelope 触发 `image_gen`，见下节。剩余问题转为正式发布与下载阶段 CF403。

## CF403 降压措施

- 图片路径调用 `_get_chat_requirements_once()`；不再复用文本路径内部的 3 次 CF 重试。
- CF/边缘 403 不进入 transport retry，立即交给上层换号。
- curl(35)/curl(56) 才有限重试；重建 session 保留原 proxy、TLS verify、impersonate 和 timeout，避免换出口/换 TLS 语义。
- canary 成功条件改为 image tool/file；conversation_id 仅表示提交被接受。
- poll 连续两次 CF edge block 后抛出带 `cf_abort` 的 `UpstreamHTTPError`；resolver 不再把该错误吞成 partial error 后继续请求附件 URL。

## Panda Webshare staging canary（2026-07-22）

- 运行方式：隔离 staging 容器，单账号、单请求、纯 `curl_cffi` HTTP；未覆盖生产挂载、未重启主服务。
- 脱敏身份：account hash `f289854023ad`；Webshare proxy hash `49675fdabb54`；观测出口 `92.113.246.176`。
- 结果：conversation `6a604b04-d4f8-83ec-9ada-df3d82276d85`，SSE `has_image_gen=true`，收到 sediment ID `file_00000000584881fab11f12fe745f3f9b`；未出现 download URL/PNG。
- 失败：`failure_stage=poll_download`，`/tasks` 与 conversation 连续两次 `403 cloudflare_or_edge_html_block`。原子脱敏证据：[`I-panda-webshare-pure-http-canary-20260722.json`](I-panda-webshare-pure-http-canary-20260722.json)。
- 说明：证据中的 `legacy_attachment_request_after_abort=true` 是修复前该次运行已发生的历史事实；随后本地已加 abort guard，不能外推为当前代码仍会继续发请求。
- Panda 复核：canary 后健康页仍 `healthy=true`，可用内存 `1276 MiB`、归一化负载 `0.08`、根盘 `61%`，隔离容器已退出；未执行第二账号、串行 5 或并发 4。

## Panda 正式发布与生产单单元 canary（2026-07-22）

- 发布：通过 Git/artifacts 最小 overlay 部署 `services/openai_backend_api.py`、`services/protocol/chatgpt_web_request.py`、`services/config.py`、`utils/turnstile.py`；artifact commit `650e899084c319ede7436c7b9497b4af9b991eba`，备份 `/root/gptimage/backups/pure-http-prod-20260722-144517`。未在 Panda build，未 scp 业务代码。
- 发布后：`healthy=true`，`image_schedulable=10`、`dispatchable=10`、`inflight=0`、`startup_errors=0`。
- canary：单账号、单 Webshare、单请求、`0.5 CPU / 512 MiB / 300s`。conversation `6a606849-e1b8-83ec-96e4-e7cfbbbf305b`，`has_image_gen=true`，sediment ID `file_0000000086d081fbb4856bd42f0b94c3`。
- 下载：PNG `1254×1254`、`2,568,782` bytes，SHA256 `1f886e15532bfc9973897a9d285f311ecadeedaa0346dc0101df25629e5fa5bd`；总耗时 `42,246ms`，流量 `2,689,545` bytes，成功率 `1/1`，`no_image_gen=0`。
- CF：`/tasks` 出现 1 次 CF403；随后 conversation poll 成功。未达到连续 2 次 abort 阈值，未整单重试、未换号。因事前门禁规定任一 CF403 停止扩测，未继续串行 5 / 并发 4。
- 结束状态：可用内存 `1427 MiB`、归一化负载 `0.03`、根盘 `61%`、健康与调度面仍 `10/10`、`inflight=0`，canary 容器已退出。
- 脱敏原子证据：[`J-panda-production-pure-http-canary-20260722.json`](J-panda-production-pure-http-canary-20260722.json)。

## 本地验证

- 定向：`34 passed, 1 warning`。
- 生图/轮询/代理隔离/Turnstile/标准 `/v1/images` 最新扩展受影响回归：`92 passed, 1 warning`。
- Panda production：已正式部署，单单元 `image_gen` + PNG 下载成功；未做串行/并发压测。
- Panda staging canary：历史上已触发 `image_gen`，但因连续 CF403 未下载；该问题经 poll abort 修复后进入上述生产单元复验。

## Panda 固定账号/Webshare 串行 5（2026-07-22）

- 限制：account hash `3b18db641494`、Webshare hash `b2f3cb7639c2`、出口 `82.29.223.111`；并发 1；`0.5 CPU / 512 MiB / 128 PIDs`；单请求硬超时 `300s`、整轮 `1800s`、image_gen deadline `45s`、轮间 `15s`。
- 第 1 轮：conversation `6a6071a3-4370-83ec-bde3-3e731a7f68ef`，`50.615s`，`no_image_gen_within_45s`；首页一次 403 后 requirements 成功。
- 第 2 轮：conversation `6a6072db-6fe8-83ec-9519-90d545255fe4`，`43.942s`，出图并下载 PNG `1254×1254`、`2,475,088` bytes，SHA256 `208d8d1bf0da35d9d0f17096fe126430eda79b087e25466951fb326e2ba742fe`；首页一次 403，`/tasks` 一次 CF403（streak=1）后 conversation poll 恢复。
- 止损：连续两轮均出现 CF 信号，停止在 2/5；未启动第 3 轮，未换号、未整单重试、未执行并发 4。
- 资源：最低可用内存 `1375 MiB`，最高归一化负载 `0.21`，canary 峰值内存 `58.91 MiB`；结束后 `healthy=true`、调度 `10/10`、`inflight=0`、canary 容器 0。
- 原子证据：[`K-panda-production-pure-http-serial5-20260722.json`](K-panda-production-pure-http-serial5-20260722.json)。

## Webshare 新旧 IP 同条件 A/B（2026-07-22）

- 固定项：account hash `3b18db641494`、fp hash `e595e15a2a6e2fb0`、prompt、prepare/start shape；无整单重试、无换号。新代理仅用于隔离测试，没有写回生产绑定。
- 新 IP `45.39.75.27`：首测 `42.301s`、复测 `37.714s`，均触发 `image_gen` 并下载成功；首页和 `/tasks` CF403 均为 0。
- 旧 IP `82.29.223.111`：按要求只测 1 次，`image_gen=true`，首页和 `/tasks` CF403 均为 0；下载阶段返回 `503 ServerBusy`，未重试。
- 裁决：新 IP 表现明显更好，但旧 IP 本次未持续 CF403，因此不满足“旧 IP 持续 CF”的严格归因标准。当前证据支持间歇性 edge/endpoint/timing，IP 可能改变风险概率但不是已证唯一原因。
- 资源：最低可用内存 `1314 MiB`、最大归一化负载 `0.32`、canary 峰值内存约 `65 MiB`；结束后 `healthy=true`、调度 `10/10`、`inflight=0`、残留容器 0。
- 原子证据：[`L-panda-webshare-ip-ab-20260722.json`](L-panda-webshare-ip-ab-20260722.json)。

## 下一步门禁

1. 保持 artifact `650e899084c3` 当前生产版本与回滚备份，不自动追加 canary。
2. 优先降低首页暖机与 `/tasks` CF 暴露，并进行冷却观察；同时分析第 1 轮 `no_image_gen`。
3. 人工放行后重新规划剩余串行验收；只有串行达到 `no_image_gen=0` 且无 CF 放大，才允许并发 4。
