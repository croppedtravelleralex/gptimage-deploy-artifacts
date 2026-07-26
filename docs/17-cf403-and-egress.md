# 17 — CF 403 / 边缘拦截：成因、历史、可做与不可做

最后更新：2026-07-26（Asia/Shanghai）

## 同出口 IP、不同账号为何 CF 表现不同？

**结论**：CF 403 不是「纯 IP 黑名单」二值开关，而是 **IP 信誉 × 账号会话 × TLS/设备指纹 × 请求路径 × 并发形态** 的联合判定。同一 Webshare 出口 IP 上，A 号 `requirements` 200、B 号 `conversation/init` 403 是正常现象。

| 维度 | 说明 |
|------|------|
| **账号会话** | 每号独立 `access_token`、Cookie、`oai-device-id` / `oai-session-id`（见 `account_fingerprint.py`）。CF 对已登录会话有单独信誉；暖号（刚 serial5 成功）与冷号（久未 bootstrap）不同。 |
| **TLS 指纹** | 同 IP 下各号 `impersonate`（chrome120/124/131）、UA、CH 可能不同；curl_cffi 栈与真浏览器仍有差，**按账号**触发率不同。 |
| **请求路径** | `home` 软 403、`requirements`、`conversation/init`、`/tasks` 分层统计（bench `cf_layers`）。同 IP 上常见：**首页偶发 403 但 requirements 仍 200**（serial5 证据）。 |
| **被动 CF 灯** | 号池 `cf_daily` **按账号**累计业务路径 CF，非按 IP 共享；同 binding 两号灯色可完全不同。 |
| **并发突发** | 2026-07-23 conc10 多账号轮询：`qaflow` 等暖号成功，`andersmia`/`felicity` 等 **同轮 10 路并发** 在 `init` 403；非「IP 坏了」，而是 **冷号 + 突发 + 账号级拦截**。 |
| **binding 上限** | `image_binding_inflight_max` 默认同出口同时生图 ≤1；多账号同 IP 并发会放大 edge 拦截概率。 |

**工程含义**：不能用「这个 IP probe 过了」推断该 IP 上所有账号都能 conc10；验收应 **先 serial5 暖号 / CF 探活**，再扩并发；conc10 应用 **暖号池或限制同 binding 并发**，而非 13 冷号同时 `init`。

## 一句话

`cloudflare_or_edge_html_block` / HTTP 403 HTML challenge 是 **Cloudflare 边缘对出口 IP + TLS/行为** 的拦截，不是本仓缺某个 Header 就能「协议绕过」。可持续策略是 **好出口 + 限同 IP 并发 + 撞 CF 立刻换号**；协议层只能逼近 Web，不能根除。

## 现象与识别

| 信号 | 含义 |
|------|------|
| `cloudflare_or_edge_html_block` | 上游返回 HTML challenge / 拦截页 |
| `cf_edge_block:` / `is_cf_edge_chat_error` | 代码侧归类为边缘拦（≠ token 失效） |
| 用户文案曾误报 `upstream image connection failed` | 旧逻辑把含 `proxy` 的 CF 文案误判为 TLS；已改为优先 CF 文案 |
| 生图空挂 ~300s 后 `ImageStreamCancelledError` | 常为 soft-bootstrap / 同号空转拖到任务硬超时，而非「全局并发坏了」 |

常见触发点：`bootstrap`（首页）、`chat_requirements_finalize`、`/backend-api/f/conversation` SSE。

## 根因（按证据强度）

1. **出口 IP 信誉**（最强）：共享 Webshare / 机房 IP 被标；同 `proxy_binding` 多号突发更容易撞。
2. **Panda 宿主机直连必炸**（已验证）：`43.156.233.219` 可过 Camoufox 注册页，空代理刷 `/backend-api`/`/me` → CF 403。见 `02`/`16`（2026-07-20 canary）。
3. **协议栈 ≠ 真浏览器**：`curl_cffi` impersonate 仍间歇 403；并发放大。
4. **历史软失败拖死**：IMAGE 路径 bootstrap CF 曾 soft 回退默认 PoW，不换号，烧到硬超时。

## 以前有过吗？

**有，长期反复。**

- 2026-07-20：Panda IP canary → 直连 backend CF 403（`02`/`06`/`16`）。
- Olivia `/me`：全头间歇 403，轻量头更稳——同类边缘拦。
- 文本路径早有 `text_stream_cf_failover`；生图此前缺对等换号，表现为暴死/空挂。
- 近 24h（部署缓解后采样）：仍可见少量 `bootstrap_soft_failed` / `cloudflare_or_edge_html_block`（偶发，非清零）。

## 「协议绕过」裁决

| 手段 | 结论 |
|------|------|
| 改 Header / 对齐请求序 | 降误伤；IP 已被标时无效 |
| `curl_cffi` Chrome 指纹 | 已在用；仍间歇 CF |
| soft-bootstrap / 默认 PoW | **不能**过 challenge；IMAGE 已改为 `soft_fail=False` 硬失败以便换号 |
| FlareSolverr 解挑战 | 脆弱，**不纳入**生产根方案 / Rust 重写 |
| Camoufox 真浏览器拿会话 | 注册/验号更稳；API 反代仍受出口限制 |
| **协议层稳定绕过 CF** | **不可行 / 不可承诺** |

## 已落地缓解（2026-07-21）

- IMAGE：`image_stream_cf_failover`（demote + 换号，`pre_conversation_max_attempts`↑）。
- IMAGE bootstrap：`_ensure_bootstrap(soft_fail=False)`，避免空转至 ~300s。
- 同 `proxy_binding` 同时生图默认 ≤1（`image_binding_inflight_max`）。
- `submit_start_min_interval_ms`≈1500；硬超时组件收紧（pre≈45、poll≈120）。
- 文案：CF 优先于 connection failed。
- **Poll 快失败（2026-07-21，2026-07-24 增强）**：连续 CF 达 `image_poll_cf_abort_streak`（默认 2）前先做 **短退避重试**（`image_poll_cf_retry_backoff_secs` 默认 1.5s）；abort 后可 **换出口再 poll 一轮**（`image_poll_cf_swap_retry`）。纳入 `image_stream_cf_failover` / `proxy_cf_failover` 体系。
- **号池 CF 灯（被动）**：账号 `cf_daily` 仅在真实业务成功/CF/生图失败路径累计；**不做**定时主动刷 `/me`/`/tasks` 探 CF。

## 根除？

**不能 100% 根除**（策略在 CF/OpenAI 侧）。业务目标应改为：发生率低到「偶发 + 自动换号恢复」，用户无感。

继续提高：独立高信誉出口数量、坏 IP 黑名单、浏览器 bootstrap 与 sticky 同出口绑定（见 `09`/`12`）。

## 票与出口关系（2026-07-23 修正）

> 依据：`P-sentinel-ticket-ablation-20260723` + `Q-sentinel-ticket-validation-20260723`（全量生图 + 带宽计量）。

| 旧结论 | 新证据 | 生产建议 |
|--------|--------|----------|
| 开票与用票**必须同出口 IP** | `cross_ip` 串行 5/5、并发 30/30 成功；开票 `92.113.x`、用票 `104.252.x` 等组合均 OK | **不强制**消费绑开票 IP；sticky 仍用于账号信誉与 CF 灯 |
| CF403 ≈「这个 IP 废了」 | 同 Webshare 出口上：A 号 requirements 200、B 号 init 403；cross_ip 用票仍可 200 | CF 按 **账号会话 × 路径 × 突发** 判定；换 IP 用票**不能**替代换号 |
| 票可缓存省 finalize | 立即复用 CF403；60s 可过 | 维持 **per-call finalize**；Rust 票池 TTL=300s 仅缓存未消费票；间隔/立即复用 **实验占位** |
| IP 漂移导致封号 | 跨 IP 用票未触发 token 失效；失败形态仍为 CF403 HTML | 「漂移」风险在 **行为频率**，非票字段绑 IP |

**不变**：CF 仍不可协议根除；`image_stream_cf_failover`、binding inflight≤1、暖号后再扩并发。

## poll CF403：每号独立 IP 仍会被打（2026-07-24）

> 依据：`sentinel-ticket-validation-20260723-production` — mixed-proxy conc10 + **own-proxy / unique-egress** conc10（`scripts/sentinel_ticket_validation_suite.py --own-proxy-only --unique-egress`）。

### 现象

开票 + SSE 已成功（`has_image_gen=true`、`sediment_ids` 已有），失败发生在 **poll** 阶段：

```
GET /backend-api/conversation/{id}
GET /backend-api/tasks
```

错误形态：`cloudflare_or_edge_html_block`，`image_poll_cf_abort_streak=2` 后快失败（见「Poll 快失败」）。

**这不是 sentinel 票失效**：票只保护 requirements → prepare → finalize → SSE `f/conversation`；**poll / download / attachment 不走票头**，是普通高频 API。

### 「每号一个 IP」能解决什么、不能解决什么

| 策略 | 解决 | 不解决 |
|------|------|--------|
| 每号 **自有 proxy**（不借 `cross_ip` alt_proxy） | 消除「多账号共享同一 egress、poll 叠加」 | 单 IP 在 burst 内仍可能被 CF 按 **per-IP** 阈值拦 |
| **`--unique-egress`**（选号时 proxy / `proxy_egress_ip` 不重复） | 10 路不再挤在同一 `92.113.x` 等重复出口 | 10 个不同 IP **同时** 自动化 poll，仍有个别 IP 中招 |
| 仅换 IP、不换号 | 有时缓解 | CF 按 **账号会话 × IP × 路径** 联合判定；冷号/坏 reput IP 仍挂 |

### 2026-07-24 own-proxy conc10 实测（Panda）

配置：10 worker，`--own-proxy-only --unique-egress`，10 个 egress 均不同（`82.21.x`、`92.113.x`、`104.252.x`、`45.39.x` 等）。验证脚本内置 **worker 错峰 2s**（`stagger_ms=i*2000`）。

| 轮次 | 结果 | 主要失败形态 | 备注 |
|------|------|--------------|------|
| R1（poll 重试前） | 5/10 | poll CF403 | 缺 `image_poll_cf_swap_retry` 全链路 |
| R1（poll 短退避 + swap，sync 不全） | 8/10 | `no_image_urls`（部署缺 config） | — |
| R1（全量 sync + swap 1 轮） | 9/10 | 单号 poll CF abort | — |
| R1（**无错峰** 连续压） | **1/10** | **Azure 503 ServerBusy**（图生图 upload） | 非 CF；见上节 |
| R1（错峰 + 轮间冷却 90s + **swap 最多 5 轮**） | **10/10** | — | `barnettregina` / `qaflowfbdb3ovksr` / `qaflow0ytb7bbp0z` 各经 1–3 轮 swap 救回 |

对比 **mixed-proxy conc10**（偶数 worker `cross_ip` 借池内他号 proxy）：更多 egress 碰撞 + 同段突发，poll CF403 率更高。

### 为何 10 路各自不同 IP，个别 IP 仍会被 CF 边缘打？

CF 不是「同 IP 才限流」的二值开关，**每个 IP 独立评分**，且叠加以下维度：

1. **路径敏感度**  
   poll/tasks 无页面导航上下文、重复 GET、间隔固定（如 3s）→ 自动化形态明显；日志常见 **`/tasks` 先 403**，随后 `conversation` 连续 2 次触发 abort。

2. **单 IP 信誉**  
   Webshare 池质量不均；机房/代理段被标过的 IP，**一次 conc10 burst** 即可 403，无需与其他账号共享。

3. **账号 × IP 组合**  
   同 IP 上 A 过 B 挂是常态；与「是否独立 IP」正交。

4. **段级 / 行为级风控**  
   10 IP 可能同属相近 ASN/机房；CF 可挑最差几个先拦，而非 10 个全挂。

5. **协议栈**  
   `curl_cffi` impersonate ≠ 真浏览器；poll 阶段无 ticket 加成，边缘拦截率高于开票链。

### 并发上限限的是什么？（不是「一张票只能并发 N」）

| 层级 | 配置 | 目的 |
|------|------|------|
| 票 | per-call finalize | 每请求一票；票可跨 IP，**不是**票并发配额卡死 |
| 单账号 | `image_account_concurrency` | 同号连打、上游空转 |
| 同出口 | `image_binding_inflight_max`（默认 1） | **多账号同 binding** 时限制同时 poll |
| 全局 | `image_global_concurrency` | 总 inflight、保护上游与本机 |

conc10 的压力来自 **多路生图全流程同时在 poll**，不是「一张 sentinel 票只能开一次」。

### 工程缓解（按优先级）

1. **每号 sticky 自有 proxy**（生产默认）；压测用 `--own-proxy-only --unique-egress` 验收。
2. **`image_binding_inflight_max=1`** + 控制 `image_global_concurrency`（暖号池后再拉满）。
3. **poll CF 快速重试（2026-07-24 落地）** + **换出口再 poll 一轮**（见下）。
4. **`image_stream_cf_failover`**（SSE 阶段换号）。
5. **轮间冷却 / 降并发**（如 10→6）若仍偶发 poll CF。
6. **不要**指望仅「每号一 IP」在 conc10 下 **0** poll 403。

### 推荐组合：单号单 IP + poll CF403 快速重试

| 层 | 机制 | 配置 / 代码 |
|----|------|-------------|
| **预防** | 每账号 sticky 自有 proxy；同 binding 在途 ≤1 | 账号 `proxy` 字段；`image_binding_inflight_max=1` |
| **poll 内** | CF 后 **短退避再 poll**（默认 1.5s），连续 N 次才 abort | `image_poll_cf_retry_backoff_secs`；`image_poll_cf_abort_streak`（默认 2） |
| **poll 外** | `cf_abort` 后 **换 Webshare 出口**，同 `conversation_id` 再 resolve（最多 N 轮） | `image_poll_cf_swap_retry=true`（默认开）；`image_poll_cf_swap_retry_max=5`；`proxy_cf_failover.swap_account_proxy_on_cf` |
| **SSE 外** | 开票/SSE CF → 换号重试 | `image_stream_cf_failover` |

**为何两者要一起用**

- **单号单 IP**：去掉多账号挤同一 egress 的叠加；不保证单 IP 在 burst 下不过 CF。
- **快速重试**：瞬时 CF 抖动时，1–2s 退避后同 IP 常能恢复；仍失败则 **换出口再 poll**（SSE 已成功、sediment 仍在，不必重开票）。
- **不是票并发限制**：每张请求独立开票；限制的是 **poll HTTP 并发形态** 与 **出口信誉**。

**调参建议**

```json
{
  "image_binding_inflight_max": 1,
  "image_poll_cf_abort_streak": 2,
  "image_poll_cf_retry_backoff_secs": 1.5,
  "image_poll_cf_swap_retry": true,
  "image_poll_cf_swap_retry_max": 5
}
```

压测仍建议 `--own-proxy-only --unique-egress`；生产不必 borrow `cross_ip` proxy。

### 图生图「Azure 上传限流」≠ 换上游（仍是 OpenAI）

图生图/多模态参考图走 **OpenAI 后端 API**，但物理上传分两段：

```text
POST chatgpt.com/backend-api/files  →  返回 upload_url（*.blob.core.windows.net）
PUT  upload_url（x-ms-blob-type: BlockBlob）  →  Azure Blob 落盘
POST .../files/{id}/uploaded  →  OpenAI 确认
```

| 现象 | 来源 | 与 CF403 关系 |
|------|------|----------------|
| `503 ServerBusy` / `Ingress is over the account limit` | **Azure 存储入口**（OpenAI 租用的 Blob 账号配额） | **无关**；发生在开票/SSE 之后、poll 之前 |
| `cloudflare_or_edge_html_block` on `/tasks` | CF 边缘 | poll 阶段；可用 swap 重试 |

**为何压测里像「Azure 限流」**：10 路 **同时** 触发 `image_edit` 上传（无错峰）时，10 个 PUT 几乎同时打到同一 Azure 租户入口 → `ServerBusy`。`openai_backend_api._upload_image` 已对 503/500 做指数退避（最多 5 次）；**worker 错峰 2s**（`stagger_ms=i*2000`）+ 轮间冷却 60–90s 后，conc10 可恢复到 **10/10**。

生产建议：图生图并发受 `asset_upload_concurrency` / 全局 inflight 约束；burst 压测须错峰，勿把 Azure ingress 限流误判为「票失效」或「出口坏了」。

### dup_binding / dup_egress 与进调度 vs 生图可用

| UI / health 字段 | 含义 | 典型排除原因 |
|------------------|------|----------------|
| **进调度** `schedulable` | `status=正常` + 人工开关 `verified_ready` | 限流、已出调度（`identity_isolated`） |
| **生图可用** `image_schedulable` | 进调度 + 额度/CF/绑定/失败证据等闸门 | 见 `GET /api/accounts/schedulable-breakdown` |

- **`proxy_binding_max_accounts`**（Panda 生产 **2**，2026-07-25 起）：同一 egress 最多挂载账号数；超过则 `excluded_by_dup_egress`。
- **2026-07-25**：9 个 `cf_fail` 号换绑至 7～8 个 CF-ok 空闲节点，允许 **单 IP 2 号**（`scripts/_tmp_rebind_cf_fail_shared_ip.py`）。证据 `captures/spa/T-cf-fail-rebind-shared-ip-20260725.json`。
- **2026-07-24**：6 号共 `82.21.231.148:7462` → `dup_proxy_binding`；换绑后 **17/17**（当时 `proxy_binding_max_accounts=1`）。

### 批量 scan 隔离 vs 账号 CF 打标（2026-07-26）

**现象**：号池 **进调度 17 / 生图可用 0**；账号 `proxy_cf_ok=true` 仍不可调度。

**根因**：

1. `webshare_cf_scan` + `auto_quarantine=true` 将池内节点批量写入 `gpt_unavailable_proxies.json`（`reason=cf403_scan`）；2026-07-26 凌晨曾 **100/100** 全隔离。
2. `is_proxy_cf_ok_for_image()` 若 **先查隔离、后看 `proxy_cf_ok` 缓存**，则已打标账号也被挡在生图闸门外（`schedulable-breakdown` 显示 `primary=other`）。

**已落地修复**：

| 改动 | 文件 |
|------|------|
| 账号 `proxy_cf_ok` 缓存 **优先于** 批量隔离 | `services/proxy_cf_eligibility.py` |
| `auto_quarantine` **跳过** 当前账号已绑定 endpoint | `services/webshare_cf_scan_service.py` |
| 运维恢复：清隔离 + live 探活打标 | `scripts/_tmp_recover_cf_quarantine.py --apply` |

**运维注意**：`stamp` 成功会 `clear_gpt_unavailable`；但定时 scan 可能再次写入。**勿**仅凭 `gpt_unavailable_proxies.json` 判断在绑号是否可生图——以 `image_schedulable` + breakdown 为准。

### 验证脚本开关（`sentinel_ticket_validation_suite.py`）

```bash
# 每号自有 IP + egress 不重复（推荐压测形态）
python scripts/sentinel_ticket_validation_suite.py cross-concurrent --round 1 --workers 10 \
  --own-proxy-only --unique-egress

# 可选：取号前 fetch_remote_info（默认关闭；生产取号路径仍保留 preflight）
# ... --preflight
```

编排：`python scripts/_tmp_run_sentinel_validation.py cross-concurrent-own-proxy --from-round 1 --to-round 1`

证据目录：`data/runlogs/spa_repro/sentinel-ticket-validation-20260723-production/`（`cross_concurrent_report.json`、`events.jsonl`）。

## 相关

- `12-protocol-gap-vs-web.md`（协议差距）
- `16-camoufox-stable-pipeline.md`（注册出口）
- `02-current-state.md` / `06-handoff.md`
- 代码：`services/protocol/conversation.py`、`services/openai_backend_api.py`、`services/account_service.py`
