# 交接摘要

日期：2026-07-23（Asia/Shanghai）

历史过程：`docs/logs/2026/2026-07.md`。产出：`16`。协议/LLM：`11`、`12`、`19`、**`20`**。CF：`17`。待办：`04`。SPA 任务书：`18`。

## 当前生产（权威摘要）

- **新号产出（固定链路）**：见 `16`——取号 → 未占用 Webshare → 探活+邮箱预检 → Camoufox 注册 → blob 上传 Panda → 默认 `identity_isolated` 观察 → 成熟后开调度。
- **正式入口**：`scripts/outlook_camoufox_stable_register.py`（Outlook）；Proton 仍走 `proton_camoufox_*`。
- 人工调度：`set_account_scheduling` / `POST /api/accounts/scheduling`；进=`verified_ready`，出=`identity_isolated`。
- 代理：账号级 Webshare sticky；同 binding 有承载上限；**同 binding 同时生图默认 ≤1**；禁止把 Panda 宿主机 IP 当调度出口。
- **CF 403**：见 `17`——边缘拦，不能协议根除；IMAGE 启动/poll 均可 failover 换号；poll 连续 CF 快失败（非空挂 180s）；号池 CF 灯仅被动计数。
- **协议**：纯 HTTP 已正式部署并通过单单元。同账号保持原 fp/session 已生产换绑到新 IP `45.39.75.27`；新 IP 串行 5 续验为 `3 ok + 1 no_image_gen`，在 `4/5` 止损。纠正后的口径：首页软失败 403 为 `4/4`，业务链传播 CF 为 0，前三轮 `/tasks` CF 为 0，第四轮未进入 poll。第 4 轮 SSE 仍活跃但 45 秒内未识别到明确 `image_gen`；不能把 CF403 定性为旧 IP 唯一故障，并发 4 未执行。证据：`captures/spa/{J,K,L,M}-*.json`。
- 安全边界：`delete_invalid=false`；不自动清失败证据；注册机 UI/协议批量已停。

## 本轮已完成（07-20～07-22）

- Proton / Outlook 多轮 Camoufox 产出与共享 IP 观察；Panda 直连能注册不能刷 backend（CF 403）。
- 2026-07-21：Outlook `CharlieTim7490` + 新 Webshare `92.113.246.215` 进池观察；固化 `16` + `outlook_camoufox_stable_register.py`。
- 2026-07-21：CF 裁决入 `17`；生图任务 UI/调度缓解部署（`72ca9d5`）；任务书 `18`。
- 2026-07-21：密码重登 + YuMail OTP；SPA 生图 flag `image_spa_tool_path`（默认关）+ `spa_image_load_test.py`。
- 2026-07-21：**SPA 协议全量逆向 Now+Next**（Create Image=`picture_v2`；上传=`process_upload_stream`+sediment；search HTTP；暖机/cookie/差 IP）→ 专页 `captures/spa/`；看板 `19`。
- 2026-07-21～22：G 专页定性无 `image_gen`；bench `image-gen-deadline`/`no_image_gen`；finalize `proofofwork`/`turnstile`+空硬失败；全量待办写入 **`docs/20`** + `04` **PROTO-PURE-HTTP**。
- 2026-07-22：Turnstile VM 真接受；本地纯 HTTP 真出图；poll CF abort 回归 `34 passed` / `92 passed`。
- 2026-07-22：artifact `650e899084c3` 正式部署 4 个运行时文件，备份 `/root/gptimage/backups/pure-http-prod-20260722-144517`；生产健康 `healthy=true`、调度面 `10/10`。
- 2026-07-22：生产单账号/单 Webshare canary 成功，conversation `6a606849-e1b8-83ec-96e4-e7cfbbbf305b`，PNG `1254×1254`、`2,568,782` bytes；`/tasks` 1 次 CF403 后 conversation poll 恢复，未换号/未整单重试。
- 2026-07-22：固定账号/Webshare 串行 5 实际执行 2 次；第 1 次 `no_image_gen`，第 2 次成功下载 PNG。因两轮均出现 CF403 信号，按门禁在第 3 轮前停止；资源与生产健康正常。
- 2026-07-22：同账号/同 fp/同 shape 新旧 IP A/B：新 IP `2/2` 成功且无首页/`tasks` CF；旧 IP 单次无 CF但下载 503。裁决为间歇性 edge/endpoint/timing，IP 仅可能影响概率。
- 2026-07-22：目标账号生产换绑到新 IP；串行 5 续验前 3 次成功，第 4 次 `no_image_gen_within_45s` 后停止，第 5 次未运行。复盘确认 4 轮均有首页软失败 403，但业务链未传播 CF；第 4 轮收到 13 个 SSE chunks 与类似生图参数 JSON，存在 deadline 先于行解析造成边界假阴性的可能。生产终态 `healthy=true`、调度 `10/10`、`inflight=0`，新绑定保留。
- 2026-07-23：**Outlook 运维**——`charlietim`/`barnettregina` relogin 恢复 + reload API 教训；新号 `felicitypamela`/`andersmia` 观察进池（`16` / `data/runlogs/outlook-camoufox-stable-20260723/`）。
- 2026-07-23：**P4-7** 落地——SSE `complete_predicate` 等 `sediment://`；poll 连续 3×429 熔断+cooldown/换出口；`image_poll_initial_wait` 默认 20s（SSE 极早结束升至 25s）；额度 `remaining<0` 标未知；`mark_image_result` 同步扣减 `limits_progress`（修复 bench 多轮后额度仍显示 23）；cf_probe 走完整 requirements；验收账号标识改用 **邮箱**（弃用 hash 展示）。`qaflowakjewai6ps@proton.me` 串行 5/5 通过（65s gate）。证据 `captures/spa/O-panda-serial5-quota-account-postfix-20260723.md`。

## 下一步（按优先级）

1. **PROTO-PURE-HTTP / `docs/20`**：串行 5 已通过（`qaflowakjewai6ps@proton.me`）。下一步：`--phase concurrent4`（需 4 个邮箱）；部署 P4-7 运行时到 Panda 后复验额度扣减。
2. **PROTO-REFACTOR**：上传链 `process_upload_stream`+`sediment://`（不挡 P2，但长期要做）。
3. Panda 写 `YUMAIL_API_KEY` secret；对目标号点恢复验收。
4. 新号一律走 `outlook_camoufox_stable_register.py`（或 Proton 等价脚本）；观察成熟后再开调度。
5. STORE/LOG 工程化（`15`）。
6. **RUST-001**：独立仓 `gptimage-gateway-rs`——Phase A + 鉴权/UI + Phase B 契约已落地；生图运行时默认关，待 `IMAGE_ENABLED=1` 后接矩阵；路线见该仓 `plan.md`。

## 禁止

- Panda 上 build / scp 当正式代码发布。
- 用 Panda 直连 IP 跑生产 `/backend-api`。
- 宣称「协议绕过 CF」或把 FlareSolverr / 浏览器 Turnstile solver 当根方案。
- 机械假聊；清 invalid 证据强塞调度；无 sticky 大批量扩池。
- 浏览器作生图数据面或暖机兜底（严格纯 HTTP 红线，见 `20`）。
