# 30 — THROUGHPUT-10 Architecture Plan（2026-07-27）

最后更新：**2026-07-27**
状态：**草案**
关联：`28-scheduling-queue-slot-audit-20260726.md`、`26-slot-lifecycle-rust-roadmap.md`

---

## 0. 目标

| 维度 | 现状（2026-07-26） | THROUGHPUT-10 目标 |
|------|---------------------|--------------------|
| 全局并发 | `image_global_concurrency_limit=10` | 保持 10，消除凑不满的原因 |
| pS/sS 比例 | 3:7 碎片化 | 严格 1:1（pS=5, sS=5） |
| 单次吞吐 | 2~6/10（空槽率 40~60%） | 8~10/10（空槽率 <20%） |
| 代理分层 | 单层 datacenter | 住宅 20 + 机房 100 |
| 配额刷新 | 被动/跑完才查 | **四段日历 + 事件驱动**（见 [`32-quota-refresh-window-prime-plan.md`](32-quota-refresh-window-prime-plan.md)） |
| probe 并行 | `max_hot=10` | `max_hot=17` |

---

## 1. Slot Topology Redesign

`28` §A2 确认 `binding_inflight_max` 乘数逻辑错误：当前 `ceil(2 × pool_coeff)` 意味着单 egress 最多 2 inflight，pool 仅 17 时分配不均。

| 参数 | 当前 | 目标 | 说明 |
|------|------|------|------|
| `global_concurrency_limit` | 10 | 10 | 不变 |
| `pS : sS` | 隐式 3:7 | 显式 **5:5** | 各 5 槽独立计数 |
| `binding_inflight_max` | `ceil(2×pool_coeff)` | **固定 1** | 1 binding = 1 inflight |
| `overflow_queue_depth` | 无 | **暴露到 health** | `slot_overflow_pending` |

`binding_inflight_max = 1`：避免单 egress 双 inflight 导致 CF 限流/代理超售；换绑后每 endpoint 独立，无需乘数。

health 新增 `slot_topology` 字段：

```json
{"ps_capacity": 5, "ss_capacity": 5, "ps_inflight": 3, "ss_inflight": 2, "overflow_pending": 0}
```

---

## 2. Residential Proxy Tier — 双池

| 池 | 节点数 | 类型 | 优先级 |
|----|--------|------|--------|
| `preferred` | 20 | 住宅 IP（白名单） | 1 |
| `fallback` | 100 | 升级数据中心 | 2 |

```python
class ProxyPoolService:
    def __init__(self):
        self.preferred = ResiPool(size=20)
        self.fallback = DCPool(size=100)
    def acquire(self, tier_hint="preferred") -> ProxyEndpoint:
        if tier_hint == "preferred" and self.preferred.available():
            return self.preferred.next()
        return self.fallback.next()
```

住宅准入条件：`proxy_resi_whitelist` 命中；`cf_ok=true` 且 6h 无 403；延迟 <3000ms；有空闲 binding。不满足时静默降级。监控指标：

| 指标 | 说明 |
|------|------|
| `proxy_pool_preferred_available` | 住宅可用数 Gauge |
| `proxy_pool_fallback_used_pct` | 回退池用量 |
| `proxy_pool_tier_downgrade_count` | 降级计数 Counter |

---

## 3. Real-Time Quota — 双触发

> **2026-07-28 更新**：60s 全池轮询将废弃，改为 [`32-quota-refresh-window-prime-plan.md`](32-quota-refresh-window-prime-plan.md) 中的 **binding 四段日历**（每天 4 次）+ 事件驱动 + 可选窗口预热。

现状：仅跑完图片后查 newapi，空载时 quota 过时 5~30 分钟。

| 触发 | 方式 | 周期 | 优先级 |
|------|------|------|--------|
| 四段日历 | binding 排期 tick | **每天 4 次/号** | 低 |
| 事件驱动 | `on_image_result` / 懒刷新 / 手动 | **即时** | 高 |

health 新增 `quota.lag_sec`（`now - last_quota_fetch_time` 分布）+ `stale_accounts`（`>120s` 的账号）。watchdog 在 `max_lag > 120s` 时触发 WARN。

```json
{"quota": {"total": 399, "lag_sec": {"max": 45, "p50": 12, "p99": 58}, "stale_accounts": []}}
```

---

## 4. Probe Scaling — max_hot 10->17

| 参数 | 当前 | 目标 | 理由 |
|------|------|------|------|
| `max_hot` | 10 | **17** | 匹配 `image_schedulable=17` |
| worker 模型 | 隐式复用生图池 | **独立 17 worker** | 不占生图 slot |

CF 403 自恢复路径：`mark cf_bad → remove → cooldown 300s → re-probe → re-insert`。独立 `ProbeWorkerPool`：17 个 `asyncio.Task`，各自持独立 `curl_cffi.Session`，不受 `global_concurrency_limit` 限制。

```json
{"probe": {"max_hot": 17, "dedicated_workers": 17, "cf_block_cooldown_s": 300, "self_heal_interval_s": 60}}
```

---

## 5. Bandwidth Monitoring

60s 滚动窗口，15s 报告周期。`on_bytes(account_id, n, direction)` 累加至滚动计数器。

```json
{"egress_mbps": 4.2, "ingress_mbps": 1.1, "top_consumer": "acc_003", "rolling_total_gb": 0.37}
```

| 指标 | 用途 |
|------|------|
| `egress_mbps` | 出口带宽，判断代理/CF 限速 |
| `ingress_mbps` | 入口带宽，判断上游健康 |
| `top_consumer` | 最大消耗账号，辅助限速 |

---

## 6. Deploy Chain

```
WSL: cargo build --release → .so
  → git commit + push → GH Actions（verify sha256, no rebuild）
  → Panda: git pull + docker compose pull && up
```

- `.so` 在 WSL（x86_64-linux-gnu）编译，CI 不重新编译
- 备选：若 Grok2API 不直接加载 `.so`，走 Rust → Python ctypes FFI 子进程
- 每 commit 记录 `.so` sha256 到 RELEASES.md

| 禁止 | 原因 |
|------|------|
| Panda 上 `cargo build` | 无 toolchain |
| `scp .so` | 不可溯源 |
| `docker cp .so` | 重启丢失 |

---

## 7. Risks

| # | 风险 | 缓解 |
|---|------|------|
| R1 | 住宅 20 IP 不够覆盖 conc10 | preferred/fail-fast 设计，满时静默降级 |
| R2 | `binding_inflight_max=1` 降低利用率 | 调度器负载均衡补；空槽率高则调回 2 |
| R3 | 60s 循环 + 事件 = 冲垮 newapi | 同账户 30s 内不重复刷新 |
| R4 | 27 并发 session 超内存 | probe 总带宽限 10mbps，超限退避 |
| R5 | WSL .so 与容器 glibc 不兼容 | CI ldd 验证；备选静态链接 |
| R6 | 住宅白名单过期 | 报警但不阻塞，fallback 兜底 |

---

### 附：Config Key 变更

```json
{
  "image_global_concurrency_limit": 10,
  "slot_topology": {"primary_slots": 5, "secondary_slots": 5, "binding_inflight_max": 1},
  "proxy_pool": {"preferred_pool_size": 20, "fallback_pool_size": 100, "tier_fallback_delay_ms": 2000},
  "quota_refresh": {"loop_interval_s": 60, "event_refresh_min_interval_s": 30, "lag_warn_threshold_s": 120},
  "probe": {"max_hot": 17, "dedicated_workers": 17, "cf_block_cooldown_s": 300, "self_heal_interval_s": 60},
  "bandwidth_monitor": {"window_sec": 60, "report_interval_s": 15, "egress_limit_mbps": 10}
}
```
