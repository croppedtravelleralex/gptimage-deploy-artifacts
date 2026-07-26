# 29 — 生产溯源审计（2026-07-26 夜）

最后更新：**2026-07-26**
状态：**权威**（本次审计的事实基线；纠正 `06` / `plan.md` 中「Panda 与仓库一致」的隐含假设）
方法：主控 + 5 个并行只读子代理（docs / 本地代码 / Panda 部署 / Panda 运行时 / 三方哈希对比），主控逐条复核关键结论
关联：`28-scheduling-queue-slot-audit-20260726.md`（代码审计）、`17-cf403-and-egress.md`、`04-improvement-backlog.md`

> **口径**：本文只记录**已复核**的事实。子代理报告中未经主控独立验证的结论一律不写入；被推翻的结论在 §6 单列。
> 全程只读：本地与 Panda 均未做任何修改。

---

## 0. 一句话结论

AUDIT-28 的 11864 行修复**已上线且运行时确认生效**，但它落在一个**溯源已断裂的地基**上，并且**上线后零生图流量验证**。

---

## 1. AUDIT-28 上线事实

| 项 | 事实 |
|----|------|
| 提交 | `9b15453` `fix: remediate AUDIT-28 scheduling/queue/slot findings` |
| 时间 | 2026-07-26 20:55 +0800 |
| 规模 | 50 文件，**+11864 / −380**；12 个新测试套件 |
| 上线路径 | `git fetch deploy audit28-remediation` → `git checkout FETCH_HEAD -- <28 paths>` |
| 落盘 | Panda 2026-07-26 21:06:32（28 个文件同一秒） |
| 重启 | 容器 `chatgpt2api-local` StartedAt `2026-07-26T13:07:10Z`，`RestartCount=0` |
| 内容校验 | 28/28 文件字节级等于 `9b15453` 对应 blob |

**这次上线走的是 git，没有违反部署铁律。** 本项目 Python 代码走 bind mount（compose 挂载 `./api ./services ./utils ./scripts ./native ./web_dist` 为 `:ro`），改文件 + 重启即生效，从来不需要在 Panda 编译。

### 1.1 运行时确认生效（非纸面完成）

```
slot_ledger      backend=rust, rust_load_error=null, forced_releases=0
watchdog         独立线程 pipeline-watchdog-loop, running=true, tick_count=266
                 force_release_expired=true      ← A3-1 生效（原硬编码 False）
reconcile        force_enabled=true, corrected=0  ← A3-1 生效
ss 池            active/queued/limit = 0/0/10     ← A3-3 生效（原取错 key 恒为 0）
deadlock guard   cpu_budget_source=cgroup_v2      ← A4-6 生效
inflight_drift   drift_count=0
```

批次 0/2/3/4 的开关均已验到运行时真的翻转。

---

## 2. 风险 A（最尖锐）：AUDIT-28 悬在 index 里

```
git merge-base --is-ancestor 9b15453 HEAD   →  NO（已分叉）
git diff --cached --name-only | wc -l       →  28

slot_ledger.py   工作树 = b4b6010a = 9b15453 的 blob   ← 在跑的新代码
                 HEAD   = 85d9fa9c                     ← 快照里的旧代码
```

Panda HEAD 停在 `2474d48`（分支 `pre-audit28-snapshot-20260726`，今天 21:03 新建，一个 commit 吞了 11070 文件 / 280 万行）。28 个文件以 **staged 状态悬空**，HEAD 从未前移。

**后果**：任何人在 `/root/gptimage` 执行一次 `git reset --hard` 或 `git checkout .`，AUDIT-28 全部 11864 行**被静默回滚**，且日志无异常痕迹。

**处置**：在 Panda 上把 index 落成 commit（纯本地 `git commit`，不推、不改代码、不重启）。代价接近零。

---

## 3. 风险 B：19 个文件不在任何 commit 里

```
git diff --name-only --ignore-cr-at-eol 9b15453 -- services api utils  →  19
```

| 类别 | 数量 | 说明 |
|------|------|------|
| 与**本地未提交工作树逐字节相同** | 16 | 两边都有、两边都没提交 |
| 本地**根本不存在** | 1 | `services/register/domain_intel.py`（505 行） |
| prod **落后于已提交的修复** | 1 | `services/yumail_otp.py` |
| prod 独有 `.bak` | 1 | `openai_register.py.bak-pre-single-reg-20260717144445` |

### 3.1 那 16 个是有功能的活代码

| 文件 | 变更 | 内容 |
|------|------|------|
| `services/proxy_cf_failover.py` | +92/−5 | `pick_swap_proxy()`、egress IP 去重、CF 验证门、`PANDA_POOL` 路径 |
| `services/proxy_quarantine.py` | +60 | 池加载时隔离过滤（`include_quarantined`）+ 无干净节点时回退 |
| `services/image_service.py` | +42/−9 | 缩略图 PNG→WebP（quality 82, method 6）+ legacy PNG 回退 |
| `services/image_sync_adapter.py` | +23/−3 | `preferred_account_email` 透传 + `compact_task_heavy_fields()` |
| `api/ai.py` | +14/−2 | `X-Preferred-Account-Email`、`n`(1-4)、`prompt_enhance`、`multi_image_mode` |
| `api/image_tasks.py` | +12 | 同步 ETA 的座位上限封顶 |
| 其余 10 个 | 小 | `log_service` 去重、`database_storage` 防御性拷贝等 |

这些**正在生产跑**，但在 GitHub 任何 ref 上都不存在。唯一副本是本机工作树 + Panda 工作树。任一硬盘故障即丢失。

### 3.2 `domain_intel.py` — 505 行孤儿死代码

```
grep -rn "domain_intel" services/ api/ utils/ scripts/ --include="*.py"   →  无结果
```

live tree 无任何引用，唯一引用在 `backups/git-artifacts-deploy-20260718-110441/`。本地仓库完全没有这个文件。

### 3.3 `yumail_otp.py` — 反方向问题：已提交的修复没上线

```diff
-    "código",
-    "codigo",
-    "inicio de sesión",
```

本地 HEAD **和** `9b15453` 都含西班牙语 OTP 关键词（`7088649` 引入），Panda 停在旧版本 —— 本次 checkout 只取了 28 个 path，它不在名单里。生产上西语 OTP 邮件匹配不到。

---

## 4. 生产运行实况（2026-07-26 22:40 前后采集）

| 项 | 值 |
|----|----|
| healthy | `true`，无 startup error |
| 账号 | total **19** / schedulable **17** / image_schedulable **17** |
| 排除 | `philliphicks336926`（status=异常，quota=0）+ `enricoalfred9264`（identity_isolated） |
| 额度 | 17×25 + 1×5 + 1×0 = **405** 可用 |
| inflight | 0；`inflight_drift.drift_count` 0 |
| **24h 生图** | **0 张**；`image_tasks` 表 **0 行**；最后一张约 30h 前（2026-07-25 16:12） |
| 24h ERROR 级 | **0** 条，无 Traceback |
| CPU / 内存 | 53% / 223MB（cgroup 上限 1.5 vCPU / 1.5GB） |
| 磁盘 | **79%** 已用；`image_tasks.db` **397MB 存 0 行** |

### 4.1 日志计数的口径陷阱

`docker logs --since 24h | grep -ciE 'ERROR'` 返回 1259，但那是在**大小写不敏感地匹配 WARNING 记录内部的 `"error":` JSON 字段**，不是 ERROR 级别行。实际 `[ERROR]` 级别为 **0**。主控最初也踩了同一个坑，此处留档避免复犯。

24h WARNING 分布（前 5）：

| 事件 | 次数 |
|------|------|
| `text_nurture_tick_error` | 655 |
| `conversation_cf_edge_retry` (403) | 259 |
| `bootstrap_soft_failed` (403) | 186 |
| `text_stream_cf_failover` (403) | 135 |
| `text_nurture_item_retired` | 30 |

CF 403 压力（581 次/24h）全部落在**文本路径**，生图路径无流量故无样本。

### 4.2 AUDIT-28 零流量验证

容器 13:07Z 重启后 `image_gen|images/generations|sediment` 日志命中 **0**。B1/B2/B4/B9 全是「代码路径确证、等流量触发」类问题，修完仍然只有静态确证 + 单测背书。`plan.md` SLO 表中 conc10 ≥8/10 的目标**一次都没复测**。

---

## 5. 新发现的活体缺陷：A1-6 修得不彻底

**现象**：`text_nurture slot not allowed for account binding schedule` 24h **407 次**，死信 5 条，`requeued_total=38`，`consecutive_errors=27`。

**根因**：`services/ip_nurture_schedule.py:270` `resolve_binding_matrix()`

```python
idx = (hash(key) + int(week)) % len(presets)
```

账号的 `binding_key` 是 hash 值（如 `67318972b9af…`），既不在 config 的 binding_schedule 里，也不是预设 id，于是走随机撞档位分支。

**实测**（容器内，19 个账号）：

```
17 个账号当前 slot_allowed = False
 2 个账号 = True
```

A1-6 只给 `business_hours` / `extended_business` 补了周末档，随机撞到 `weekday_only` / `rest_weekend` / `pulse_2h` 等**未补周末**的档位照样全关。

**已生效的部分**：A1-5 的 lease/ack 确实工作 —— 工作项在 requeue + 退避 + 死信，而不是被静默销毁。这正是那条修复的价值。

**修复方向**：hash fallback 只从「含周末档」的预设子集中选，或给 `slot_allowed` 加「连续 N 次全关则降级放行」兜底。

---

## 6. 被推翻的结论（留档）

| 结论 | 来源 | 复核结果 |
|------|------|----------|
| 「Panda 无手改，与仓库一致」 | 主控首轮（只比对 11 个核心文件就外推） | **错**。全量比对得 19 个漂移文件 |
| 「106/108 prod .py 是 CRLF，证明整体从 Windows 拷贝」 | drift-check 子代理 | **错**。实测 `CRLF=48 / LF=186`；AUDIT-28 四个核心文件全部 LF。CRLF 只集中在漂移文件，是**那 19 个**经 scp 上传的证据，不是 AUDIT-28 的 |
| 「1259 条 ERROR」 | 主控首轮 grep | **错**。是 WARNING 内的 `"error":` 字段，ERROR 级实为 0 |

> 教训：**抽样一致不能外推为全量一致**；`grep -ci 'error'` 不等于 ERROR 级别计数。

---

## 7. config 变更（2026-07-26 21:28）

备份 `config.json.bak-audit28-cfscan-20260726-212700`。多数变更是新代码把默认值持久化回盘（整数变浮点是 `json.dump` 指纹），但有两项是**主动关停**：

```diff
  webshare_cf_scan.enabled          true → false
  webshare_cf_scan.auto_quarantine  true → false
+ webshare_cf_scan.block_unscanned_for_schedule  true（新增）
+ account_warmup.cf_block_sec       86400 → 3600.0（AUDIT-28 A2-1）
```

关掉 `auto_quarantine` 是对的 —— 07-26 那次 100 节点误隔离就是它干的（见 `17`）。

**但组合有后效**：`enabled=false` + `block_unscanned_for_schedule=true` 意味着**任何新加入的未扫描 endpoint 永远拿不到 scan verdict**，只能靠 `probe_on_assign` 活体探测兜底。`services/proxy_cf_eligibility.py:138`：

```python
if block_unscanned_for_schedule() and not allow_live_probe:
    return False
```

现有 19 个账号靠 `account_cf_cache_ok()` 短路放行（`:128`）不受影响，**但引入新代理池时会直接踩到**。

---

## 8. 仓库卫生

| 项 | 现状 |
|----|------|
| `git status` | **404** 条（256 untracked） |
| `web_dist.backup-*` | **36 个目录** + 5 个 `.tgz`（`.gitignore` 只挡了带斜杠的目录形式，`.tgz` 漏网） |
| 本地分支 | `codex/img016-async-admission-hard-timeout`，`[ahead 22, behind 5]` of `deploy/main` |
| `deploy/main` 树 | **只有 README.md** —— 那是 artifact 仓库，不是源码仓库 |
| `origin` | 本地**未配置**（Panda 上才有 `basketikun/chatgpt2api`） |
| 未上线的前端重构 | `web/src/app/image/page.tsx` 1963 → 25 行 + `image-workbench.tsx` + 4 components；`ops/page.tsx` −1124；`image-manager` −728。Panda `web_dist` 停在 **07-24 21:43** |

**22 个 commit 从未推到任何真正的源码远端。**

---

## 9. 测试基线

| 范围 | 结果 |
|------|------|
| AUDIT-28 九个回归套件 | **201 passed** |
| 全量 `pytest test/` | 826 项，**58 failed** |
| 58 项失败根因 | 全部是 `BASE_URL = "http://localhost:8000"` 的 HTTP 集成测试，`ConnectionRefusedError`。**环境问题，非代码缺陷** |

---

## 10. 待办（已并入 `04` 与 `plan.md`）

| 优先级 | 事项 |
|--------|------|
| **P0** | Panda 上把 AUDIT-28 的 28 个 staged 文件落成 commit（消除静默回滚风险） |
| **P0** | 本地 22 commit + 16 个漂移文件提交并推送到真正的源码远端 |
| **P0** | conc10 + 基线复测（当前空载窗口最适合） |
| P1 | A1-6 补完：`resolve_binding_matrix` hash fallback 限定含周末档预设 |
| P1 | 补发 `yumail_otp.py`（已提交未上线） |
| P1 | `domain_intel.py` 定性：补提交或删除 |
| P1 | `image_tasks.db` 397MB/0 行离线 VACUUM（磁盘已 79%） |
| P2 | `.gitignore` 补 `web_dist*.tgz` / `web_dist.backup-*` / `.artifact-*/` / `.deploy-*/` / `crates/*/target/` |
| P2 | 前端重构决策：提交上线 or 明确搁置 |

## 11. 需拍板

`scripts/_tmp_deploy_*.py` 有 **13 个**使用 `scp` 直传 + `docker compose up --force-recreate`。§3 那 19 个漂移文件基本就是它们的产物。本次 AUDIT-28 未走它们，但脚本仍在。

建议：**加守卫改造**——保留脚本中的健康检查 / 备份 / smoke 逻辑（有价值），把 `scp` 段替换为从 git ref checkout。
