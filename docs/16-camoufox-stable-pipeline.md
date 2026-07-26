# Camoufox 稳定产出流水线（固定链路）

最后更新：2026-07-22（Asia/Shanghai）

## 结论（当前权威）

**新号唯一稳定产出方式**不是注册机 UI / 协议批量，而是下面这条固定链路；已注册 Outlook 的人工恢复也复用同一正式入口，但切到 `relogin` 模式：

```text
取邮箱账号
  → 取「号池未占用」的新 Webshare IP
  → 检查（代理出口探活 + 邮箱 OTP 可达）
  → 本机 Camoufox 注册 OpenAI
  → 本地落盘 + 导出 panda_import.secret.json
  → 上传 Panda 号池（默认 identity_isolated 观察）
  → 成熟后再人工升 verified_ready / 开调度
```

正式入口：

| 路径 | 脚本 |
| --- | --- |
| **Outlook（默认生产）** | `scripts/outlook_camoufox_stable_register.py` |
| Proton（OTP 信箱） | `scripts/proton_camoufox_panda_ip_register.py` + `scripts/_tmp_proton_camoufox_openai_observe.py` |
| 页面操作底座 | `scripts/yumail_camoufox_openai_register.py` |

文档索引：本文件；交接见 `06-handoff.md`。

正式脚本内已经固化两种互不混用的认证路径：

- `--mode register`（默认）：新号走 Platform authorize / OTP / about-you / PKCE，必须取得 refresh token。
- `--mode relogin`：已注册号先从 ChatGPT NextAuth 入口取得匹配的 state/PKCE Cookie，再由 Camoufox 完成 OTP；callback 回到 `chatgpt.com/` 后读取 `/api/auth/session`。该模式允许只有 access/session token，不伪造 refresh token。
- 正式脚本不再依赖 `_tmp_outlook_camoufox_webshare_register.py` 或 `_tmp_proton_camoufox_openai_observe.py`。

---

## 固定链路（逐步）

### 1. 配置 / 取号

**Outlook（推荐）**

- 凭据文件格式：`email----password----client_id----refresh_token`（一行一号）
- 示例来源：`C:\Users\Lenovo\Downloads\0716-4000_015.txt`
- 用 `--account-index` 取第 N 行（默认 0）

**Proton**

- `D:\SelfMadeTool\AutoRegister\proton\registered_accounts.txt`
- 须 `login_ok` 且能收 OTP；已绑定 OpenAI 的勿再注册

**本机依赖**

- Clash / 邮件出口：`http://127.0.0.1:7897`（Proton OTP；Outlook Graph/IMAP 一般直连）
- Camoufox + Python 3.12 环境

### 2. 取新 Webshare IP

- 池文件（Panda）：`/root/gptimage/data/runlogs/webshare_good_csrf_200.secret.txt`
- 行格式支持：`http://user:pass@host:port` 或 `host:port:user:pass`
- **必须**排除号池已占用 egress/host（`--exclude-hosts` / `--used-hosts-file`）
- **注册前 live 探活**（`gpt_unavailable_proxies.json` 批量 scan 后可能全表隔离，仍须 live 找 OK 节点）：

```bash
docker exec -w /app -e PYTHONPATH=/app chatgpt2api-local \
  /app/.venv/bin/python /app/scripts/_tmp_probe_webshare_cf_ok.py \
  --pool /app/data/runlogs/webshare_100_proxies.secret.txt \
  --exclude-hosts "<号池已用 host>" --count 2 --workers 8
```

- 生产原则：**一号一 sticky**；同 egress 承载有上限（见配置 `proxy_binding_max_accounts`，Panda 生产 **2**，2026-07-25 起）；观察期仍优先独立节点

### 3. 检查（不通过不注册）

| 检查 | 标准 |
| --- | --- |
| 代理探活 | `probe_proxy` → HTTP 200 + 出口 IP 与目标 host 一致 |
| 邮箱 | Outlook：`preflight_mailbox_access` 成功；Proton：登录成功可解密 OTP |
| 冲突 | 所选 host 不在当前号池 used_hosts |

`--proxy` 始终表示账号长期绑定的 sticky Webshare。若本机直连 Webshare 在 ChatGPT session/callback 阶段 reset，使用 `--browser-proxy` 或 `--browser-proxy-file` 指向已建立的“本机 → Panda → Webshare”本地转发入口；Camoufox/NextAuth/代理探活走该入口，账号和导出 blob 仍只保存原 sticky Webshare。结果和错误只记录脱敏 endpoint，不写代理用户名/密码。

仅检查：

```bash
python scripts/outlook_camoufox_stable_register.py \
  --accounts-file "C:/Users/Lenovo/Downloads/0716-4000_015.txt" \
  --proxy "http://user:pass@NEW_HOST:PORT" \
  --browser-proxy-file "C:/secure/panda-webshare-chain.secret.txt" \
  --check-only
```

### 4. 注册

```bash
python scripts/outlook_camoufox_stable_register.py \
  --accounts-file "C:/Users/Lenovo/Downloads/0716-4000_015.txt" \
  --account-index 0 \
  --webshare-pool "path/to/webshare_good_csrf_200.secret.txt" \
  --exclude-hosts "82.29.223.111,92.113.236.188,92.113.246.215" \
  --out-dir data/runlogs/outlook-camoufox-stable
```

或已知新代理时：

```bash
python scripts/outlook_camoufox_stable_register.py \
  --accounts-file "..." \
  --proxy "http://user:pass@NEW_HOST:PORT" \
  --out-dir data/runlogs/outlook-camoufox-stable
```

Camoufox 流程：authorize →（多语言）切 OTP → 填码 → about-you → PKCE 换票。  
`source_detail` 固定前缀：`outlook_camoufox_stable_pipeline`。

### 4.1 已注册 Outlook 重登

```bash
python scripts/outlook_camoufox_stable_register.py \
  --mode relogin \
  --accounts-file "C:/Users/Lenovo/Downloads/0716-4000_015.txt" \
  --account-index 0 \
  --proxy "http://user:pass@STICKY_HOST:PORT" \
  --browser-proxy-file "C:/secure/panda-webshare-chain.secret.txt" \
  --out-dir data/runlogs/outlook-camoufox-stable
```

固定顺序：

1. 用 browser proxy 探活，用 Outlook RT/IMAP 做邮箱预检。
2. 同一 browser proxy 调用 ChatGPT `/api/auth/csrf` 与 `/api/auth/signin/openai`，取得官方 authorize URL 和 `__Secure-next-auth.state` Cookie。
3. Cookie 注入 Camoufox 后才打开 authorize URL并完成 OTP；禁止手工随机 state 直开 `auth.openai.com`。
4. callback 回到 `https://chatgpt.com/` 时读取 `/api/auth/session`；不能继续只等 URL code/refresh token。
5. 新 token 只在本地写成 `identity_isolated` 并导出 blob。Panda 侧必须先备份、隔离导入、通过同一 sticky Webshare 验证 `/backend-api/me`，成功后才能删除旧 token 并 `reload_from_storage()`。
6. **提交后必须刷新运行时内存**：`POST /api/accounts/reload-from-storage`（脚本 `scripts/_tmp_reload_panda_accounts.py`）。`docker exec` 写库不会自动更新 8012 进程缓存。

### 4.2 恢复提交（替换旧 token → verified_ready）

```bash
# blob 放到 Panda 挂载目录后：
docker exec -w /app -e PYTHONPATH=/app chatgpt2api-local \
  /app/.venv/bin/python /app/scripts/_tmp_panda_commit_import_blob.py \
  --root /app \
  --blob /app/data/runlogs/recovery-xxx-import.secret.json \
  --backup-dir /app/data/backups/outlook-relogin-xxx-before-20260723

# 宿主机执行（容器内 8012 不可达）：
python3 /root/gptimage/scripts/_tmp_reload_panda_accounts.py
```

### 5. 入库观察（新号 register，非 relogin）

1. 产物：`…/panda_import.secret.json`（含 sticky `proxy`、`identity_isolated`）
2. 受控上传到 Panda `/root/gptimage/data/runlogs/`（挂载为 `/app/data/runlogs/`，**账号 secret，不是代码部署**）
3. 容器内观察态导入（**勿**用 `_tmp_panda_commit_import_blob.py`，那是恢复替换逻辑）：

```bash
docker exec -w /app -e PYTHONPATH=/app chatgpt2api-local \
  /app/.venv/bin/python /app/scripts/_tmp_panda_import_observe_blob.py \
  --root /app \
  --blob /app/data/runlogs/outlook-xxx-observe-import.secret.json \
  --backup-dir /app/data/backups/outlook-observe-xxx-20260723
```

4. 宿主机 `POST /api/accounts/reload-from-storage`（`scripts/_tmp_reload_panda_accounts.py`）
5. **默认保持 `identity_isolated`** 做观察；观察态导入仅 `/backend-api/me` 验 token（避免 Panda 上 `conversation/init` CF403 误杀）。确认存活后再：

```text
account_service.set_account_scheduling(access_token, enabled=True)
```

### 6. 调度出口纪律

- 调度走账号级 sticky Webshare，**不要**用 Panda 宿主机公网 IP 做 `/backend-api`
- 证据（2026-07-20）：Panda `43.156.233.219` 能完成 Camoufox 注册页，直连刷 `/me` → CF 403

---

## 2026-07-21 实跑样例（固定链路验收）

| 项 | 值 |
| --- | --- |
| 邮箱文件 | `0716-4000_015.txt` 第 1 行 |
| 账号 | `CharlieTim7490@outlook.com` |
| 新 Webshare | `92.113.246.215:5800`（当时号池未占用） |
| 检查 | proxy OK / mailbox OK |
| 注册 | Camoufox 成功 |
| 号池 | `identity_isolated`，quota=5，观察中 |
| 证据目录 | `data/runlogs/outlook-ws-20260721/` |

---

## 2026-07-23 实跑样例（新号观察 + 死号恢复）

| 项 | 新号观察（×2） | 死号恢复（×2） |
| --- | --- | --- |
| 脚本模式 | `--mode register` | `--mode relogin` |
| 邮箱 | `felicitypamela2673` / `andersmia76491`（0716 index 3/4） | `charlietim7490` / `barnettregina91891` |
| Webshare | `45.39.75.27` / `92.113.231.203`（号池未占用） | 各号原 sticky |
| Panda 终态 | `identity_isolated` | `verified_ready` |
| 导入脚本 | `_tmp_panda_import_observe_blob.py` + reload API | `_tmp_panda_commit_import_blob.py` + reload API |
| 证据 | `data/runlogs/outlook-camoufox-stable-20260723/` | `data/runlogs/outlook-recovery-20260723/` |
| 晚间批次 | `blakekyle` / `haroldsunny`（0716 index 14/15）；`batch3/` | |
| 2026-07-24 批次 | `gibsonarthur` / `ivorbrown`（index 16/17）；`batch1/`；`haroldsunny` 删号 | |
| 2026-07-24 晚间批次 | `issacandrew` / `frasierandy`（index 20/22）；`batch2/`；index 18/19 弃用 | |

---

## 相关脚本与证据

| 路径 | 用途 |
| --- | --- |
| `scripts/outlook_camoufox_stable_register.py` | **Outlook 固定链路入口** |
| `scripts/_tmp_probe_webshare_cf_ok.py` | live `probe_proxy_cf` 选注册用 Webshare |
| `scripts/_tmp_panda_import_observe_blob.py` | 新号观察态 Panda 导入（`/me` 验 token） |
| `scripts/_tmp_panda_commit_import_blob.py` | 死号恢复替换导入（升 `verified_ready`） |
| `scripts/_tmp_reload_panda_accounts.py` | 提交后刷新 8012 内存 |
| `scripts/yumail_camoufox_openai_register.py` | OTP/密码/授权底座（含多语言 OTP 文案） |
| `scripts/proton_camoufox_panda_ip_register.py` | Proton + 指定 browser-proxy |
| `data/runlogs/outlook-ws-20260721/` | Outlook 新鲜 Webshare canary |
| `data/runlogs/proton-panda-ip-20260720/` | Proton / 共享 IP 观察 |

---

## 禁止

- 用注册机 UI / 协议批量当正式产出
- 跳过代理探活或邮箱预检直接开浏览器
- 手工随机 OAuth state 直开 `auth.openai.com`，或在 callback 回到首页后仍只等 code/refresh token
- 把本地 Panda 转发地址写进账号 `proxy`；它只允许作为 `browser proxy`，账号必须保存真实 sticky Webshare
- 把 Panda 直连 IP 当长期调度出口
- 未观察成熟就批量 `verified_ready`
- 用 scp/远程 build 当正式**代码**发布（账号 blob 受控上传除外）
