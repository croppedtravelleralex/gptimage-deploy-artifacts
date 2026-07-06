# Panda 同步与公网入口保护策略

最后更新：2026-07-03

## 1. 目标

降低 Panda 同步开销，防止公网 `import-batch` 高频打爆 Panda，同时兼容本地公网 IP 经常变化的现实。

## 2. 水位线

```text
high = 1500
low = 500
emergency = 200
critical = 100
```

## 3. 同步频率

| Panda 可用号 | 同步频率 | 每次账号数 | 行为 |
| ---: | ---: | ---: | --- |
| >=1500 | 30~60min | 20~50 | 慢同步，保护 Panda |
| 500~1499 | 15~30min | 30~100 | 正常补货 |
| 200~499 | 5~15min | 50~150 | 加速补货 |
| 100~199 | 3~5min | 50~200 | 紧急补货 |
| <100 | 人工确认 | 视情况 | 默认仍只传 high confidence |

默认只同步：

```text
confidence = high
status = verified_ready
panda_sync_state = not_uploaded
```

## 4. 本地上传决策

上传前先读 Panda 水位：

```text
remote_available_count
remote_image_inflight
remote_maintenance_state
remote_import_rate_limit_state
```

决策：

```text
if remote_available >= 1500:
    min_interval = 30~60min
    max_batch = 20~50
elif remote_available >= 500:
    min_interval = 15~30min
    max_batch = 30~100
elif remote_available >= 200:
    min_interval = 5~15min
    max_batch = 50~150
else:
    min_interval = 3~5min
    max_batch = 50~200
```

如果 Panda 正在高生图压力：

```text
remote_image_inflight > 0 时优先延后同步，除非 remote_available < emergency
```

## 5. 动态公网 IP 保护

由于本地公网 IP 经常变化，不使用固定 IP allowlist 作为主方案。

保护层：

1. Nginx 限频。
2. Bearer admin key。
3. HMAC 签名。
4. nonce 防重放。
5. idempotency key 防重复写。
6. 应用层按水位限流。

## 6. HMAC 请求头

客户端请求头：

```text
Authorization: Bearer <admin_key>
X-Sync-Timestamp: 2026-07-03T15:00:00Z
X-Sync-Nonce: <random-uuid>
X-Idempotency-Key: <batch-id>
X-Body-SHA256: <hex>
X-Sync-Signature: <hex-hmac>
```

签名内容：

```text
METHOD + "\n" + PATH + "\n" + TIMESTAMP + "\n" + NONCE + "\n" + BODY_SHA256
```

校验规则：

- timestamp 距离服务器时间不超过 5 分钟。
- nonce 未使用过。
- body sha256 匹配。
- HMAC 匹配。
- idempotency key 未成功处理过；已成功处理则直接返回上次结果摘要，不重复写库。

## 7. Nginx 限频建议

仅对同步入口：

```text
/api/accounts/import-batch
```

初始建议：

```text
1 req/min
burst 2~3
```

Panda 高水位时，应用层会进一步放大间隔到 30~60 分钟。

## 8. 应用层水位限流

即使绕过 Nginx，也必须由应用层兜底：

```text
if remote_available >= high and last_import < 30min:
    return 429 Retry-After

if idempotency_key already_success:
    return previous_summary

if same payload hash recently processed:
    skip write
```

## 9. 批量与去重

服务端 `import-batch` 应：

- batch 内按 token_hash 去重。
- 与现有账号无变化时 skipped，不写库。
- 有变化时 transaction upsert。
- 返回 added / updated / skipped / rejected / retry_after。

## 10. 本地脚本迁移

当前事实：

```text
scripts/sync_accounts_delta_to_panda.ps1 读取 data/accounts.json
```

目标：

- 改为从本地 SQLite 读取 `verified_ready`。
- 根据 Panda 水位决定 `MaxAccountsPerRun` 和最小间隔。
- 上传成功后批量更新 SQLite：`panda_sync_state=uploaded`。
- 失败时记录 batch error，不回退到高频重试。

## 11. 验收

- 合法请求在正常频率下 2xx。
- 高频请求 429，并有 Retry-After。
- nonce 重放失败。
- 重复 idempotency key 不重复写。
- Panda 高水位时，本地同步频率降到 30~60min。
- Panda 低水位时，能自动加快但不上传未通过 6h 探测的低置信账号。


## 12. 2026-07-06 本地 staging 补池策略修正

已落地到本地代码：

```text
normal probe schedule     = 30 / 120 / 360 min
low probe schedule        = 10 / 30 / 90 min
emergency probe schedule  = 5 / 15 / 45 min
normal upload interval    = 30 min
low upload interval       = 60 sec
emergency upload interval = 30 sec
single import batch cap   = public_import_max_batch_size，当前 20
```

关键规则：

- Panda `current >= high`：不上传，保留本地 staging/ready。
- Panda `low < current < high`：不上传，降低 Panda 写入和探活压力。
- Panda `current <= low`：加快上传；`current <= emergency`：使用 emergency 档。
- 本地 ready backlog 足够时，上传优先于 staging 探活，避免大批探活阻塞补池。
- 探活档位只控制进入 ready 的节奏；已 ready 的号不应再被 `1/3/6h` 阻塞上传。
