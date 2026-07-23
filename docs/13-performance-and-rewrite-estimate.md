# 13 — 当前性能占用与 Go/Rust 重写预估

最后更新：2026-07-20（Asia/Shanghai）  
证据：Panda `chatgpt2api-local`；成功采样 runlog `data/runlogs/rust-baseline-retry-20260720-173225.json`  
（此前失败采样：`rust-baseline-20260720-165030.json` / `rust-baseline-image-20260720-165206.json`，模型名/账号列举错误已修正）

## 1. 空闲占用（实测）

| 指标 | 值 | 说明 |
|------|-----|------|
| 容器 CPU | **~0.5–0.7%** | `docker stats --no-stream` |
| 容器内存 | **~160–230 MiB / 1.5 GiB** | 采样随业务波动 |
| PIDs | ~25–29 | |
| `data/accounts.db` | **~48.6 MiB** | |
| `data/image_tasks.db` | **~9.7 MiB+** | P0 后有回填 |
| `data/logs.jsonl` | **~0.15 MiB 级热文件** | |
| `clearance.enabled` | **false** | 热路径不依赖 FlareSolverr |
| 业务水位 | healthy；**schedulable=6**；inflight=0；quota≈180；账号 8 个 `verified_ready`+sticky 代理 | |

## 2. 低并发业务采样（复用现网账号/代理，pin `X-Preferred-Account-Email`）

脚本：`scripts/_rust_baseline_retry_panda.py`。生图模型：**`gpt-image-2`**。账号库字段在 sqlite `data` JSON 内解析。

### 文本 `/v1/chat/completions`（n=4，目标 3 成功）

| 轮 | 账号 | ok | 耗时 s | class |
|----|------|-----|--------|-------|
| 1 | phil*** | true | 13.36 | ok |
| 2 | ivet*** | true | 5.55 | ok |
| 3 | qafl*** | false | 0.31 | upstream（CF HTML chat_requirements） |
| 4 | qafl*** | true | 6.76 | ok |

- **成功 3/4**；**self=0**
- 成功耗时约 **5.5–13.4 s**

### 生图 `/v1/images/generations`（n=4，目标 3 成功）

| 轮 | 账号 | ok | 耗时 s | b64_len | class |
|----|------|-----|--------|---------|-------|
| 1 | phil*** | true | 54.1 | 1_056_132 | ok |
| 2 | ivet*** | false | 540.0 | 0 | gate/timeout（`image_task_timeout` sync wait） |
| 3 | qafl*** | true | 42.6 | 1_082_224 | ok |
| 4 | qafl*** | true | 50.9 | 1_146_064 | ok |

- **成功 3/4**；**self=0**（无空 data / 无 Bearer 自伤）
- 成功耗时约 **43–54 s**；b64 约 **1.0–1.1M chars**

### 修复点（相对上一轮全失败）

1. 账号列举改为解析 `accounts.data` JSON（否则 candidates=0）
2. 生图模型改为现网支持的 `gpt-image-2`
3. 生图路径补齐 `X-Preferred-Account-Email` → `get_available_access_token(preferred_email=…)`（热修已上 Panda，备份 `backups/hotfix-preferred-email-*`）
4. 唯一 prompt + 等 inflight 清零，避免 duplicate-prompt / 槽占用

## 3. 开销结构 / 预估

（同前：上游 RTT 主导 E2E；Rust 收益在并发与 RSS；helper 保守列见下）

| 维度 | Go | Rust 理想 | Rust 保守（curl_cffi helper） |
|------|-----|-----------|-------------------------------|
| 空闲 RSS | −40%~−60% | −50%~−70% | −20%~−40% |
| 高负载 CPU | −20%~−40% | −30%~−50% | −10%~−25% |
| 生图 E2E P50 | +0%~+8% | +0%~+10% | +0%~+15% |
| 同机并发 | ×1.5–2.5 | ×2–3 | ×1.3–2.0 |

## 4. Rust `:8013` 验收空表

| 指标 | Python 基线（本窗） | Rust 实测 | 门禁 |
|------|---------------------|-----------|------|
| text 成功（剔 upstream） | 3/3 有效尝试中的成功语义；样本 3 ok / 1 upstream | | self=0；≤×1.10 P50 |
| image 成功 | 3 ok；P50≈43–54s | | self=0；≤×1.15/×1.20 |
| self | **0** | | **=0** |

矩阵：`../gptimage-gateway-rs/docs/18-test-matrix.md`。

## 5. 复测

```bash
scp scripts/_rust_baseline_retry_panda.py panda:/tmp/
ssh panda 'PYTHONUNBUFFERED=1 python3 -u /tmp/_rust_baseline_retry_panda.py'
```
