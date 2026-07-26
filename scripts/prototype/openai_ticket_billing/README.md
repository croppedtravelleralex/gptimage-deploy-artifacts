# OpenAI 开票 + 生图 — Python 原型（冻结）

**状态**：`v20260723` 快照，**后续不改**。Rust/其他语言复现以此为准；生产代码演进不得回写本目录。

## 开票定义（OpenAI）

1. `POST /backend-api/sentinel/chat-requirements/prepare` → `prepare_token` + `proofofwork` + `turnstile.dx`
2. 本地 `pow.py` + `turnstile.py`（VM）求解
3. `POST .../finalize` → `token`（Sentinel 票）
4. `POST /f/conversation` SSE 带 `OpenAI-Sentinel-*` 头 → `image_gen` → poll → estuary

**不是**浏览器 DOM 取票；**不是** grok `chrometicket` 字段（后置参考）。

## 冻结文件（`v20260723/`）

| 文件 | 角色 |
|------|------|
| `turnstile.py` | Turnstile VM 求解（开票核心） |
| `pow.py` | PoW token |
| `_tmp_spa_image_bench3.py` | 原子验收：requirements→SSE→poll→download |
| `spa_bench_sse.py` | SSE 消费 / gate / 诊断 |
| `_tmp_verify_ticket_image_panda.py` | 生产 API `/v1/images` 单轮验证 |
| `_tmp_export_spa_secret.py` | 从 Panda DB 导出账号 secret |

生产对应路径：`utils/turnstile.py`、`utils/pow.py`、`services/openai_backend_api.py`（可演进）；本目录仅作对照原型。

## 2026-07-23 验证基线（Panda）

| 路径 | 结果 | 耗时 |
|------|------|------|
| `/v1/images/generations` | 200, b64≈1.1MB | 35s |
| bench3 `spa_tool` | ok, PNG 2.3MB | 47s |

证据：`data/runlogs/spa_repro/ticket-verify-20260723/`（Panda 宿主机）。

## 使用

```bash
# 只读对照，勿 pip install 或 import 本目录为包
diff utils/turnstile.py scripts/prototype/openai_ticket_billing/v20260723/turnstile.py
```

文档：`docs/22-ticket-image-pipeline-and-go-spike.md`。
