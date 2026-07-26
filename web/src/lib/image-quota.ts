import type { Account } from "@/lib/api";

export type ImageQuotaState =
  | "unlimited"
  | "unknown"
  | "ready"
  | "unverified"
  | "stale"
  | "blocked"
  | "refresh_pending"
  | "exhausted";

export type AccountStatsQuotaFields = {
  total_quota?: number;
  unlimited_quota_count?: number;
  unknown_quota_count?: number;
  verified_total_quota?: number;
  available_image_quota?: number;
  latest_quota_refresh_at?: string | null;
  image_schedulable?: number;
  dispatchable_candidate_count?: number;
  schedulable?: number;
  scheduling_enabled?: number;
};

const QUOTA_STATE_LABEL: Record<ImageQuotaState, string> = {
  unlimited: "无限额",
  unknown: "未核对",
  ready: "可用",
  unverified: "待核对",
  stale: "待刷新",
  blocked: "不可调度",
  refresh_pending: "待恢复",
  exhausted: "已耗尽",
};

export function formatCompactNumber(value: number) {
  if (value >= 1000) {
    return `${(value / 1000).toFixed(1)}k`;
  }
  return String(value);
}

export function isUnlimitedImageQuotaAccount(account: Pick<Account, "type">) {
  const type = String(account.type || "").trim().toLowerCase();
  return type === "pro" || type === "prolite";
}

export function accountImageQuotaState(account: Account): ImageQuotaState {
  const state = String(account.image_quota_state || "").trim().toLowerCase();
  if (state in QUOTA_STATE_LABEL) {
    return state as ImageQuotaState;
  }
  if (isUnlimitedImageQuotaAccount(account)) {
    return "unlimited";
  }
  if (account.image_quota_unknown) {
    return "unknown";
  }
  if (typeof account.available_image_quota === "number" && account.available_image_quota > 0) {
    return "ready";
  }
  if (Number(account.quota || 0) > 0) {
    return "blocked";
  }
  return "exhausted";
}

export function formatAccountQuotaValue(account: Account) {
  if (isUnlimitedImageQuotaAccount(account) || accountImageQuotaState(account) === "unlimited") {
    return "∞";
  }
  if (account.image_quota_unknown || accountImageQuotaState(account) === "unknown") {
    return "未知";
  }
  // 账号行展示账面核对额度；观察态(identity_isolated)也显示真实 quota，不与可调度额度混同。
  const ledger = Math.max(0, Number(account.quota || 0));
  return String(ledger);
}

export function accountQuotaBadgeVariant(account: Account): "success" | "info" | "warning" | "secondary" | "danger" {
  const state = accountImageQuotaState(account);
  if (state === "ready") return "success";
  if (state === "unverified" || state === "refresh_pending") return "warning";
  if (state === "stale" || state === "blocked" || state === "unknown") return "secondary";
  if (state === "unlimited") return "info";
  return "secondary";
}

export function formatAccountQuotaHint(account: Account) {
  const state = accountImageQuotaState(account);
  const label = QUOTA_STATE_LABEL[state];
  const cached = Math.max(0, Number(account.quota || 0));
  if (state === "ready" || state === "unverified") {
    const schedulable = Math.max(0, Number(account.available_image_quota ?? account.quota ?? 0));
    return `生图可调度 ${schedulable}（账面 ${cached}，${label}）`;
  }
  if (state === "stale" || state === "blocked") {
    const schedulable = Math.max(0, Number(account.available_image_quota ?? 0));
    const schedPart = schedulable > 0 ? `，可调度 ${schedulable}` : "，当前不可调度";
    return `账面 ${cached}${schedPart}（${label}）`;
  }
  if (state === "unknown") {
    return "额度未核对，不参与生图调度";
  }
  if (state === "refresh_pending") {
    return "额度窗口待恢复，取号时将远程核对";
  }
  return label;
}

export function formatPoolQuotaFromStats(stats?: AccountStatsQuotaFields | null) {
  if (!stats) {
    return "—";
  }
  const unlimited = Number(stats.unlimited_quota_count || 0);
  if (unlimited > 0) {
    return "∞";
  }
  const available = Number(
    stats.available_image_quota ?? stats.verified_total_quota ?? stats.total_quota ?? 0,
  );
  return formatCompactNumber(Math.max(0, available));
}

export function formatPoolQuotaDetail(stats?: AccountStatsQuotaFields | null) {
  if (!stats) {
    return "";
  }
  const schedulable = Number(stats.image_schedulable ?? 0);
  const dispatchable = Number(stats.dispatchable_candidate_count ?? 0);
  const available = Number(stats.available_image_quota ?? stats.verified_total_quota ?? 0);
  const book = Number(stats.total_quota ?? 0);
  const refresh = formatQuotaRefreshAgeFromIso(
    (stats as AccountStatsQuotaFields & { latest_quota_refresh_at?: string | null }).latest_quota_refresh_at,
  );
  const refreshPart = refresh ? ` · ${refresh}` : "";
  return `可用 ${available} · 账面 ${book} · 生图候选 ${schedulable} · 可派发 ${dispatchable}${refreshPart}`;
}

export function formatQuotaRefreshAgeFromIso(raw?: string | null) {
  if (!raw) {
    return "未核对";
  }
  const at = new Date(raw.endsWith("Z") || raw.includes("+") ? raw : `${raw}Z`);
  if (Number.isNaN(at.getTime())) {
    return "未核对";
  }
  const diffMs = Math.max(0, Date.now() - at.getTime());
  const diffMin = Math.floor(diffMs / 60_000);
  if (diffMin < 1) {
    return "刚刚刷新";
  }
  if (diffMin < 60) {
    return `${diffMin}分钟前刷新`;
  }
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 48) {
    return `${diffHr}小时前刷新`;
  }
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}天前刷新`;
}

export function formatQuotaRefreshAge(account: Pick<Account, "last_quota_refresh_at">) {
  return formatQuotaRefreshAgeFromIso(account.last_quota_refresh_at);
}
