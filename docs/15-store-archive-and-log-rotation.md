# 15 — image_tasks.db 归档与 logs 轮转方案

最后更新：2026-07-19  
状态：**P0 运维已执行（2026-07-19）**；S1/L1 工程仍待做。  
现场证据（Panda `chatgpt2api-local`）：

| 文件 | 执行前 | 执行后（P0） |
|------|--------|----------------|
| `image_tasks.db` | ~1575 MiB，**rows=0** | **~0.02 MiB**（VACUUM） |
| `logs.jsonl` | ~162–163 MiB | 热文件 **0**；归档 `logs.jsonl.20260719-211833.gz` **~20 MiB** |

备份：`/root/gptimage/backups/store-p0-20260719-211735/`（含执行前 db+logs 全量）。  
健康：VACUUM/轮转后 recreate → `healthy=true`，`schedulable=4`，startup_errors=0。

现有能力：

- 任务保留：`ImageTaskService._cleanup_locked` 按 `config.image_retention_days`（默认 30）删除终态行，但 **不 VACUUM** → 文件可不缩。
- 日志：仅支持按 id 删除；`delete()` 会整文件重写；**无按日期轮转 / 无大小上限**。

## 1. image_tasks.db 应该怎么归档

### 1.1 根因分层

1. **历史膨胀**：终态任务曾把 **b64 结果** 写进 `data` JSON（STORE-004）。
2. **空洞不回收**：清理 `DELETE` 后 SQLite 文件体积不降（本次 rows=0 仍 1.5G+ 即证明）。
3. **启动风险**：旧版曾全量读入大 `data` 导致 OOM/502；现状查询已尽量轻量，但仍要防止回归。

### 1.2 推荐策略（三阶段，由易到难）

#### Phase S0 — 紧急缩容（当天可做，运维）

前提：确认 `unfinished=0`（无 queued/running）。

```bash
# 1) 备份
ssh panda 'ts=$(date +%Y%m%d-%H%M%S); cp -a /root/gptimage/data/image_tasks.db /root/gptimage/backups/image_tasks-$ts.db; ls -lh /root/gptimage/backups/image_tasks-$ts.db'

# 2) 容器内 VACUUM（会短暂锁库；低峰执行）
ssh panda 'docker exec chatgpt2api-local python3 -c "import sqlite3; c=sqlite3.connect(\"/app/data/image_tasks.db\"); c.execute(\"VACUUM\"); c.close(); import os; print(os.path.getsize(\"/app/data/image_tasks.db\"))"'

# 3) 健康检查
ssh panda 'curl -fsS http://127.0.0.1:8012/health?format=json | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get(\"healthy\"), (d.get(\"accounts\") or {}).get(\"schedulable\"))"'
```

验收：文件体积降到 **数 MB 级**（空表）或与行数匹配；health 仍 healthy。

> 若 VACUUM 期间必须服务：可 `VACUUM INTO '/app/data/image_tasks.compact.db'` 后原子替换（先停写入或短维护窗）。

#### Phase S1 — 工程化清理（STORE-004，建议下一迭代）

| 改动 | 说明 |
|------|------|
| 成功后不落 b64 | `result.data[].b64_json` 改为只存 **URL / 本地 path / 外置对象键**；已有 `images/` 或 WebDAV 则引用之 |
| 保留期可配 | `image_task_retention_days`（可短于 `image_retention_days`，建议默认 **7**） |
| 清理后自动回收 | `_cleanup_locked` 若删除行数>0 或文件>阈值，触发 **增量 `PRAGMA incremental_vacuum`** 或定期 `VACUUM`（凌晨） |
| 归档而非丢弃（可选） | 删除前把轻量元数据（task_id/status/email_hash/latency/error_code，**无 b64**）append 到 `data/archive/image_tasks/YYYY-MM.jsonl.gz` |
| 告警 | health 增加 `image_tasks_db_bytes`、`image_tasks_rows`；>500MiB 或 rows=0 且 size>50MiB 告警「需 VACUUM」 |

验收：

- 新产生成功任务 DB 增量以 KB/任务计，而非 MB/任务。
- 重启不 OOM；`/api/image-tasks/status` 不读大字段。
- 日终 `du` 稳定或下降。

#### Phase S2 — 冷热分离（可选）

- 热库：仅保留 N 天终态 + 全部未完成。
- 冷库：对象存储或按月 sqlite 分片 `image_tasks-2026-06.db`，管理页「历史任务」走冷查。
- 备份：`backup_service` 对热库全量；冷库只备份清单。

### 1.3 禁止项

- 不在有 unfinished 任务时强行删库文件。
- 不把含 b64 的 DB 推进 git / artifacts。
- 不在 Panda 上为「清盘」而 `rm image_tasks.db` 后不重建 schema（服务会重建，但会丢审计；应走备份→VACUUM/归档流程）。

## 2. logs.jsonl 应该怎么轮转

### 2.1 现状问题

- 单文件追加，已 **~162MiB**，会继续涨。
- UI/API `list` 从文件尾部反向扫描；文件越大，偶发查询越慢。
- `delete(ids)` 整文件读写，大文件时 CPU/IO 尖刺。

### 2.2 推荐策略

#### Phase L0 — 立即运维（手工）

```bash
# 低峰：按日期切开并压缩（示例）
ssh panda 'ts=$(date +%Y%m%d); cd /root/gptimage/data; \
  cp logs.jsonl backups/logs-$ts.jsonl; \
  mv logs.jsonl logs.jsonl.$ts; \
  gzip -9 logs.jsonl.$ts; \
  : > logs.jsonl; \
  docker restart chatgpt2api-local'   # 或 compose recreate；确认 LogService 重新打开文件
```

更稳妥：应用内提供 `POST /api/logs/rotate`（见 L1），避免重启。

保留策略建议：

| 层级 | 保留 |
|------|------|
| 热文件 `logs.jsonl` | 最近 **7 天** 或 **≤64MiB** |
| 压缩归档 `logs/YYYY-MM-DD.jsonl.gz` | **30–90 天** |
| 更旧 | 删或丢对象存储 |

#### Phase L1 — 工程化轮转（LOG-ROT）

在 `LogService` 增加：

1. **写路径**：`add()` 前检查大小/日期；超限则 `rename` 为 `logs-YYYYMMDD-HHMMSS.jsonl` 并可选 gzip 线程。
2. **配置**（`config.json`）：
   - `log_rotate_max_mb` 默认 `64`
   - `log_rotate_keep_days` 默认 `30`
   - `log_hot_keep_days` 默认 `7`（热文件内再按时间裁剪可选）
3. **API**：
   - `POST /api/logs/rotate`（admin）强制切割
   - `GET /api/logs/archives` 列出压缩包
   - `GET /api/logs` 仍只扫热文件；需要历史时显式 `archive=` 
4. **类型分流（可选）**：`llm_ops` / `call` / `account` 分文件，避免互相拖垮。

验收：

- 热文件稳定 < `log_rotate_max_mb`。
- 切割不丢当日最后一条（rename 原子 + 新 fd）。
- 管理页日志查询延迟不随总历史线性恶化。

#### Phase L2 — 结构化存储（远期）

- 高频 `call` 进 sqlite/clickhouse；`llm_ops` 保持 jsonl 亦可。
- 与 L2 RCA `list_llm_ops` 工具对齐「只读热+可选冷」。

### 2.3 与备份的关系

- `backup_service`：热 `logs.jsonl` 每次备；归档 `.gz` **抽样或按周备**，避免备份包被日志撑爆。
- 备份保留与 `log_rotate_keep_days` 对齐，防止「盘上删了备份里还有 10 份全量」。

## 3. 实施顺序（建议排期）

| 优先级 | 项 | 预估 | 产出 |
|--------|-----|------|------|
| P0 | S0 VACUUM + 日志手工切割 | 0.5 天 | 盘立刻下降 |
| P0 | S1 停写 b64 + 清理后 incremental_vacuum | 2–4 天 | STORE-004 |
| P1 | L1 LogService 轮转 | 1–2 天 | LOG-ROT |
| P2 | S2 冷热分片 / L2 分库 | 按需 | |
| 其后 | RUST-001 Phase 0 | 见 `14` | |

## 4. 验收总表

- [ ] `image_tasks.db`：rows 与文件大小匹配；无「0 行却 >50MiB」。
- [ ] 新任务成功：DB 增量可解释（无整图 b64）。
- [ ] `logs.jsonl` 热文件 ≤ 配置上限；存在 `.gz` 归档。
- [ ] health 暴露 DB/日志体积字段。
- [ ] 备份体积不再被任务库/日志支配。

## 5. 关联

- `04-improvement-backlog.md` — STORE-004 / LOG-ROT / RUST-001  
- `13-performance-and-rewrite-estimate.md` — 为何优先归档  
- `14-rust-rewrite-plan.md` — 归档为 Rust 前置
