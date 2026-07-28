# 额度语义规范

最后更新：**2026-07-28**

本文是账号池「额度三态」与 **`restore_at` / 核对时间** 的维护真相源。改统计、展示、调度或健康页时，先对照本文，再改代码。

> **排期与预热总方案**：[`32-quota-refresh-window-prime-plan.md`](32-quota-refresh-window-prime-plan.md)

## 背景与根因

历史上，系统把 `image_quota_unknown=true` 一律当成「无限额」，在健康页和账号列表里显示为 `∞`。这会把 **上游未返回 `image_gen` 限额** 的 Plus / Free 等账号误报成无限额。

实际上，OpenAI 后端在 `limits_progress` 里找不到 `feature_name=image_gen` 时，会设置 `image_quota_unknown=true`，但这只表示 **额度未知**，不等于 **真无限额**。

## 三态定义

| 状态 | 判定条件 | 展示 | 是否消耗额度 | 是否计入 `total_quota` |
| --- | --- | --- | --- | --- |
| **真无限额** | 账号类型为 `Pro` 或 `ProLite`（含 `pro_lite` 等别名） | `∞` | 否 | 否 |
| **未知额度** | `image_quota_unknown=true` 且 **不是** Pro/ProLite，且状态为 `正常` | `未知` | 否 | 否 |
| **数值额度** | 有明确 `quota`（通常来自 `limits_progress.image_gen.remaining`） | 数字 | 是（成功生成后 -1） | 是 |

优先级：**真无限额 > 未知额度 > 数值额度**。

### 类型别名

`AccountService._normalize_account_type()` 会把下列写法归一化：

- `pro` → `Pro`
- `prolite` / `pro_lite` → `ProLite`
- `plus` → `Plus`
- `free` → `free`

真无限额只认归一化后的 `Pro` 和 `ProLite`。

## 代码落点

### 后端核心

| 文件 | 职责 |
| --- | --- |
| `services/account_service.py` | `_is_true_unlimited_image_account()`、`_is_unknown_image_quota_account()`、`get_stats()` 统计拆分、`_normalize_account()` 从 `limits_progress` 覆盖陈旧 unknown |
| `services/openai_backend_api.py` | `_extract_quota_and_restore_at()`：无 `image_gen` 时设 `image_quota_unknown=true` |
| `services/account_refresh_all_service.py` | 慢刷进度：`unlimited_quota` / `unknown_quota` / `quota_total` 分开计数 |
| `api/system.py` | `/health` JSON 与 HTML：展示「真无限额」「未知额度」 |

### 前端

| 文件 | 职责 |
| --- | --- |
| `web/src/app/accounts/page.tsx` | `isUnlimitedImageQuotaAccount()`、`isUnknownImageQuotaAccount()`、`formatQuota()`、统计卡片三态 |
| `web/src/lib/api.ts` | `AccountRefreshAllStatus` 含 `unlimited_quota` / `unknown_quota` |

### 关键函数（后端）

```python
# 真无限额：仅 Pro / ProLite
AccountService._is_true_unlimited_image_account(account)

# 未知额度：正常 + image_quota_unknown + 非 Pro/ProLite
AccountService._is_unknown_image_quota_account(account)

# 可调度：限流/异常/禁用除外；真无限额、未知额度、quota>0 均可
AccountService._is_image_account_available(account)
```

### `limits_progress` 覆盖规则

在 `_normalize_account()` 中：若 `limits_progress` 能解析出 `image_gen.remaining`，且账号 **不是** 真无限额，则：

- 写入 `quota` 和 `restore_at`
- 将 `image_quota_unknown` 置为 `False`

真无限额账号 **不** 被 `limits_progress` 覆盖，避免把 Pro 误当成有上限的数值账号。

## 统计字段

`account_service.get_stats()` 返回：

| 字段 | 含义 |
| --- | --- |
| `unlimited_quota_count` | 状态为 `正常` 的真无限额账号数 |
| `unknown_quota_count` | 状态为 `正常` 的未知额度账号数 |
| `total_quota` | 所有 `正常` 账号的 `quota` 之和（不含 unknown/unlimited 语义） |
| `schedulable` | 满足可调度条件的账号数 |

`account_health()` 与 `/health?format=json` 的 `healthy` 判定：

```text
active > 0  OR  unlimited_quota_count > 0  OR  unknown_quota_count > 0
```

## 展示规则（前端）

单账号 `formatQuota(account)`：

1. `Pro` / `ProLite` → `∞`
2. `image_quota_unknown` 且非 Pro/ProLite → `未知`
3. 否则 → `quota` 数字

汇总卡片（有 `accountStats` 时）：

1. `unlimited_quota_count > 0` → `∞`
2. 否则 `unknown_quota_count > 0` → `未知`
3. 否则 → `total_quota` 紧凑格式（如 `68.8k`）

慢刷进度条旁统计：

- `本次明确额度` → `quota_total`
- `真无限额` → `unlimited_quota`
- `未知额度` → `unknown_quota`

## 测试

```bash
python -m pytest test/test_account_image_capabilities.py -q
python -m pytest test/test_account_refresh_all_service.py -q
```

重点用例：

- `test_stats_split_true_unlimited_and_unknown_quota`：统计拆分
- `test_true_unlimited_accounts_do_not_consume_quota`：Pro 不扣额度
- `test_limits_progress_image_gen_overrides_stale_unknown_quota`：有明确 remaining 时清除 unknown
- `test_refresh_all_splits_true_unlimited_and_unknown_quota`：慢刷计数拆分

## 线上验收命令

```bash
curl -sS 'https://gptimage.relai.asia/health?format=json' | python3 -m json.tool
```

关注 `accounts.unlimited_quota_count` 与 `accounts.unknown_quota_count` 是否 **同时存在**，且不再把非 Pro 的 unknown 计入 unlimited。

## 常见误判场景

| 场景 | 错误表现 | 正确表现 |
| --- | --- | --- |
| Plus 账号无 `image_gen` | `∞` | `未知` |
| Pro 账号无 `image_gen` | 被算进 `unknown_quota` | `∞`，计入 `unlimited_quota_count` |
| 刷新后有 `remaining=25` | 仍显示 `未知` | 显示 `25` |
| 慢刷把 Pro 计入 `unknown_quota` | 面板 unknown 偏高 | 计入 `unlimited_quota` |

---

## `restore_at` 与核对时间（2026-07-28）

### 两个不同字段

| 字段 | 来源 | 含义 |
| --- | --- | --- |
| `last_quota_refresh_at` | 本地：上次 `fetch_remote_info` 成功写库时刻 | **核对时间**（UI：「X 分钟前核对」） |
| `restore_at` | 上游 `limits_progress.image_gen.reset_after` | 额度窗口的 **结束/恢复时刻**（上游原值） |

**打开网页 / F5 不会打上游**；`GET /api/accounts` 只读库。若「核对时间」总像刚刚，是后台定时刷新（历史上 60s 全池）在更新 `last_quota_refresh_at`，不是浏览器触发的。

### 窗口结束 vs 预计恢复

| 账号状态 | `restore_at` 语义 | UI 建议文案 |
| --- | --- | --- |
| `quota > 0` | 当前额度**窗口结束**时刻（常 ≈ 进入周期后 +24h） | **窗口结束** + 绝对时间 |
| `quota == 0` | 上游说的**额度恢复**时刻 | **预计恢复** + 绝对时间 |
| 满额 25、从未生图、无锚点 | 可能为空或随探测漂移 | **待预热**（见 32 方案） |

有额度时不要把 `restore_at` 标成「恢复时间」——账号并未耗尽。

### 本地扣减 vs 上游

成功生图后 `mark_image_result` **本地** `quota -= 1`（并镜像 `limits_progress.remaining`），**不**自动改 `restore_at`。  
`restore_at` 仅在拉上游 limits 或预热生图后再 `fetch_remote_info` 时更新。

### 新鲜度门禁（将取消）

历史上 `image_quota_freshness_hours` 要求近期核对才允许调度。  
新方案（32）：以本地扣减为准，**关闭新鲜度**；四段日历 + 生图后即时刷新保证最终一致。

### 预热（quota window prime）

对 `quota==25 && success==0` 且非新号的账号，**一次性**最小生图（256×256）以钉住上游 `reset_after`。  
`quota < 25` 不自动预热。详见 [`32-quota-refresh-window-prime-plan.md`](32-quota-refresh-window-prime-plan.md)。
