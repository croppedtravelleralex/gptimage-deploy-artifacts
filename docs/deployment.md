# 部署与升级指南

本文介绍 ChatGPT2API 的常见部署方式，以及后续升级项目时需要保留的数据和执行步骤。

## 部署前准备

服务器需要安装：

- Docker
- Docker Compose v2
- Git

首次部署前建议确认：

```bash
docker version
docker compose version
git --version
```

项目核心持久化文件：

| 路径 | 作用 |
| --- | --- |
| `config.json` | 主配置、后台密钥、代理、图片、备份等配置 |
| `.env` | Docker compose 环境变量 |
| `data/` | 账号、注册配置、日志、图片、任务记录等运行数据 |

升级和迁移时重点保留以上内容。

## 方式一：普通 Docker 部署

适合不需要 WARP / FlareSolverr 清障的场景。

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
```

设置 `config.json` 中的 `auth-key`，或在 `docker-compose.yml` 中配置：

```yaml
environment:
  - CHATGPT2API_AUTH_KEY=your_secret_key
```

启动：

```bash
docker compose up -d
```

访问：

```text
http://localhost:3000
```

API 基础地址：

```text
http://localhost:3000/v1
```

查看日志：

```bash
docker logs -f chatgpt2api
```

停止：

```bash
docker compose down
```

## 方式二：WARP / FlareSolverr 部署

适合注册流程经常遇到 Cloudflare 拦截的场景。该方式会启动：

- `warp-proxy`
- `privoxy`
- `flaresolverr`
- `init-config`
- `app`

复制环境变量模板：

```bash
cp .env.example .env
```

至少修改 `.env` 中的：

```text
CHATGPT2API_AUTH_KEY=your_secret_key_here
```

启动：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

访问：

```text
http://localhost:3000
```

FlareSolverr 相关配置可以在后台设置页的 `FlareSolverr` tab 中查看和测试。更详细的 Cloudflare 清障说明见：

```text
docs/flaresolverr-cloudflare.md
```

查看容器状态：

```bash
docker compose -f docker-compose.warp.yml ps
```

查看日志：

```bash
docker logs -f chatgpt2api-warp
docker logs -f chatgpt2api-flaresolverr
```

停止：

```bash
docker compose -f docker-compose.warp.yml down
```

## 方式三：源码运行

适合本地开发或临时调试。

后端：

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

前端开发服务：

```bash
cd web
bun install
bun run dev
```

源码方式运行时，后端默认读取项目根目录的 `config.json` 和 `data/`。

## 存储后端

默认使用本地 JSON 文件：

```text
STORAGE_BACKEND=json
```

可选值：

| 值 | 说明 |
| --- | --- |
| `json` | 本地 JSON 文件，默认方式 |
| `sqlite` | 本地 SQLite，通常存放在 `data/accounts.db` |
| `postgres` | 外部 PostgreSQL |
| `git` | Git 私有仓库存储账号数据 |

PostgreSQL 示例：

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

SQLite 示例：

```yaml
environment:
  - STORAGE_BACKEND=sqlite
  - DATABASE_URL=sqlite:////app/data/accounts.db
```

## 升级前备份

升级前建议备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

如果没有 `.env`，可以去掉：

```bash
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json data
```

也可以在后台设置页配置 Cloudflare R2 备份，用于定时备份关键数据。

## 升级：普通 Docker 部署

进入项目目录：

```bash
cd chatgpt2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码和镜像：

```bash
git pull
docker compose pull
docker compose up -d
```

查看状态：

```bash
docker compose ps
docker logs -f chatgpt2api
```

## 升级：WARP / FlareSolverr 部署

进入项目目录：

```bash
cd chatgpt2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码并重新构建：

```bash
git pull
docker compose -f docker-compose.warp.yml up -d --build
```

查看状态：

```bash
docker compose -f docker-compose.warp.yml ps
docker logs -f chatgpt2api-warp
```

## 升级：源码运行

```bash
cd chatgpt2api
git pull
uv sync
```

如果需要重新构建前端静态产物：

```bash
cd web
bun install
bun run build
```

然后按你的进程管理方式重启后端服务。

## 回滚

如果升级后需要回滚代码：

```bash
git log --oneline -n 20
git checkout <旧版本commit>
```

普通 Docker 部署：

```bash
docker compose up -d
```

WARP / FlareSolverr 部署：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

如果需要恢复数据：

```bash
tar -xzf backups/你的备份文件.tgz
```

恢复数据前建议先停止容器，避免运行中写入覆盖：

```bash
docker compose down
```

或：

```bash
docker compose -f docker-compose.warp.yml down
```

## Panda 生产热更新（gptimage.relai.asia）

本节描述当前 **Panda VPS** 上的实际部署方式，与上文「拉 ghcr 镜像」的通用流程不同。

### 环境信息

| 项 | 值 |
| --- | --- |
| SSH | `ssh panda`（`root@100.69.228.93`） |
| 项目目录 | `/root/gptimage` |
| Compose | `docker-compose.panda.yml` |
| 容器 | `chatgpt2api-local` |
| 镜像 | `chatgpt2api:local`（本地 Dockerfile build） |
| 端口 | 宿主机 `8012` → 容器 `80` |
| 公网 | `https://gptimage.relai.asia` |

### 部署模式

生产使用 **bind-mount 热更新**，不把业务代码打进镜像再发布。以下路径在 compose 中挂载进容器：

- `./data` → `/app/data`
- `./config.json` → `/app/config.json`
- `./web_dist` → `/app/web_dist`（只读）
- `./api` → `/app/api`（只读）
- `./services` → `/app/services`（只读）
- `./utils` → `/app/utils`（只读）

改上述文件后，执行 `docker compose -f docker-compose.panda.yml up -d` 使 mount 生效；Python 模块变更通常需要容器重建或 restart。

未列入 mount 的代码仍在镜像内。若要热更新其他目录，需先加入 `docker-compose.panda.yml` 的 `volumes`，或 `docker compose build` 重建镜像。

### 热更新标准流程

**1. 部署前检查（必做）**

```bash
ssh panda
cd /root/gptimage
docker ps --filter name=chatgpt2api-local
curl -sS 'http://127.0.0.1:8012/health?format=json' | python3 -m json.tool
```

记录 `version`、`accounts.total`、`unlimited_quota_count`、`unknown_quota_count` 等基线。

**2. 备份**

```bash
BACKUP=/root/gptimage/backups/hotfix-$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP"
cp services/account_service.py services/account_refresh_all_service.py api/system.py docker-compose.panda.yml "$BACKUP/"
tar -czf "$BACKUP/web_dist.tgz" -C /root/gptimage web_dist
echo "BACKUP=$BACKUP"
```

**3. 上传变更**

在开发机执行（示例）：

```bash
scp services/account_service.py services/account_refresh_all_service.py panda:/root/gptimage/services/
scp api/system.py panda:/root/gptimage/api/
# 前端：本地 npm run build 后
scp -r web/out panda:/root/gptimage/web_dist_new
```

在服务器上原子替换前端：

```bash
cd /root/gptimage
mv web_dist web_dist.bak.$(date +%Y%m%d-%H%M%S)
mv web_dist_new web_dist
```

**4. 重建容器**

```bash
cd /root/gptimage
docker compose -f docker-compose.panda.yml up -d
```

等待 3–10 秒后验收：

```bash
curl -sS 'http://127.0.0.1:8012/health?format=json'
curl -sS 'https://gptimage.relai.asia/health?format=json'
docker logs chatgpt2api-local --tail 30
```

**5. 验收要点**

- `status=ok`，`healthy=true`
- 额度相关改动：对照 `docs/quota-semantics.md` 检查 `unlimited_quota_count` / `unknown_quota_count`
- 账号页 spot-check：Pro → `∞`，unknown 非 Pro → `未知`，有 remaining → 数字
- **不要**在未备份情况下删除 `data/` 或 `config.json`

### 回滚

使用对应备份目录（示例为额度修复备份）：

```bash
cd /root/gptimage
BACKUP=/root/gptimage/backups/quota-fix-20260629-235620
cp "$BACKUP/account_service.py" services/
cp "$BACKUP/account_refresh_all_service.py" services/
cp "$BACKUP/system.py" api/
cp "$BACKUP/docker-compose.panda.yml" .
tar -xzf "$BACKUP/web_dist.tgz" -C /root/gptimage
docker compose -f docker-compose.panda.yml up -d
```

### 已知备份

| 日期 | 路径 | 说明 |
| --- | --- | --- |
| 2026-06-29 | `/root/gptimage/backups/quota-fix-20260629-235620/` | 额度三态热更新前全量备份 |
| 2026-07-04 | `/root/gptimage/backups/p6-image-queue-20260704-175026/` | 生图 P6 异步队列 / timeout_pending / deadlock_guard 生产部署前备份；含 `ROLLBACK.sh` 与 `post-deploy-validation.json` |

### 2026-07-04 生图 P6 部署回滚

```bash
ssh panda
cd /root/gptimage
/root/gptimage/backups/p6-image-queue-20260704-175026/ROLLBACK.sh
```

该备份覆盖本次上传的 `api/`、`services/` 代码文件和 `docker-compose.panda.yml`；若回滚时发现本次新增的 `services/image_deadlock_guard_service.py` 没有原始备份，回滚脚本会删除该新增文件并重启 app。

### 与通用 Docker 部署的区别

| | 通用 `docker-compose.yml` | Panda `docker-compose.panda.yml` |
| --- | --- | --- |
| 镜像来源 | `ghcr.io/basketikun/chatgpt2api:latest` | 本地 `chatgpt2api:local` |
| 代码更新 | `git pull` + `docker compose pull` | scp + bind-mount |
| 端口 | 3000 | 8012 |
| 数据 | `./data` | `./data`（同一持久化方式） |

## 常用维护命令

查看容器：

```bash
docker compose ps
```

查看主服务日志：

```bash
docker logs -f chatgpt2api
```

查看 WARP 部署主服务日志：

```bash
docker logs -f chatgpt2api-warp
```

重启普通部署：

```bash
docker compose restart
```

重启 WARP 部署：

```bash
docker compose -f docker-compose.warp.yml restart
```

清理未使用镜像：

```bash
docker image prune
```

不要直接删除 `data/`、`config.json`、`.env`，除非已经确认有可用备份。
