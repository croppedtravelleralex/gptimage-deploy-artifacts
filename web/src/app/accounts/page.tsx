"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ComponentProps } from "react";
import dynamic from "next/dynamic";
import {
  Ban,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleOff,
  CloudUpload,
  Copy,
  Download,
  Link2,
  LoaderCircle,
  LogIn,
  MessageSquare,
  Pencil,
  Play,
  Pause,
  RefreshCw,
  Search,
  Trash2,
  UserRound,
} from "lucide-react";
import { toast } from "sonner";

import { BindingSgHeatmap, normalizeBindingWeights } from "@/components/accounts/BindingSgHeatmap";
import { BindingActivityHeatmaps } from "@/components/accounts/BindingActivityHeatmaps";
import { AccountUsageHeatstrip } from "@/components/accounts/AccountUsageHeatstrip";
import { CfStatusLight, summarizeCfDay, type CfDayPoint } from "@/components/accounts/CfStatusLight";
import { EgressDriftLights } from "@/components/accounts/EgressDriftLights";
import { ScheduleCountdownIcons } from "@/components/accounts/ScheduleCountdownIcons";
import {
  accountImageQuotaState,
  accountQuotaBadgeVariant,
  formatAccountQuotaHint,
  formatAccountQuotaValue,
  formatPoolQuotaDetail,
  formatPoolQuotaFromStats,
  formatQuotaRefreshAge,
  formatCompactNumber as formatCompact,
  isUnlimitedImageQuotaAccount,
} from "@/lib/image-quota";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  deleteAccounts,
  fetchAccounts,
  fetchAccountMaintenanceLoopStatus,
  fetchModels,
  fetchOutlookAccountRecoveryProgress,
  fetchOutlookAutoRecoveryStatus,
  fetchPandaSyncSettings,
  fetchRefreshAllStatus,
  fetchRefreshProgress,
  fetchSettingsConfig,
  fetchReLoginProgress,
  recoverOutlookAccount,
  reLoginAccounts,
  refreshAccounts,
  startRefreshAllAccounts,
  stopRefreshAllAccounts,
  syncAccountsToPanda,
  fetchAccountsUsageRecent,
  fetchBindingUsageSlots,
  fetchIpNurtureBindings,
  fetchIpNurturePresets,
  processNurtureOne,
  fetchNurtureStatus,
  saveIpNurtureBinding,
  testProxy,
  updateAccountMaintenanceLoop,
  updateOutlookAutoRecovery,
  updatePandaSyncSettings,
  updateAccount,
  setAccountScheduling,
  setAccountsSchedulingBulk,
  type Account,
  type AccountMaintenanceLoopStatus,
  type AccountRefreshAllStatus,
  type AccountRefreshResponse,
  type AccountStatus,
  type AccountUsageRecentResponse,
  type BindingUsageSlotsResponse,
  type IpNurtureBinding,
  type IpNurturePreset,
  type Model,
  type OutlookAutoRecoveryStatus,
  type PandaAccountSyncResponse,
  type PandaSyncPublicSettings,
  type RefreshProgressResponse,
} from "@/lib/api";
import { humanizeUpstreamError } from "@/lib/chat-format";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

const AccountsActivityPanels = dynamic(() => import("./accounts-activity-panels"), {
  loading: () => (
    <div className="flex min-h-[200px] items-center justify-center rounded-2xl border border-white/80 bg-white/90 text-sm text-stone-500">
      加载账号流水…
    </div>
  ),
});

import { AccountImportDialog } from "./components/account-import-dialog";

const isOutlookRecoveryTerminal = (account: Account) => (
  String(account.outlook_recovery_state ?? "").trim().toLowerCase() === "terminal"
  || String(account.outlook_recovery_terminal_reason ?? "").trim().toLowerCase() === "account_deactivated"
);

const isOutlookRecoveryCandidate = (account: Account) => {
  if (isOutlookRecoveryTerminal(account) || account.status === "禁用") {
    return false;
  }
  const email = String(account.email ?? "").trim().toLowerCase();
  return (
    account.status === "异常"
    && ["@outlook.com", "@hotmail.com", "@live.com"].some((suffix) => email.endsWith(suffix))
  ) || (
    account.panda_receive_state === "rejected"
    && ["@outlook.com", "@hotmail.com", "@live.com"].some((suffix) => email.endsWith(suffix))
  );
};

/** 人工调度开关：空 receive_state 视为可调度；verified* 正式入池。 */
const isManualSchedulingEnabled = (account: Account) => {
  const receive = String(account.panda_receive_state ?? "").trim().toLowerCase();
  if (!receive) return true;
  return receive === "verified_ready" || receive === "verified" || receive === "local_verified";
};

const accountStatusOptions: { label: string; value: AccountStatus | "all" }[] = [
  { label: "全部状态", value: "all" },
  { label: "正常", value: "正常" },
  { label: "限流", value: "限流" },
  { label: "异常", value: "异常" },
  { label: "禁用", value: "禁用" },
];

const statusMeta: Record<
  AccountStatus,
  {
    icon: typeof CheckCircle2;
    badge: ComponentProps<typeof Badge>["variant"];
  }
> = {
  正常: { icon: CheckCircle2, badge: "success" },
  限流: { icon: CircleAlert, badge: "warning" },
  异常: { icon: CircleOff, badge: "danger" },
  禁用: { icon: Ban, badge: "secondary" },
};

const metricCards = [
  { key: "total", label: "账户总数", color: "text-stone-900", icon: UserRound },
  { key: "active", label: "正常账户", color: "text-emerald-600", icon: CheckCircle2 },
  {
    key: "schedulable",
    label: "进调度",
    color: "text-emerald-700",
    icon: CheckCircle2,
    title: "已打开「进调度」且状态为正常的账号数（人工开关）",
  },
  {
    key: "image_schedulable",
    label: "生图可用",
    color: "text-teal-700",
    icon: Play,
    title: "通过额度核对与生图门槛、当前可进入生图候选池的账号数",
  },
  { key: "limited", label: "限流账户", color: "text-orange-500", icon: CircleAlert },
  { key: "abnormal", label: "异常账户", color: "text-rose-500", icon: CircleOff },
  { key: "disabled", label: "禁用账户", color: "text-stone-500", icon: Ban },
  { key: "quota", label: "可用生图额度", color: "text-blue-500", icon: RefreshCw, title: "已核对且可参与生图调度的额度合计（非账面缓存总和）" },
] as const;

type AccountStats = {
  total: number;
  active: number;
  limited: number;
  abnormal: number;
  disabled: number;
  total_quota: number;
  unlimited_quota_count?: number;
  unknown_quota_count?: number;
  panda_staging_count?: number;
  panda_ready_count?: number;
  panda_synced_count?: number;
  panda_upload_queue_count?: number;
  panda_upload_eligible_count?: number;
  panda_upload_unsynced_eligible_count?: number;
  panda_upload_blocked_count?: number;
  panda_upload_retained_count?: number;
  panda_upload_remote_pending_count?: number;
  panda_upload_remote_verified_count?: number;
  panda_upload_remote_rejected_count?: number;
  panda_incoming_count?: number;
  panda_verified_count?: number;
  panda_rejected_count?: number;
  schedulable?: number;
  image_schedulable?: number;
  available_image_quota?: number;
  verified_total_quota?: number;
  dispatchable_candidate_count?: number;
  tainted_count?: number;
};

const maxRefreshTokens = 50;
const accountListLimit = 200;

const refreshAllStateText: Record<string, string> = {
  idle: "空闲",
  running: "运行中",
  paused: "资源保护暂停",
  stopping: "停止中",
  stopped: "已停止",
  completed: "已完成",
};

const maintenanceStateText: Record<string, string> = {
  off: "已关闭",
  idle: "等待中",
  running_batch: "保活批次中",
  cooldown: "冷却中",
  resource_paused: "资源保护暂停",
  traffic_paused: "流量保护暂停",
  manual_paused: "手动任务让路",
  error_backoff: "异常退避",
};

const outlookAutoRecoveryStateText: Record<string, string> = {
  off: "已关闭",
  idle: "等待中",
  scanning: "扫描中",
  recovering: "恢复中",
  paused: "已暂停",
};

function imageQuotaUnknown(account: Account) {
  return Boolean(account.image_quota_unknown);
}

function isUnknownImageQuotaAccount(account: Account) {
  return imageQuotaUnknown(account) && !isUnlimitedImageQuotaAccount(account);
}

const EGRESS_STATUS_PRIORITY: Record<string, number> = {
  error: 3,
  warn: 2,
  ok: 1,
  none: 0,
};

const CF_STATUS_PRIORITY: Record<string, number> = {
  error: 3,
  warn: 2,
  ok: 1,
  none: 0,
};

function bindingKeyForAccount(account: Account): string {
  const hash = String(account.proxy_binding_hash ?? "").trim();
  if (hash) return hash;
  const egress = String(account.proxy_egress_ip ?? "").trim();
  if (egress) return `egress:${egress}`;
  const raw = String(account.proxy ?? "").trim();
  if (raw) {
    try {
      const parsed = new URL(raw.includes("://") ? raw : `http://${raw}`);
      const host = parsed.port ? `${parsed.hostname}:${parsed.port}` : parsed.hostname;
      return `proxy:${host}`;
    } catch {
      const stripped = raw.replace(/^[a-z]+:\/\//i, "").replace(/^[^@]+@/, "").split("/")[0];
      return `proxy:${stripped || "unknown"}`;
    }
  }
  return "default";
}

function bindingLabelForAccount(account: Account) {
  return proxyDisplay(account).endpoint;
}

function proxyDisplay(account: Account) {
  const egressIp = String(account.proxy_egress_ip ?? "").trim();
  const rawProxy = String(account.proxy ?? "").trim();
  let endpoint = "默认出口";
  if (egressIp) {
    endpoint = egressIp;
  } else if (rawProxy) {
    try {
      const parsed = new URL(rawProxy);
      endpoint = parsed.port ? `${parsed.hostname}:${parsed.port}` : parsed.hostname;
    } catch {
      endpoint = rawProxy.replace(/^[a-z]+:\/\//i, "").replace(/^[^@]+@/, "").split("/")[0] || "账号代理";
    }
  }
  const provider = String(account.proxy_provider ?? "").trim();
  return {
    endpoint,
    provider,
    detail: provider || (rawProxy ? "账号级代理" : "运行时默认"),
  };
}

function egressDaysForAccount(account: Account) {
  const today = new Date();
  const dates: string[] = [];
  for (let i = 6; i >= 0; i -= 1) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    dates.push(`${y}-${m}-${day}`);
  }
  const byDate = new Map<string, { date: string; status: string; ip?: string }>();
  for (const row of account.egress_daily || []) {
    if (!row || typeof row !== "object") continue;
    const date = String(row.date || "").slice(0, 10);
    if (!date) continue;
    byDate.set(date, {
      date,
      status: String(row.status || "ok"),
      ip: String(row.ip || "") || undefined,
    });
  }
  return dates.map((date) => byDate.get(date) || { date, status: "none" });
}

function cfDaysForAccount(account: Account): CfDayPoint[] {
  const today = new Date();
  const dates: string[] = [];
  for (let i = 6; i >= 0; i -= 1) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    dates.push(`${y}-${m}-${day}`);
  }
  const byDate = new Map<string, CfDayPoint>();
  for (const row of account.cf_daily || []) {
    if (!row || typeof row !== "object") continue;
    const date = String(row.date || "").slice(0, 10);
    if (!date) continue;
    byDate.set(date, {
      date,
      ok: Math.max(0, Number(row.ok) || 0),
      cf: Math.max(0, Number(row.cf) || 0),
      image_fail: Math.max(0, Number(row.image_fail) || 0),
    });
  }
  return dates.map((date) => byDate.get(date) || { date, ok: 0, cf: 0, image_fail: 0 });
}

function aggregateEgressDays(accounts: Account[]) {
  if (!accounts.length) return [];
  const base = egressDaysForAccount(accounts[0]);
  return base.map((day, idx) => {
    let worst = day;
    let worstPri = EGRESS_STATUS_PRIORITY[String(day.status || "none").toLowerCase()] ?? 0;
    for (let i = 1; i < accounts.length; i += 1) {
      const other = egressDaysForAccount(accounts[i])[idx];
      const pri = EGRESS_STATUS_PRIORITY[String(other.status || "none").toLowerCase()] ?? 0;
      if (pri > worstPri) {
        worst = other;
        worstPri = pri;
      }
    }
    return worst;
  });
}

function aggregateCfDays(accounts: Account[]) {
  if (!accounts.length) return [];
  const base = cfDaysForAccount(accounts[0]);
  return base.map((day, idx) => {
    let worst = day;
    let worstPri = CF_STATUS_PRIORITY[summarizeCfDay(day).status] ?? 0;
    for (let i = 1; i < accounts.length; i += 1) {
      const other = cfDaysForAccount(accounts[i])[idx];
      const pri = CF_STATUS_PRIORITY[summarizeCfDay(other).status] ?? 0;
      if (pri > worstPri) {
        worst = other;
        worstPri = pri;
      }
    }
    return worst;
  });
}

function weightsForBinding(
  bindingKey: string,
  presets: IpNurturePreset[],
  bindings: Record<string, IpNurtureBinding>,
) {
  const binding = bindings[bindingKey];
  if (binding?.custom_matrix?.length) {
    return normalizeBindingWeights(binding.custom_matrix);
  }
  const preset = presets.find((item) => item.id === binding?.preset_id) || presets[0];
  return normalizeBindingWeights(preset?.weights || []);
}

const TABLE_COLUMN_COUNT = 13;

function formatRefreshAllState(state?: string) {
  const key = String(state || "idle");
  return refreshAllStateText[key] ?? key;
}

function formatRefreshAllOption(status: AccountRefreshAllStatus | null, key: string) {
  const value = status?.options?.[key];
  if (typeof value === "number" || typeof value === "string" || typeof value === "boolean") {
    return String(value);
  }
  return "-";
}

function formatRefreshAllResource(status: AccountRefreshAllStatus | null) {
  const resource = status?.resource ?? {};
  const parts: string[] = [];
  if (typeof resource.available_memory_mb === "number") {
    parts.push(`可用内存 ${resource.available_memory_mb}MB`);
  }
  if (typeof resource.memory_current_mb === "number" && typeof resource.memory_limit_mb === "number") {
    parts.push(`容器 ${resource.memory_current_mb}/${resource.memory_limit_mb}MB`);
  }
  if (typeof resource.load_1m === "number") {
    parts.push(`负载 ${resource.load_1m}`);
  }
  return parts.join(" · ");
}

function formatMaintenanceState(state?: string) {
  const key = String(state || "off");
  return maintenanceStateText[key] ?? key;
}

function formatMaintenanceTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatOutlookAutoRecoveryState(state?: string) {
  const key = String(state || "off");
  return outlookAutoRecoveryStateText[key] ?? key;
}

function formatCountdown(totalSeconds?: number | null) {
  if (typeof totalSeconds !== "number" || !Number.isFinite(totalSeconds) || totalSeconds < 0) {
    return "—";
  }
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remain = seconds % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(remain).padStart(2, "0")}`;
  }
  return `${String(minutes).padStart(2, "0")}:${String(remain).padStart(2, "0")}`;
}

function formatShortDateTime(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatMaintenanceResource(status: AccountMaintenanceLoopStatus | null) {
  const resource = status?.resource ?? {};
  const parts: string[] = [];
  if (typeof resource.load_1m === "number") parts.push(`load ${resource.load_1m}`);
  if (typeof resource.available_memory_mb === "number") parts.push(`mem ${resource.available_memory_mb}MB`);
  if (typeof resource.image_inflight === "number") parts.push(`inflight ${resource.image_inflight}`);
  return parts.join(" · ");
}

function formatRestoreAt(value?: string | null) {
  if (!value) {
    return { absolute: "—", relative: "" };
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return { absolute: value, relative: "" };
  }

  const diffMs = Math.max(0, date.getTime() - Date.now());
  const totalHours = Math.ceil(diffMs / (1000 * 60 * 60));
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  const relative = diffMs > 0 ? `剩余 ${days}d ${hours}h` : "已到恢复时间";

  const pad = (num: number) => String(num).padStart(2, "0");
  const absolute = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;

  return { absolute, relative };
}

function formatQuotaSummary(accounts: Account[]) {
  const availableAccounts = accounts.filter((account) => account.status === "正常");
  if (availableAccounts.some(isUnlimitedImageQuotaAccount)) {
    return "∞";
  }
  const availableSum = availableAccounts.reduce(
    (sum, account) => sum + Math.max(0, Number(account.available_image_quota ?? 0)),
    0,
  );
  if (availableSum > 0) {
    return formatCompact(availableSum);
  }
  if (availableAccounts.some(isUnknownImageQuotaAccount)) {
    return "未知";
  }
  return formatCompact(availableAccounts.reduce((sum, account) => sum + Math.max(0, account.quota), 0));
}

function maskToken(token?: string) {
  if (!token) return "—";
  if (token.length <= 18) return token;
  return `${token.slice(0, 16)}...${token.slice(-8)}`;
}

function compactToastMessage(value?: string) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= 180) {
    return text;
  }
  return `${text.slice(0, 177)}...`;
}

function formatPandaSyncDetails(data: PandaAccountSyncResponse) {
  const details = data.details ?? {};
  const parts = [
    `上传 ${data.synced ?? 0}`,
    `失败 ${data.failed ?? 0}`,
    `pending ${data.queued ?? 0}`,
    `本地删除 ${details.deleted_local ?? 0}`,
    `可上传 ${details.eligible ?? 0}`,
    `远端缺失重传 ${details.remote_missing_reupload ?? 0}`,
    `已在远端 ${details.already_remote ?? 0}`,
  ];
  const blocked = [
    details.blocked_by_config ?? 0,
    details.blocked_by_watermark ?? 0,
    details.blocked_by_state ?? 0,
    details.blocked_by_quota_or_status ?? 0,
    details.blocked_by_failure_evidence ?? 0,
    details.blocked_by_missing_quota_refresh ?? 0,
    details.blocked_by_probe_error ?? 0,
  ].reduce((sum, value) => sum + value, 0);
  if (blocked > 0) {
    parts.push(`阻断 ${blocked}`);
  }
  return parts.join(" · ");
}

function downloadTokens(accounts: Account[]) {
  const content = `${accounts.map((account) => account.access_token).join("\n")}\n`;
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `accounts-${Date.now()}.txt`;
  link.click();
  URL.revokeObjectURL(url);
}

function displayAccountType(account: Account) {
  return account.type || "Free";
}

function displayAccountSource(account: Account) {
  const source = String(account.source_type || "").trim().toLowerCase();
  if (!source) {
    return "web";
  }
  if (source === "web") {
    return "web";
  }
  return source;
}

function pandaSyncLabel(value?: string | null) {
  const state = String(value || "").trim().toLowerCase();
  if (state === "staging") return "探活中";
  if (state === "ready") return "待上传";
  if (state === "synced") return "已上传";
  if (state === "incoming") return "已导入";
  return "未进入";
}

function pandaSyncVariant(value?: string | null): ComponentProps<typeof Badge>["variant"] {
  const state = String(value || "").trim().toLowerCase();
  if (state === "synced" || state === "incoming") return "success";
  if (state === "ready") return "info";
  if (state === "staging") return "warning";
  return "secondary";
}

function pandaReceiveLabel(value?: string | null) {
  const state = String(value || "").trim().toLowerCase();
  if (state === "incoming") return "待验证";
  if (state === "verified" || state === "verified_ready" || state === "local_verified") return "已验证";
  if (state === "rejected") return "拒绝";
  return "本地";
}

function pandaReceiveVariant(value?: string | null): ComponentProps<typeof Badge>["variant"] {
  const state = String(value || "").trim().toLowerCase();
  if (state === "verified" || state === "verified_ready" || state === "local_verified") return "success";
  if (state === "incoming") return "warning";
  if (state === "rejected") return "danger";
  return "outline";
}

function formatPandaInlineError(value?: string | null) {
  const text = String(value || "").trim();
  if (!text) return "";
  const lower = text.toLowerCase();
  if (lower.startsWith("restored_after_accidental")) {
    return "事故恢复后隔离观察（待验证），不是账号已废";
  }
  if (lower.includes("account_deactivated")) {
    return "OpenAI 账号已删除或停用";
  }
  if (
    lower.includes("chat_requirements_prepare") ||
    lower.includes("chat_requirements_finalize")
  ) {
    return "对话鉴权被 CF/出口拦截（403），可重试或换节点；不等于账号作废";
  }
  // /backend-api/* 403 常见是 CF 边缘 HTML 拦截或出口抽风，不等于 token 失效、也不等于必须重登
  if (
    lower.includes("cf_edge_block")
    || lower.includes("cloudflare_or_edge_html_block")
    || (lower.includes("cloudflare") && (lower.includes("403") || lower.includes("block")))
    || (lower.includes("/backend-api/") && lower.includes("403"))
  ) {
    return "Web 接口被 Cloudflare/出口拦截（常间歇），可重试或换节点；通常无需重登";
  }
  return text;
}

function pandaStatusTitle(account: Account) {
  return [
    account.panda_ready_at ? `ready: ${formatShortDateTime(account.panda_ready_at)}` : "",
    account.panda_synced_at ? `synced: ${formatShortDateTime(account.panda_synced_at)}` : "",
    account.panda_imported_at ? `imported: ${formatShortDateTime(account.panda_imported_at)}` : "",
    account.panda_verified_at ? `verified: ${formatShortDateTime(account.panda_verified_at)}` : "",
    account.panda_rejected_at ? `rejected: ${formatShortDateTime(account.panda_rejected_at)}` : "",
    account.panda_probe_last_error ? `probe: ${account.panda_probe_last_error}` : "",
    account.panda_verify_last_error ? `verify: ${account.panda_verify_last_error}` : "",
    account.outlook_recovery_terminal_reason ? `recovery: ${account.outlook_recovery_terminal_reason}` : "",
    account.outlook_recovery_terminal_at ? `terminal: ${formatShortDateTime(account.outlook_recovery_terminal_at)}` : "",
  ].filter(Boolean).join("\n");
}

function AccountsPageContent() {
  const didLoadRef = useRef(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountTotal, setAccountTotal] = useState(0);
  const [accountStats, setAccountStats] = useState<AccountStats | null>(null);
  const [availableModels, setAvailableModels] = useState<Model[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState<AccountStatus | "all">("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState("100");
  const [editingAccount, setEditingAccount] = useState<Account | null>(null);
  const [editStatus, setEditStatus] = useState<AccountStatus>("正常");
  const [editProxy, setEditProxy] = useState("");
  const [isTestingProxy, setIsTestingProxy] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingModels, setIsLoadingModels] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshingTokens, setRefreshingTokens] = useState<Set<string>>(new Set());
  const [isDeleting, setIsDeleting] = useState(false);
  const [isSyncingPanda, setIsSyncingPanda] = useState(false);
  const [isUpdating, setIsUpdating] = useState(false);
  const [schedulingBusyTokens, setSchedulingBusyTokens] = useState<Set<string>>(new Set());
  const [isBulkScheduling, setIsBulkScheduling] = useState(false);
  const [usageByEmail, setUsageByEmail] = useState<AccountUsageRecentResponse["by_email"]>({});
  const [usageDates, setUsageDates] = useState<string[]>([]);
  const [accountViewMode, setAccountViewMode] = useState<"flat" | "grouped">("flat");
  const [nurturePresets, setNurturePresets] = useState<IpNurturePreset[]>([]);
  const [nurtureBindings, setNurtureBindings] = useState<Record<string, IpNurtureBinding>>({});
  const [bindingSaveBusy, setBindingSaveBusy] = useState<Set<string>>(new Set());
  const [bindingUsageSlots, setBindingUsageSlots] = useState<BindingUsageSlotsResponse["by_binding"]>({});
  const [weightEditKey, setWeightEditKey] = useState<string | null>(null);
  const [weightEditPreset, setWeightEditPreset] = useState("");
  const [weightEditMatrix, setWeightEditMatrix] = useState<number[][]>([]);
  const [isRelogining, setIsRelogining] = useState(false);
  const [isStartingRefreshAll, setIsStartingRefreshAll] = useState(false);
  const [isStoppingRefreshAll, setIsStoppingRefreshAll] = useState(false);
  const [refreshAllConcurrency, setRefreshAllConcurrency] = useState("4");
  const [refreshAllBatchSize, setRefreshAllBatchSize] = useState("25");
  const [refreshAllDelaySec, setRefreshAllDelaySec] = useState("0.2");
  const [refreshAllStatus, setRefreshAllStatus] = useState<AccountRefreshAllStatus | null>(null);
  const [refreshAllMaxConcurrency, setRefreshAllMaxConcurrency] = useState(8);
  const [maintenanceStatus, setMaintenanceStatus] = useState<AccountMaintenanceLoopStatus | null>(null);
  const [outlookAutoRecoveryStatus, setOutlookAutoRecoveryStatus] = useState<OutlookAutoRecoveryStatus | null>(null);
  const [outlookAutoRecoveryCountdown, setOutlookAutoRecoveryCountdown] = useState<number | null>(null);
  const [pandaSyncSettings, setPandaSyncSettings] = useState<PandaSyncPublicSettings | null>(null);
  const [isTogglingPandaSync, setIsTogglingPandaSync] = useState(false);
  const [lastPandaSyncResult, setLastPandaSyncResult] = useState<PandaAccountSyncResponse | null>(null);
  const [activityRefreshToken, setActivityRefreshToken] = useState(0);
  const [isTogglingMaintenance, setIsTogglingMaintenance] = useState(false);
  const [isTogglingOutlookAutoRecovery, setIsTogglingOutlookAutoRecovery] = useState(false);
  const [progress, setProgress] = useState<{
    visible: boolean;
    current: number;
    total: number;
    message: string;
    email: string;
  }>({
    visible: false,
    current: 0,
    total: 0,
    message: "",
    email: "",
  });
  const progressRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const refreshAllPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const maintenancePollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const outlookAutoRecoveryPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const outlookAutoRecoveryCountdownRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const refreshAllLastStateRef = useRef<string>("");
  const [refreshSummary, setRefreshSummary] = useState<Record<string, number | string> | null>(null);
  /** Multi-key sort: [{key, dir}] — default created_at desc */
  const [sortKeys, setSortKeys] = useState<Array<{ key: string; dir: "asc" | "desc" }>>([
    { key: "created_at", dir: "desc" },
  ]);

  const loadAccounts = async (silent = false, options: { bustCache?: boolean } = {}) => {
    if (!silent) {
      setIsLoading(true);
    }
    try {
      const data = await fetchAccounts({ limit: accountListLimit, bustCache: options.bustCache });
      setAccounts(data.items);
      setAccountTotal(typeof data.total === "number" ? data.total : data.items.length);
      setAccountStats(data.stats ?? null);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载账户失败";
      toast.error(message);
    } finally {
      if (!silent) {
        setIsLoading(false);
      }
    }
    // usage/recent 扫日志很慢，不阻塞首屏
    void fetchAccountsUsageRecent(4)
      .then((usage) => {
        if (usage?.by_email) setUsageByEmail(usage.by_email);
        if (usage?.dates?.length) setUsageDates(usage.dates);
      })
      .catch(() => null);
  };

  const handlePageRefresh = async () => {
    setIsLoading(true);
    try {
      await loadAccounts(true, { bustCache: true });
      setPage(1);
      await Promise.all([
        loadRefreshAllStatus(),
        loadIpNurtureData(),
      ]);
      setActivityRefreshToken((n) => n + 1);
      toast.success("号池数据已刷新");
    } catch (error) {
      const message = error instanceof Error ? error.message : "刷新失败";
      toast.error(message);
    } finally {
      setIsLoading(false);
    }
  };

  const refreshAccountPage = async () => {
    await loadAccounts(true, { bustCache: true });
    setPage(1);
  };

  const loadModels = async () => {
    setIsLoadingModels(true);
    try {
      const data = await fetchModels();
      setAvailableModels(Array.isArray(data.data) ? data.data : []);
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载模型列表失败";
      toast.error(message);
    } finally {
      setIsLoadingModels(false);
    }
  };

  const loadIpNurtureData = async () => {
    try {
      const [presetsRes, bindingsRes] = await Promise.all([
        fetchIpNurturePresets(),
        fetchIpNurtureBindings(),
      ]);
      setNurturePresets(Array.isArray(presetsRes.presets) ? presetsRes.presets : []);
      const map: Record<string, IpNurtureBinding> = {};
      for (const item of bindingsRes.bindings || []) {
        if (item?.binding_key) {
          map[item.binding_key] = item;
        }
      }
      setNurtureBindings(map);
      void fetchBindingUsageSlots(28)
        .then((res) => setBindingUsageSlots(res.by_binding || {}))
        .catch(() => setBindingUsageSlots({}));
    } catch {
      // 后端未部署时静默降级
    }
  };

  const saveBindingPreset = async (bindingKey: string, presetId: string, customMatrix?: number[][]) => {
    setBindingSaveBusy((prev) => new Set(prev).add(bindingKey));
    try {
      const result = await saveIpNurtureBinding(bindingKey, presetId, customMatrix);
      if (result.binding?.binding_key) {
        setNurtureBindings((prev) => ({ ...prev, [result.binding.binding_key]: result.binding }));
      }
      toast.success("IP 养号日历已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存绑定日历失败");
    } finally {
      setBindingSaveBusy((prev) => {
        const next = new Set(prev);
        next.delete(bindingKey);
        return next;
      });
    }
  };

  useEffect(() => {
    if (didLoadRef.current) {
      return;
    }
    didLoadRef.current = true;
    void loadAccounts();
    // 次要状态延后，避免首屏并行打满接口（模型列表仅编辑时需要，按需懒加载）
    const timer = window.setTimeout(() => {
      void loadRefreshSettings();
      void loadRefreshAllStatus();
      void loadOutlookAutoRecoveryStatus();
      void loadPandaSyncSettings();
    }, 50);

    // 清理进度条定时器
    return () => {
      window.clearTimeout(timer);
      if (progressRef.current) clearInterval(progressRef.current);
      if (refreshAllPollRef.current) clearInterval(refreshAllPollRef.current);
      if (maintenancePollRef.current) clearInterval(maintenancePollRef.current);
      if (outlookAutoRecoveryPollRef.current) clearInterval(outlookAutoRecoveryPollRef.current);
      if (outlookAutoRecoveryCountdownRef.current) clearInterval(outlookAutoRecoveryCountdownRef.current);
    };
  }, []);

  useEffect(() => {
    if (accountViewMode !== "grouped") {
      return;
    }
    void loadIpNurtureData();
  }, [accountViewMode]);

  const isUploadSyncNode = Boolean(pandaSyncSettings?.base_url?.trim());
  const refreshAllActive = Boolean(refreshAllStatus?.state === "running" || refreshAllStatus?.state === "paused" || refreshAllStatus?.state === "stopping");

  const loadRefreshAllStatus = async () => {
    try {
      const status = await fetchRefreshAllStatus();
      setRefreshAllStatus(status);
      return status;
    } catch {
      return null;
    }
  };

  const loadRefreshSettings = async () => {
    try {
      const data = await fetchSettingsConfig();
      const settings = data.config.account_refresh_all ?? {};
      const maxConcurrency = Number(settings.max_concurrency || settings.concurrency || 8);
      setRefreshAllMaxConcurrency(maxConcurrency);
      setRefreshAllConcurrency(String(settings.concurrency ?? 4));
      setRefreshAllBatchSize(String(settings.batch_size ?? 25));
      setRefreshAllDelaySec(String(settings.delay_between_accounts_sec ?? 0.2));
    } catch {
      // Keep local defaults if settings are unavailable.
    }
  };

  const loadMaintenanceStatus = async () => {
    try {
      const status = await fetchAccountMaintenanceLoopStatus();
      setMaintenanceStatus(status);
      return status;
    } catch {
      return null;
    }
  };

  const loadOutlookAutoRecoveryStatus = async () => {
    try {
      const status = await fetchOutlookAutoRecoveryStatus();
      setOutlookAutoRecoveryStatus(status);
      if (typeof status.seconds_until_next_run === "number") {
        setOutlookAutoRecoveryCountdown(status.seconds_until_next_run);
      } else if (status.next_run_at) {
        const deadline = new Date(status.next_run_at).getTime();
        setOutlookAutoRecoveryCountdown(
          Number.isNaN(deadline) ? null : Math.max(0, Math.floor((deadline - Date.now()) / 1000)),
        );
      } else {
        setOutlookAutoRecoveryCountdown(null);
      }
      return status;
    } catch {
      return null;
    }
  };

  const loadPandaSyncSettings = async () => {
    try {
      const data = await fetchPandaSyncSettings();
      setPandaSyncSettings(data.panda_sync);
      return data.panda_sync;
    } catch {
      return null;
    }
  };

  useEffect(() => {
    if (refreshAllStatus?.state === "stopping") {
      setIsStoppingRefreshAll(true);
    }
    if (refreshAllStatus?.state === "stopped" || refreshAllStatus?.state === "completed" || refreshAllStatus?.state === "idle") {
      setIsStoppingRefreshAll(false);
    }
    if (!refreshAllActive) {
      if (refreshAllPollRef.current) {
        clearInterval(refreshAllPollRef.current);
        refreshAllPollRef.current = null;
      }
      return;
    }
    if (refreshAllPollRef.current) {
      return;
    }
    refreshAllPollRef.current = setInterval(() => {
      void (async () => {
        const status = await loadRefreshAllStatus();
        if (!status) return;
        const previous = refreshAllLastStateRef.current;
        refreshAllLastStateRef.current = status.state;
        if ((status.state === "completed" || status.state === "stopped") && previous && previous !== status.state) {
          setIsStoppingRefreshAll(false);
          await loadAccounts(true);
          toast.success(status.state === "completed" ? "慢速刷新全部额度已完成" : "慢速刷新任务已停止");
        }
      })();
    }, 2000);
  }, [refreshAllActive]);

  useEffect(() => {
    if (maintenancePollRef.current) {
      return;
    }
    maintenancePollRef.current = setInterval(() => {
      void loadMaintenanceStatus();
    }, 3000);
    return () => {
      if (maintenancePollRef.current) {
        clearInterval(maintenancePollRef.current);
        maintenancePollRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (outlookAutoRecoveryPollRef.current) {
      return;
    }
    outlookAutoRecoveryPollRef.current = setInterval(() => {
      void loadOutlookAutoRecoveryStatus();
    }, 3000);
    return () => {
      if (outlookAutoRecoveryPollRef.current) {
        clearInterval(outlookAutoRecoveryPollRef.current);
        outlookAutoRecoveryPollRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (outlookAutoRecoveryCountdownRef.current) {
      clearInterval(outlookAutoRecoveryCountdownRef.current);
      outlookAutoRecoveryCountdownRef.current = null;
    }
    if (!outlookAutoRecoveryStatus?.enabled || outlookAutoRecoveryCountdown == null) {
      return;
    }
    outlookAutoRecoveryCountdownRef.current = setInterval(() => {
      setOutlookAutoRecoveryCountdown((prev) => {
        if (prev == null) return prev;
        return Math.max(0, prev - 1);
      });
    }, 1000);
    return () => {
      if (outlookAutoRecoveryCountdownRef.current) {
        clearInterval(outlookAutoRecoveryCountdownRef.current);
        outlookAutoRecoveryCountdownRef.current = null;
      }
    };
  }, [outlookAutoRecoveryStatus?.enabled, outlookAutoRecoveryStatus?.next_run_at]);

  const filteredAccounts = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    const filtered = accounts.filter((account) => {
      const searchMatched =
        normalizedQuery.length === 0 || (account.email ?? "").toLowerCase().includes(normalizedQuery);
      const typeMatched = typeFilter === "all" || displayAccountType(account) === typeFilter;
      const statusMatched = statusFilter === "all" || account.status === statusFilter;
      return searchMatched && typeMatched && statusMatched;
    });

    const cmp = (a: Account, b: Account, key: string, dir: "asc" | "desc") => {
      const mul = dir === "asc" ? 1 : -1;
      const proxyHost = (acc: Account) => {
        const raw = String(acc.proxy || "").trim();
        try {
          const u = new URL(raw.includes("://") ? raw : `http://${raw}`);
          return u.hostname || raw;
        } catch {
          return raw;
        }
      };
      let av: string | number = "";
      let bv: string | number = "";
      switch (key) {
        case "email":
          av = String(a.email || "").toLowerCase();
          bv = String(b.email || "").toLowerCase();
          break;
        case "type":
          av = displayAccountType(a);
          bv = displayAccountType(b);
          break;
        case "status":
          av = a.status;
          bv = b.status;
          break;
        case "schedule":
          av = isManualSchedulingEnabled(a) ? 1 : 0;
          bv = isManualSchedulingEnabled(b) ? 1 : 0;
          break;
        case "quota":
          av = Number(a.quota || 0);
          bv = Number(b.quota || 0);
          break;
        case "proxy":
          av = proxyHost(a);
          bv = proxyHost(b);
          break;
        case "created_at":
        default:
          av = String(a.created_at || "");
          bv = String(b.created_at || "");
          break;
      }
      if (av < bv) return -1 * mul;
      if (av > bv) return 1 * mul;
      return 0;
    };

    const keys = sortKeys.length ? sortKeys : [{ key: "created_at", dir: "desc" as const }];
    return [...filtered].sort((a, b) => {
      for (const { key, dir } of keys) {
        const r = cmp(a, b, key, dir);
        if (r !== 0) return r;
      }
      // proxy column: same host → created_at
      if (keys.some((k) => k.key === "proxy")) {
        return cmp(a, b, "created_at", "desc");
      }
      return 0;
    });
  }, [accounts, query, statusFilter, typeFilter, sortKeys]);

  const toggleSort = (key: string, shiftKey: boolean) => {
    setSortKeys((prev) => {
      const existing = prev.find((k) => k.key === key);
      if (shiftKey) {
        if (existing) {
          return prev.map((k) =>
            k.key === key ? { ...k, dir: k.dir === "asc" ? "desc" : "asc" } : k,
          );
        }
        return [...prev, { key, dir: "asc" }];
      }
      if (existing && prev.length === 1) {
        return [{ key, dir: existing.dir === "asc" ? "desc" : "asc" }];
      }
      return [{ key, dir: "desc" }];
    });
  };

  const sortIndicator = (key: string) => {
    const idx = sortKeys.findIndex((k) => k.key === key);
    if (idx < 0) return "";
    const arrow = sortKeys[idx].dir === "asc" ? "↑" : "↓";
    return sortKeys.length > 1 ? `${arrow}${idx + 1}` : arrow;
  };

  const pageCount = Math.max(1, Math.ceil(filteredAccounts.length / Number(pageSize)));
  const safePage = Math.min(page, pageCount);
  const startIndex = (safePage - 1) * Number(pageSize);
  const currentRows = filteredAccounts.slice(startIndex, startIndex + Number(pageSize));
  const allCurrentSelected =
    currentRows.length > 0 && currentRows.every((row) => selectedIds.includes(row.access_token));

  const accountGroups = useMemo(() => {
    const map = new Map<string, { key: string; label: string; accounts: Account[] }>();
    for (const account of filteredAccounts) {
      const key = bindingKeyForAccount(account);
      const existing = map.get(key);
      if (existing) {
        existing.accounts.push(account);
      } else {
        map.set(key, {
          key,
          label: bindingLabelForAccount(account),
          accounts: [account],
        });
      }
    }
    return Array.from(map.values()).sort((a, b) => a.label.localeCompare(b.label, "zh-CN"));
  }, [filteredAccounts]);

  const tableBlocks = useMemo(() => {
    if (accountViewMode === "flat") {
      return currentRows.map((account, rowIndex) => ({
        kind: "account" as const,
        account,
        rowNo: startIndex + rowIndex + 1,
      }));
    }
    const blocks: Array<
      | { kind: "group"; key: string; label: string; accounts: Account[] }
      | { kind: "account"; account: Account; rowNo: number }
    > = [];
    const seenGroups = new Set<string>();
    let rowCounter = startIndex;
    for (const account of currentRows) {
      const key = bindingKeyForAccount(account);
      if (!seenGroups.has(key)) {
        seenGroups.add(key);
        const group = accountGroups.find((item) => item.key === key);
        if (group) {
          blocks.push({
            kind: "group",
            key: group.key,
            label: group.label,
            accounts: group.accounts,
          });
        }
      }
      rowCounter += 1;
      blocks.push({ kind: "account", account, rowNo: rowCounter });
    }
    return blocks;
  }, [accountViewMode, accountGroups, currentRows, startIndex]);

  const summary = useMemo(() => {
    if (accountStats) {
      return {
        total: accountStats.total,
        active: accountStats.active,
        limited: accountStats.limited,
        abnormal: accountStats.abnormal,
        disabled: accountStats.disabled,
        schedulable: typeof accountStats.schedulable === "number" ? accountStats.schedulable : 0,
        image_schedulable: typeof accountStats.image_schedulable === "number" ? accountStats.image_schedulable : 0,
        quota: formatPoolQuotaFromStats(accountStats),
      };
    }
    const total = accounts.length;
    const active = accounts.filter((item) => item.status === "正常").length;
    const limited = accounts.filter((item) => item.status === "限流").length;
    const abnormal = accounts.filter((item) => item.status === "异常").length;
    const disabled = accounts.filter((item) => item.status === "禁用").length;
    const schedulable = accounts.filter((a) => isManualSchedulingEnabled(a) && a.status === "正常").length;
    const imageSchedulable = accounts.filter((a) => a.image_schedulable).length;
    const quota = formatQuotaSummary(accounts);

    return { total, active, limited, abnormal, disabled, schedulable, image_schedulable: imageSchedulable, quota };
  }, [accountStats, accounts]);

  const accountTypeOptions = useMemo(
    () => [
      { label: "全部类型", value: "all" },
      ...Array.from(new Set(accounts.map(displayAccountType))).map((type) => ({ label: type, value: type })),
    ],
    [accounts],
  );

  const selectedTokens = useMemo(() => {
    const selectedSet = new Set(selectedIds);
    return accounts.filter((item) => selectedSet.has(item.access_token)).map((item) => item.access_token);
  }, [accounts, selectedIds]);

  const refreshPageTokens = selectedTokens.length > 0
    ? selectedTokens
    : currentRows.map((item) => item.access_token);
  const canRefreshPageTokens = refreshPageTokens.length > 0 && refreshPageTokens.length <= maxRefreshTokens;

  const abnormalTokens = useMemo(() => {
    return accounts.filter((item) => item.status === "异常").map((item) => item.access_token);
  }, [accounts]);

  const paginationItems = useMemo(() => {
    const items: (number | "...")[] = [];
    const start = Math.max(1, safePage - 1);
    const end = Math.min(pageCount, safePage + 1);

    if (start > 1) items.push(1);
    if (start > 2) items.push("...");
    for (let current = start; current <= end; current += 1) items.push(current);
    if (end < pageCount - 1) items.push("...");
    if (end < pageCount) items.push(pageCount);

    return items;
  }, [pageCount, safePage]);

  const handleDeleteTokens = async (tokens: string[]) => {
    if (tokens.length === 0) {
      toast.error("请先选择要删除的账户");
      return;
    }

    setIsDeleting(true);
    try {
      const data = await deleteAccounts(tokens);
      await refreshAccountPage();
      setActivityRefreshToken((n) => n + 1);
      setSelectedIds((prev) => prev.filter((id) => !tokens.includes(id)));
      toast.success(`删除 ${data.removed ?? 0} 个账户`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "删除账户失败";
      toast.error(message);
    } finally {
      setIsDeleting(false);
    }
  };

  const handleSyncPanda = async () => {
    setIsSyncingPanda(true);
    try {
      const data = await syncAccountsToPanda();
      setLastPandaSyncResult(data);
      const detail = compactToastMessage(formatPandaSyncDetails(data));
      await refreshAccountPage();
      setActivityRefreshToken((n) => n + 1);
      if (data.ok) {
        toast.success(detail ? `上传完成：${detail}` : "上传完成");
      } else {
        toast.error(detail ? `上传失败：${detail}` : data.error || "上传失败");
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "上传到 Panda 失败";
      toast.error(message);
    } finally {
      setIsSyncingPanda(false);
    }
  };

  const handleTogglePandaSync = async () => {
    const nextEnabled = !pandaSyncSettings?.enabled;
    setIsTogglingPandaSync(true);
    try {
      const data = await updatePandaSyncSettings({ enabled: nextEnabled });
      setPandaSyncSettings(data.panda_sync);
      toast.success(nextEnabled ? "自动上传已开启" : "自动上传已暂停");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新自动上传失败");
    } finally {
      setIsTogglingPandaSync(false);
    }
  };

  const handleStartRefreshAll = async () => {
    setIsStartingRefreshAll(true);
    refreshAllLastStateRef.current = "";
    try {
      const requestedConcurrency = Number(refreshAllConcurrency) || undefined;
      const status = await startRefreshAllAccounts({
        concurrency: requestedConcurrency,
        max_concurrency: requestedConcurrency,
        batch_size: Number(refreshAllBatchSize) || undefined,
        delay_between_accounts_sec: Number(refreshAllDelaySec) || undefined,
        stale_after_hours: 0,
        include_recent: true,
        resource_pause_enabled: false,
        delete_invalid: false,
        delete_after_failures: 1,
      });
      setRefreshAllStatus(status);
      const effectiveConcurrency = Number(status.options?.concurrency || 0);
      if (requestedConcurrency && effectiveConcurrency && requestedConcurrency !== effectiveConcurrency) {
        toast.info(`并发已按上限生效：${requestedConcurrency} -> ${effectiveConcurrency}`);
      }
      if (status.state === "completed" && status.total === 0) {
        toast.info(`没有需要慢刷的账号，已跳过 ${status.skipped} 个近期刷新账号`);
      } else {
        toast.success(`慢速刷新已启动：队列 ${status.total} 个账号`);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动慢速刷新失败");
    } finally {
      setIsStartingRefreshAll(false);
    }
  };

  const handleStopRefreshAll = async () => {
    setIsStoppingRefreshAll(true);
    try {
      const status = await stopRefreshAllAccounts();
      setRefreshAllStatus(status);
      toast.success("已请求停止慢速刷新，正在等待当前请求结束");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "停止慢速刷新失败");
    }
  };

  const maintenanceSafeSettings = {
    batch_limit: 80,
    concurrency: 1,
    batch_size: 20,
    cooldown_sec: 10,
    stale_after_hours: 0,
    include_recent: true,
    resource_pause_enabled: false,
    slow_when_image_inflight: 8,
    pause_when_image_inflight: 0,
    slow_batch_limit: 20,
    slow_delay_between_accounts_sec: 3,
    slow_cooldown_sec: 10,
    startup_delay_sec: 5,
    delete_invalid: false,
    delete_after_failures: 1,
  };

  const updateMaintenanceEnabled = async (enabled: boolean, successMessage?: string) => {
    setIsTogglingMaintenance(true);
    try {
      const status = await updateAccountMaintenanceLoop({
        enabled,
        ...maintenanceSafeSettings,
      });
      setMaintenanceStatus(status);
      toast.success(successMessage ?? (enabled ? "panda 轻量保活已开启" : "panda 轻量保活已关闭"));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新轻量保活失败");
    } finally {
      setIsTogglingMaintenance(false);
    }
  };

  const handleToggleMaintenance = async () => {
    await updateMaintenanceEnabled(!Boolean(maintenanceStatus?.enabled));
  };

  const handleApplyMaintenanceSafeSettings = async () => {
    await updateMaintenanceEnabled(Boolean(maintenanceStatus?.enabled), "已应用保活新版参数：不硬暂停、每轮 80、慢速 20");
  };

  const handleToggleOutlookAutoRecovery = async () => {
    setIsTogglingOutlookAutoRecovery(true);
    try {
      const status = await updateOutlookAutoRecovery({
        enabled: !Boolean(outlookAutoRecoveryStatus?.enabled),
      });
      setOutlookAutoRecoveryStatus(status);
      if (typeof status.seconds_until_next_run === "number") {
        setOutlookAutoRecoveryCountdown(status.seconds_until_next_run);
      } else {
        setOutlookAutoRecoveryCountdown(null);
      }
      toast.success(status.enabled ? "自动恢复已开启" : "自动恢复已关闭");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新自动恢复失败");
    } finally {
      setIsTogglingOutlookAutoRecovery(false);
    }
  };

  const handleRecoverOutlookAccount = async (account: Account) => {
    const accessToken = account.access_token;
    setRefreshingTokens((prev) => new Set([...prev, accessToken]));
    try {
      const { progress_id } = await recoverOutlookAccount(accessToken);
      const deadline = Date.now() + 15 * 60 * 1000;
      let recovery = await fetchOutlookAccountRecoveryProgress(progress_id);
      while (!recovery.done) {
        if (Date.now() >= deadline) {
          throw new Error("Outlook 恢复等待超时，请稍后刷新页面查看结果");
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        recovery = await fetchOutlookAccountRecoveryProgress(progress_id);
      }
      if (!recovery.ok) {
        throw new Error(recovery.error || "Outlook 账号恢复失败");
      }
      await refreshAccountPage();
      const quota = recovery.result?.quota;
      toast.success(
        typeof quota === "number"
          ? `Outlook 账号已恢复并重新入调度，额度 ${quota}`
          : "Outlook 账号已恢复并重新入调度",
      );
    } catch (error) {
      const raw = error instanceof Error ? error.message : "Outlook 账号恢复失败";
      const mapped =
        raw.includes("need_openai_password")
          ? "账号缺少 OpenAI 密码，无法密码重登"
          : raw.includes("yumail_not_configured")
            ? "未配置 YuMail API Key，无法收取验证码"
            : raw.includes("yumail_unreachable")
              ? "YuMail 环回不可达（请确认本机 8782 / 勿走公网域名）"
              : raw.includes("yumail_otp_timeout")
                ? "YuMail 等待验证码超时"
                : raw.includes("yumail_outlook_token_not_found")
                  ? "YuMail 邮件池中未找到该 Outlook 邮箱"
                  : raw.includes("need_outlook_mailbox_credentials")
                    ? "缺少邮箱凭据且 YuMail 不可用，无法收取验证码"
                    : raw;
      toast.error(mapped);
    } finally {
      setRefreshingTokens((prev) => {
        const next = new Set(prev);
        next.delete(accessToken);
        return next;
      });
    }
  };

  const handleAccountRefreshAction = async (account: Account) => {
    if (isOutlookRecoveryTerminal(account)) {
      toast.error("OpenAI 账号已删除或停用，系统已停止自动恢复；官方恢复后请重新导入账号");
      return;
    }
    if (isOutlookRecoveryCandidate(account)) {
      await handleRecoverOutlookAccount(account);
      return;
    }
    await handleRefreshAccounts([account.access_token]);
  };

  const handleRefreshAccounts = async (accessTokens: string[]) => {
    if (accessTokens.length === 0) {
      toast.error("没有需要刷新的账户");
      return;
    }
    if (accessTokens.length > maxRefreshTokens) {
      toast.error(`单次最多刷新 ${maxRefreshTokens} 个账号，请先筛选或选择少量账号`);
      return;
    }

    if (accessTokens.length === 1) {
      setRefreshingTokens((prev) => new Set([...prev, accessTokens[0]]));
      try {
        const { progress_id } = await refreshAccounts(accessTokens);
        // 单账号：轮询等待完成
        await pollRefreshProgress(progress_id, (progress) => {
          if (progress.done && progress.result) {
            void refreshAccountPage();
          }
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : "刷新账户失败";
        toast.error(message);
      } finally {
        setRefreshingTokens((prev) => {
          const next = new Set(prev);
          next.delete(accessTokens[0]);
          return next;
        });
      }
      return;
    }

    setIsRefreshing(true);

    // 计算非选中账号的基数（统计卡片联动用）
    const selectedTokenSet = new Set(accessTokens);
    const baseAccountsList = accounts.filter((a) => !selectedTokenSet.has(a.access_token));
    const baseActive = baseAccountsList.filter((a) => a.status === "正常").length;
    const baseLimited = baseAccountsList.filter((a) => a.status === "限流").length;
    const baseAbnormal = baseAccountsList.filter((a) => a.status === "异常").length;
    const baseDisabled = baseAccountsList.filter((a) => a.status === "禁用").length;
    const baseNormalAccounts = baseAccountsList.filter((a) => a.status === "正常");
    const baseHasUnlimited = baseNormalAccounts.some(isUnlimitedImageQuotaAccount);
    const baseHasUnknown = baseNormalAccounts.some(isUnknownImageQuotaAccount);
    const baseQuotaNum = baseNormalAccounts.reduce((s, a) => s + Math.max(0, a.quota), 0);

    // 显示进度条（只显示当前任务，不含分类统计）
    const total = accessTokens.length;
    setProgress({
      visible: true,
      current: 0,
      total,
      message: "正在刷新账号信息...",
      email: "",
    });

    try {
      const { progress_id } = await refreshAccounts(accessTokens);

      // 轮询进度到完成
      const data = await new Promise<AccountRefreshResponse>((resolve, reject) => {
        const pollTimer = setInterval(async () => {
          try {
            const p = await fetchRefreshProgress(progress_id);
            if (p.done) {
              clearInterval(pollTimer);
              if (p.error) {
                reject(new Error(p.error));
                return;
              }
              if (!p.result) {
                reject(new Error("刷新结果为空"));
                return;
              }
              // 更新最终进度显示
              setProgress((prev) => ({
                ...prev,
                current: prev.total,
                message: "刷新完成",
              }));
              // 清除联动统计
              setRefreshSummary(null);
              resolve(p.result);
            } else {
              // 实时更新进度
              setProgress((prev) => ({
                ...prev,
                current: p.processed,
              }));
              // 实时更新统计卡片：基数 + 已刷新的累加结果
              const runningActive = baseActive + ((p.status_counts?.["正常"]) ?? 0);
              const runningLimited = baseLimited + ((p.status_counts?.["限流"]) ?? 0);
              const runningAbnormal = baseAbnormal + ((p.status_counts?.["异常"]) ?? 0);
              const runningDisabled = baseDisabled + ((p.status_counts?.["禁用"]) ?? 0);
              let runningQuota: string | number;
              if (baseHasUnlimited) {
                runningQuota = "∞";
              } else if (baseHasUnknown) {
                runningQuota = "未知";
              } else {
                runningQuota = formatCompact(baseQuotaNum + (p.total_quota ?? 0));
              }
              setRefreshSummary({
                total: accountTotal || accounts.length,
                active: runningActive,
                limited: runningLimited,
                abnormal: runningAbnormal,
                disabled: runningDisabled,
                quota: runningQuota,
              });
            }
          } catch (err) {
            clearInterval(pollTimer);
            reject(err);
          }
        }, 300);
      });

      // 刷新完成，更新数据
      await refreshAccountPage();

      const relogined = data.relogined ?? 0;

      // 显示重新登录进度
      if (relogined > 0) {
        setProgress({
          visible: true,
          current: 0,
          total: relogined,
          message: `正在尝试对 ${relogined} 个账号进行移除异常状态`,
          email: "",
        });
        // 模拟重新登录进度
        let reCount = 0;
        await new Promise<void>((resolve) => {
          const timer = setInterval(() => {
            reCount += 1;
            if (reCount >= relogined) {
              clearInterval(timer);
              setProgress({
                visible: true,
                current: relogined,
                total: relogined,
                message: "移除异常状态完成",
                email: "",
              });
              setTimeout(() => setProgress({ visible: false, current: 0, total: 0, message: "", email: "" }), 800);
              resolve();
            } else {
              setProgress((prev) => ({ ...prev, current: reCount }));
            }
          }, 150);
          setTimeout(resolve, 2000);
        });
      } else {
        setProgress({
          visible: true,
          current: total,
          total,
          message: "刷新完成",
          email: "",
        });
        setTimeout(() => setProgress({ visible: false, current: 0, total: 0, message: "", email: "" }), 800);
      }

      if ((data.errors ?? []).length > 0) {
        const firstError = data.errors?.[0]?.error;
        toast.error(
          `刷新成功 ${data.refreshed} 个，失败 ${(data.errors ?? []).length} 个${firstError ? `，首个错误：${firstError}` : ""}`,
        );
      } else {
      setActivityRefreshToken((n) => n + 1);
      toast.success(`刷新成功 ${data.refreshed} 个账户${relogined > 0 ? `，已触发 ${relogined} 个账号重新登录` : ""}`);
      }
    } catch (error) {
      setProgress({ visible: false, current: 0, total: 0, message: "", email: "" });
      setRefreshSummary(null);
      const message = error instanceof Error ? error.message : "刷新账户失败";
      toast.error(message);
    } finally {
      setIsRefreshing(false);
    }
  };

  const pollRefreshProgress = async (
    progressId: string,
    onUpdate: (p: RefreshProgressResponse) => void,
  ): Promise<void> => {
    let missingRetries = 0;
    return new Promise<void>((resolve, reject) => {
      const timer = setInterval(async () => {
        try {
          const p = await fetchRefreshProgress(progressId);
          missingRetries = 0;
          if (p.done) {
            clearInterval(timer);
            if (p.error) {
              reject(new Error(p.error));
            } else {
              onUpdate(p);
              resolve();
            }
          }
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          if (message.includes("progress not found") && missingRetries < 12) {
            missingRetries += 1;
            return;
          }
          clearInterval(timer);
          reject(err);
        }
      }, 500);
    });
  };

  const handleReLogin = async (accessTokens: string[]) => {
    if (accessTokens.length === 0) {
      toast.error("请先选择要恢复的账户");
      return;
    }

    // 只处理异常账号，过滤非异常账号
    const abnormalTokens = accessTokens.filter((token) => {
      const account = accounts.find((a) => a.access_token === token);
      return account?.status === "异常";
    });

    if (abnormalTokens.length === 0) {
      toast.error("选中账号中没有异常账号");
      return;
    }

    if (abnormalTokens.length < accessTokens.length) {
      toast.info(`已过滤 ${accessTokens.length - abnormalTokens.length} 个非异常账号`);
    }

    setIsRelogining(true);

    // 计算非选中账号的基数（统计卡片联动用）
    const selectedTokenSet = new Set(abnormalTokens);
    const baseAccountsList = accounts.filter((a) => !selectedTokenSet.has(a.access_token));
    const baseActive = baseAccountsList.filter((a) => a.status === "正常").length;
    const baseLimited = baseAccountsList.filter((a) => a.status === "限流").length;
    const baseAbnormal = baseAccountsList.filter((a) => a.status === "异常").length;
    const baseDisabled = baseAccountsList.filter((a) => a.status === "禁用").length;

    // 显示进度条（真实进度）
    const total = abnormalTokens.length;
    setProgress({ visible: true, current: 0, total, message: "正在尝试恢复异常账号...", email: "" });

    try {
      const { progress_id } = await reLoginAccounts(abnormalTokens);

      // 轮询进度到完成
      await new Promise<void>((resolve, reject) => {
        const pollTimer = setInterval(async () => {
          try {
            const p = await fetchReLoginProgress(progress_id);
            if (p.done) {
              clearInterval(pollTimer);
              if (p.error) {
                reject(new Error(p.error));
                return;
              }
              setProgress((prev) => ({ ...prev, current: prev.total, message: "恢复流程已完成" }));
              setRefreshSummary(null);
              resolve();
            } else {
              // 实时更新进度
              const results = p.results ?? [];
              // 找到最新一条有错误的结果
              const lastErrorResult = [...results].reverse().find((r) => r.error);
              const emailHint = lastErrorResult
                ? `失败: ${lastErrorResult.token} ${lastErrorResult.error ?? ""}`
                : `已处理 ${p.processed}/${p.total}`;
              setProgress((prev) => ({
                ...prev,
                current: p.processed,
                email: emailHint,
                message: "正在尝试恢复异常账号...",
              }));

              // 实时更新统计卡片：基数 + 已处理的恢复结果
              let runningActive = baseActive;
              let runningAbnormal = baseAbnormal;
              let runningDisabled = baseDisabled;
              for (const r of results) {
                if (r.status === "成功") {
                  runningActive += 1;
                  runningAbnormal -= 1;
                } else if (r.status === "禁用") {
                  runningDisabled += 1;
                  runningAbnormal -= 1;
                }
                // "异常"或"跳过"：保持异常状态不变
              }
              setRefreshSummary({
                total: accountTotal || accounts.length,
                active: runningActive,
                limited: baseLimited,
                abnormal: runningAbnormal,
                disabled: runningDisabled,
                quota: summary.quota,
              });
            }
          } catch (err) {
            clearInterval(pollTimer);
            reject(err);
          }
        }, 300);
      });

      // 等待后台线程完成，再拉取最新数据
      await new Promise<void>((resolve) => setTimeout(resolve, 500));
      try {
        const freshData = await fetchAccounts({ limit: accountListLimit });
        setAccounts(freshData.items);
        setAccountTotal(typeof freshData.total === "number" ? freshData.total : freshData.items.length);
        setAccountStats(freshData.stats ?? null);
        setSelectedIds((prev) => prev.filter((id) => freshData.items.some((item) => item.access_token === id)));
      } catch { /* 静默失败 */ }

      setProgress({
        visible: true,
        current: total,
        total,
        message: "恢复完成",
        email: "",
      });
      setTimeout(() => setProgress({ visible: false, current: 0, total: 0, message: "", email: "" }), 800);

      toast.success(`恢复流程已全部完成`);
    } catch (error) {
      setProgress({ visible: false, current: 0, total: 0, message: "", email: "" });
      setRefreshSummary(null);
      const message = error instanceof Error ? error.message : "重新登录失败";
      toast.error(message);
    } finally {
      setIsRelogining(false);
    }
  };

  const openEditDialog = (account: Account) => {
    setEditingAccount(account);
    setEditStatus(account.status);
    setEditProxy(account.proxy ?? "");
  };

  const handleTestAccountProxy = async () => {
    const candidate = editProxy.trim();
    if (!candidate) {
      toast.error("请先填写代理地址");
      return;
    }
    setIsTestingProxy(true);
    try {
      const data = await testProxy(candidate);
      data.result.ok
        ? toast.success(`代理可用（${data.result.latency_ms} ms，HTTP ${data.result.status}）`)
        : toast.error(`代理不可用：${data.result.error ?? "未知错误"}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "测试代理失败");
    } finally {
      setIsTestingProxy(false);
    }
  };

  const handleUpdateAccount = async () => {
    if (!editingAccount) {
      return;
    }

    setIsUpdating(true);
    try {
      const data = await updateAccount(editingAccount.access_token, {
        status: editStatus,
        proxy: editProxy.trim(),
      });
      await refreshAccountPage();
      setEditingAccount(null);
      toast.success("账号信息已更新");
    } catch (error) {
      const message = error instanceof Error ? error.message : "更新账号失败";
      toast.error(message);
    } finally {
      setIsUpdating(false);
    }
  };

  const applySchedulingStats = (stats?: AccountStats | null) => {
    if (!stats) return;
    setAccountStats((prev) => ({
      ...(prev ?? {
        total: 0,
        active: 0,
        limited: 0,
        abnormal: 0,
        disabled: 0,
        total_quota: 0,
      }),
      ...stats,
    }));
  };

  const handleToggleScheduling = async (account: Account) => {
    const token = account.access_token;
    const nextEnabled = !isManualSchedulingEnabled(account);
    setSchedulingBusyTokens((prev) => new Set(prev).add(token));
    try {
      const data = await setAccountScheduling(token, nextEnabled);
      if (data.item) {
        setAccounts((prev) => prev.map((item) => (item.access_token === token ? { ...item, ...data.item } : item)));
      }
      applySchedulingStats(data.stats ?? null);
      toast.success(nextEnabled ? "已进入调度" : "已退出调度（隔离观察）");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "切换调度失败");
    } finally {
      setSchedulingBusyTokens((prev) => {
        const next = new Set(prev);
        next.delete(token);
        return next;
      });
    }
  };

  const handleBulkScheduling = async (enabled: boolean) => {
    const tokens = filteredAccounts.map((item) => item.access_token).filter(Boolean);
    if (tokens.length === 0) {
      toast.error("当前筛选没有可操作的账号");
      return;
    }
    if (tokens.length > 50) {
      toast.error("单次最多操作 50 个账号，请先缩小筛选");
      return;
    }
    setIsBulkScheduling(true);
    try {
      const data = await setAccountsSchedulingBulk(tokens, enabled);
      applySchedulingStats(data.stats ?? null);
      await refreshAccountPage();
      const updated = data.updated ?? 0;
      toast.success(enabled ? `已全部进调度（${updated}）` : `已全部出调度（${updated}）`);
      if (data.errors?.length) {
        toast.error(`${data.errors.length} 个账号操作失败`);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "批量切换调度失败");
    } finally {
      setIsBulkScheduling(false);
    }
  };

  const toggleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedIds((prev) => Array.from(new Set([...prev, ...currentRows.map((item) => item.access_token)])));
      return;
    }
    setSelectedIds((prev) => prev.filter((id) => !currentRows.some((row) => row.access_token === id)));
  };

  return (
    <>
      <section className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="space-y-1">
          <div className="text-xs font-semibold tracking-[0.18em] text-stone-500 uppercase">
            Account Pool
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">号池管理</h1>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => void handlePageRefresh()}
            disabled={isLoading || isRefreshing || isDeleting || isSyncingPanda}
          >
            <RefreshCw className={cn("size-4", isLoading ? "animate-spin" : "")} />
            刷新
          </Button>
          <Button
            variant="outline"
            className="h-10 rounded-xl border-emerald-200 bg-white/80 px-4 text-emerald-700 hover:bg-emerald-50"
            onClick={() => void handleBulkScheduling(true)}
            disabled={isLoading || isBulkScheduling || filteredAccounts.length === 0}
            title="将当前筛选结果全部进入调度"
          >
            {isBulkScheduling ? <LoaderCircle className="size-4 animate-spin" /> : <Play className="size-4" />}
            全部进调度
          </Button>
          <Button
            variant="outline"
            className="h-10 rounded-xl border-amber-200 bg-white/80 px-4 text-amber-700 hover:bg-amber-50"
            onClick={() => void handleBulkScheduling(false)}
            disabled={isLoading || isBulkScheduling || filteredAccounts.length === 0}
            title="将当前筛选结果全部退出调度（identity_isolated）"
          >
            {isBulkScheduling ? <LoaderCircle className="size-4 animate-spin" /> : <Pause className="size-4" />}
            全部出调度
          </Button>
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => void handleStartRefreshAll()}
            disabled={isLoading || isRefreshing || isDeleting || isSyncingPanda || refreshAllActive || accounts.length === 0}
          >
            {isStartingRefreshAll || refreshAllActive ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
            全量慢刷额度
          </Button>
          <div className="flex items-center gap-1 rounded-xl border border-stone-200 bg-white/80 px-2 py-1">
            <Input
              value={refreshAllConcurrency}
              onChange={(event) => setRefreshAllConcurrency(event.target.value)}
              className="h-8 w-16 rounded-lg border-stone-200 bg-white px-2 text-sm"
              placeholder="并发"
              disabled={refreshAllActive}
              title="慢刷并发"
            />
            <Input
              value={refreshAllBatchSize}
              onChange={(event) => setRefreshAllBatchSize(event.target.value)}
              className="h-8 w-16 rounded-lg border-stone-200 bg-white px-2 text-sm"
              placeholder="批量"
              disabled={refreshAllActive}
              title="批大小"
            />
            <Input
              value={refreshAllDelaySec}
              onChange={(event) => setRefreshAllDelaySec(event.target.value)}
              className="h-8 w-16 rounded-lg border-stone-200 bg-white px-2 text-sm"
              placeholder="间隔"
              disabled={refreshAllActive}
              title="账号间隔秒"
            />
          </div>
          {refreshAllActive ? (
            <Button
              variant="outline"
              className="h-10 rounded-xl border-rose-200 bg-white/80 px-4 text-rose-600 hover:bg-rose-50"
              onClick={() => void handleStopRefreshAll()}
              disabled={isStoppingRefreshAll}
            >
              {isStoppingRefreshAll ? <LoaderCircle className="size-4 animate-spin" /> : <CircleOff className="size-4" />}
              {isStoppingRefreshAll || refreshAllStatus?.state === "stopping" ? "停止中" : "停止慢刷"}
            </Button>
          ) : null}
          {isUploadSyncNode ? (
            <>
              <Button
                variant="outline"
                className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
                onClick={() => void handleSyncPanda()}
                disabled={isLoading || isRefreshing || isDeleting || isSyncingPanda}
              >
                {isSyncingPanda ? <LoaderCircle className="size-4 animate-spin" /> : <CloudUpload className="size-4" />}
                上传到 Panda
              </Button>
              <Button
                variant={pandaSyncSettings?.enabled ? "outline" : "default"}
                className={cn(
                  "h-10 rounded-xl px-4",
                  pandaSyncSettings?.enabled
                    ? "border-stone-200 bg-white/80 text-stone-700 hover:bg-white"
                    : "bg-stone-900 text-white hover:bg-stone-800",
                )}
                onClick={() => void handleTogglePandaSync()}
                disabled={isTogglingPandaSync}
              >
                {isTogglingPandaSync ? <LoaderCircle className="size-4 animate-spin" /> : <CloudUpload className="size-4" />}
                {pandaSyncSettings?.enabled ? "暂停自动上传" : "开启自动上传"}
              </Button>
            </>
          ) : null}
          <AccountImportDialog
            disabled={isLoading || isRefreshing || isDeleting || isSyncingPanda}
            onImported={(items) => {
              if (items?.length) {
                setAccounts(items.slice(0, accountListLimit));
              }
              void refreshAccountPage();
              setSelectedIds([]);
              setPage(1);
            }}
          />
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => downloadTokens(accounts)}
            disabled={accounts.length === 0 || isSyncingPanda}
          >
            <Download className="size-4" />
            导出已载入 Token
          </Button>
        </div>
      </section>

      {outlookAutoRecoveryStatus ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-amber-200/70 bg-amber-50/50 px-3 py-1.5 text-xs text-stone-600">
          <span className="font-medium text-stone-800">自动恢复</span>
          <Badge
            variant={
              outlookAutoRecoveryStatus.enabled
                ? outlookAutoRecoveryStatus.state === "paused"
                  ? "warning"
                  : "success"
                : "secondary"
            }
            className="h-5 rounded px-1.5 text-[10px]"
          >
            {formatOutlookAutoRecoveryState(outlookAutoRecoveryStatus.state)}
          </Badge>
          <span>
            下次{" "}
            {outlookAutoRecoveryStatus.enabled ? formatCountdown(outlookAutoRecoveryCountdown) : "—"}
          </span>
          <span>候选 {outlookAutoRecoveryStatus.candidate_count ?? 0}</span>
          <Button
            variant="ghost"
            className="ml-auto h-6 rounded-md px-2 text-[11px] text-amber-800 hover:bg-amber-100"
            onClick={() => void handleToggleOutlookAutoRecovery()}
            disabled={isTogglingOutlookAutoRecovery}
          >
            {isTogglingOutlookAutoRecovery ? (
              <LoaderCircle className="size-3 animate-spin" />
            ) : null}
            {outlookAutoRecoveryStatus.enabled ? "关闭" : "开启"}
          </Button>
        </div>
      ) : null}

      <div className="text-xs text-stone-500">
        慢刷参数上限：并发 {refreshAllMaxConcurrency}，批量 200；当前任务启动后参数锁定，修改只对下次启动生效；异常/限流账号只记录失败证据，不自动删除。
      </div>

      {/* 进度条 */}
      {progress.visible && (
        <div className="overflow-hidden rounded-2xl border border-stone-200 bg-white/90 shadow-sm">
          <div className="px-4 py-3">
            <div className="flex items-center justify-between text-sm">
              <span className="text-stone-600">
                {progress.message}
                {progress.email && <span className="ml-1 font-medium text-stone-700">{progress.email}</span>}
              </span>
              <span className="font-medium text-stone-700">
                {progress.current}/{progress.total}
              </span>
            </div>
            <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-stone-100">
              <div
                className="h-full rounded-full bg-gradient-to-r from-amber-400 to-orange-500 transition-all duration-300 ease-out"
                style={{ width: `${progress.total > 0 ? (progress.current / progress.total) * 100 : 0}%` }}
              />
            </div>
          </div>
        </div>
      )}

      {refreshAllStatus && refreshAllStatus.state !== "idle" ? (
        <div className="overflow-hidden rounded-2xl border border-stone-200 bg-white/90 shadow-sm">
          <div className="px-4 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <div className="flex min-w-0 items-center gap-2 text-stone-700">
                {refreshAllActive ? (
                  <LoaderCircle className="size-4 animate-spin text-amber-500" />
                ) : (
                  <CheckCircle2 className="size-4 text-emerald-500" />
                )}
                <span className="font-medium">慢速刷新全部额度</span>
                <Badge variant={refreshAllStatus.state === "paused" ? "warning" : refreshAllStatus.state === "completed" ? "success" : "secondary"}>
                  {formatRefreshAllState(refreshAllStatus.state)}
                </Badge>
              </div>
              <div className="text-stone-500">
                {refreshAllStatus.processed}/{refreshAllStatus.total}
              </div>
            </div>
            <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-stone-100">
              <div
                className="h-full rounded-full bg-gradient-to-r from-emerald-400 to-blue-500 transition-all duration-300 ease-out"
                style={{
                  width: `${refreshAllStatus.total > 0 ? Math.min(100, (refreshAllStatus.processed / refreshAllStatus.total) * 100) : 100}%`,
                }}
              />
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-stone-500">
              <span>刷新成功 {refreshAllStatus.refreshed}</span>
              <span>可调度 {refreshAllStatus.available}</span>
              <span>新增可调度 {refreshAllStatus.became_available}</span>
              <span>本次明确额度 {refreshAllStatus.quota_total ?? 0}</span>
              <span>真无限额 {refreshAllStatus.unlimited_quota ?? 0}</span>
              <span>未知额度 {refreshAllStatus.unknown_quota ?? 0}</span>
              <span>失败 {refreshAllStatus.failed}</span>
              <span>已删除 {refreshAllStatus.removed ?? 0}</span>
              <span>过期清理 {refreshAllStatus.expired_removed ?? 0}</span>
              <span>已上传 Panda {refreshAllStatus.synced_to_panda ?? 0}</span>
              <span>待上传 Panda {refreshAllStatus.queued_for_panda ?? 0}</span>
              <span>上传失败 {refreshAllStatus.sync_failed ?? 0}</span>
              <span>跳过 {refreshAllStatus.skipped}</span>
              <span>并发 {formatRefreshAllOption(refreshAllStatus, "concurrency")}</span>
              <span>批量 {formatRefreshAllOption(refreshAllStatus, "batch_size")}</span>
              <span>间隔 {formatRefreshAllOption(refreshAllStatus, "delay_between_accounts_sec")}s</span>
              {formatRefreshAllResource(refreshAllStatus) ? <span>{formatRefreshAllResource(refreshAllStatus)}</span> : null}
            </div>
            {refreshAllStatus.pause_reason ? (
              <div className="mt-2 text-xs text-amber-700">{refreshAllStatus.pause_reason}</div>
            ) : null}
          </div>
        </div>
      ) : null}

      <Dialog open={Boolean(editingAccount)} onOpenChange={(open) => (!open ? setEditingAccount(null) : null)}>
        <DialogContent showCloseButton={false} className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>编辑账户</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              手动修改账号状态和专属代理。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">状态</label>
              <Select value={editStatus} onValueChange={(value) => setEditStatus(value as AccountStatus)}>
                <SelectTrigger className="h-11 rounded-xl border-stone-200 bg-white">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {accountStatusOptions
                    .filter((option) => option.value !== "all")
                    .map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">账号代理</label>
              <div className="flex flex-col gap-2 sm:flex-row">
                <Input
                  value={editProxy}
                  onChange={(event) => setEditProxy(event.target.value)}
                  placeholder="留空走全局代理，例如 http://127.0.0.1:7890"
                  className="h-11 rounded-xl border-stone-200 bg-white"
                />
                <Button
                  variant="outline"
                  className="h-11 rounded-xl border-stone-200 bg-white px-4 text-stone-700 sm:w-24"
                  onClick={() => void handleTestAccountProxy()}
                  disabled={isTestingProxy}
                >
                  {isTestingProxy ? <LoaderCircle className="size-4 animate-spin" /> : <Link2 className="size-4" />}
                  测试
                </Button>
              </div>
            </div>
          </div>
          <DialogFooter className="pt-2">
            <Button
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={() => setEditingAccount(null)}
              disabled={isUpdating}
            >
              取消
            </Button>
            <Button
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => void handleUpdateAccount()}
              disabled={isUpdating}
            >
              {isUpdating ? <LoaderCircle className="size-4 animate-spin" /> : null}
              保存修改
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <section className="space-y-3">
        <div className="grid gap-2 md:gap-3 grid-cols-2 sm:grid-cols-4 xl:grid-cols-8">
          {metricCards.map((item) => {
            const Icon = item.icon;
            const value = (refreshSummary ?? summary)[item.key];
            return (
              <Card
                key={item.key}
                className="rounded-2xl border-white/80 bg-white/90 shadow-sm"
                title={
                  item.key === "quota" && accountStats
                    ? formatPoolQuotaDetail(accountStats)
                    : "title" in item
                      ? item.title
                      : undefined
                }
              >
                <CardContent className="p-3 xl:p-4">
                  <div className="mb-2 flex items-start justify-between xl:mb-4">
                    <span className="text-[11px] font-medium text-stone-400 xl:text-xs">{item.label}</span>
                    <Icon className="size-3.5 text-stone-400 xl:size-4" />
                  </div>
                  <div className={cn("text-xl font-semibold tracking-tight xl:text-[1.75rem]", item.color)}>
                    <span className={typeof value === "number" ? "" : "text-[1.1rem]"}>
                      {typeof value === "number" ? formatCompact(value) : value}
                    </span>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
        <AccountsActivityPanels refreshToken={activityRefreshToken} />
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="p-4">
            <div className="mb-3 text-sm font-medium text-stone-700">
              系统可用模型
              <span className="ml-1 text-stone-400">({availableModels.length})</span>
            </div>
            <div className="flex flex-wrap gap-2">
              {availableModels.length > 0 ? (
                availableModels.map((model) => (
                  <button
                    key={model.id}
                    type="button"
                    className="inline-flex cursor-pointer items-center rounded-full border border-stone-200 bg-white px-2.5 py-1 text-xs font-medium text-stone-700 transition hover:border-stone-300 hover:bg-stone-50"
                    onClick={() => {
                      void navigator.clipboard.writeText(model.id);
                      toast.success("模型名已复制");
                    }}
                    title={`点击复制 ${model.id}`}
                  >
                    <img
                      src="/openai.svg"
                      alt=""
                      aria-hidden="true"
                      className="mr-1.5 size-3.5 shrink-0"
                    />
                    {model.id}
                  </button>
                ))
              ) : isLoadingModels ? (
                <span className="text-sm text-stone-400">正在加载模型列表...</span>
              ) : (
                <span className="text-sm text-stone-400">当前暂无可用模型</span>
              )}
            </div>
          </CardContent>
        </Card>
      </section>

      <section className="space-y-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold tracking-tight">账户列表</h2>
            <Badge variant="secondary" className="rounded-lg bg-stone-200 px-2 py-0.5 text-stone-700">
              {filteredAccounts.length}/{accountTotal || accounts.length}
            </Badge>
          </div>

          <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
            <div className="relative min-w-[260px]">
              <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-stone-400" />
              <Input
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setPage(1);
                }}
                placeholder="搜索邮箱"
                className="h-10 rounded-xl border-stone-200 bg-white/85 pl-10"
              />
            </div>
            <Select
              value={typeFilter}
              onValueChange={(value) => {
                setTypeFilter(value);
                setPage(1);
              }}
            >
              <SelectTrigger className="h-10 w-full rounded-xl border-stone-200 bg-white/85 lg:w-[150px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {accountTypeOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={statusFilter}
              onValueChange={(value) => {
                setStatusFilter(value as AccountStatus | "all");
                setPage(1);
              }}
            >
              <SelectTrigger className="h-10 w-full rounded-xl border-stone-200 bg-white/85 lg:w-[150px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {accountStatusOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="inline-flex rounded-xl border border-stone-200 bg-white/85 p-0.5">
              <button
                type="button"
                className={cn(
                  "rounded-lg px-3 py-1.5 text-xs font-medium transition",
                  accountViewMode === "flat"
                    ? "bg-stone-900 text-white"
                    : "text-stone-600 hover:bg-stone-100",
                )}
                onClick={() => setAccountViewMode("flat")}
              >
                平铺
              </button>
              <button
                type="button"
                className={cn(
                  "rounded-lg px-3 py-1.5 text-xs font-medium transition",
                  accountViewMode === "grouped"
                    ? "bg-stone-900 text-white"
                    : "text-stone-600 hover:bg-stone-100",
                )}
                onClick={() => setAccountViewMode("grouped")}
              >
                按IP分组
              </button>
            </div>
          </div>
        </div>

        {isLoading && accounts.length === 0 ? (
          <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
            <CardContent className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
              <div className="rounded-xl bg-stone-100 p-3 text-stone-500">
                <LoaderCircle className="size-5 animate-spin" />
              </div>
              <div className="space-y-1">
                <p className="text-sm font-medium text-stone-700">正在加载账户摘要</p>
                <p className="text-sm text-stone-500">只加载前 {accountListLimit} 条账号，避免大号池卡住页面。</p>
              </div>
            </CardContent>
          </Card>
        ) : null}

        <Card
          className={cn(
            "overflow-hidden rounded-2xl border-white/80 bg-white/90 shadow-sm",
            isLoading && accounts.length === 0 ? "hidden" : "",
          )}
        >
          <CardContent className="space-y-0 p-0">
            <div className="flex flex-col gap-3 border-b border-stone-100 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex flex-wrap items-center gap-2 text-sm text-stone-500">
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-stone-500 hover:bg-stone-100"
                  onClick={() => void handleRefreshAccounts(refreshPageTokens)}
                  disabled={!canRefreshPageTokens || isRefreshing}
                >
                  {isRefreshing ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                  {selectedTokens.length > 0 ? "刷新选中账号信息和额度" : "刷新当前页账号信息和额度"}
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-amber-600 hover:bg-amber-50 hover:text-amber-700"
                  onClick={() => void handleReLogin(selectedTokens)}
                  disabled={selectedTokens.length === 0 || isRelogining}
                  title="尝试密码登录恢复账号"
                >
                  {isRelogining ? <LoaderCircle className="size-4 animate-spin" /> : <LogIn className="size-4" />}
                  尝试恢复异常账号
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-rose-500 hover:bg-rose-50 hover:text-rose-600"
                  onClick={() => void handleDeleteTokens(abnormalTokens)}
                  disabled={abnormalTokens.length === 0 || isDeleting}
                >
                  {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                  移除异常账号
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-rose-500 hover:bg-rose-50 hover:text-rose-600"
                  onClick={() => void handleDeleteTokens(selectedTokens)}
                  disabled={selectedTokens.length === 0 || isDeleting}
                >
                  {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                  删除所选
                </Button>
                {selectedIds.length > 0 ? (
                  <span className="rounded-lg bg-stone-100 px-2.5 py-1 text-xs font-medium text-stone-600">
                    已选择 {selectedIds.length} 项
                  </span>
                ) : null}
              </div>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full min-w-[1400px] text-left">
                <thead className="border-b border-stone-100 text-[11px] text-stone-400 uppercase tracking-[0.18em]">
                  <tr>
                    <th className="w-10 px-2 py-2">#</th>
                    <th className="w-10 px-2 py-2">
                      <Checkbox
                        checked={allCurrentSelected}
                        onCheckedChange={(checked) => toggleSelectAll(Boolean(checked))}
                      />
                    </th>
                    <th className="w-56 px-2 py-2">
                      <button type="button" className="hover:text-stone-700" onClick={(e) => toggleSort("email", e.shiftKey)}>
                        Token / 邮箱 {sortIndicator("email")}
                      </button>
                    </th>
                    <th className="w-24 px-2 py-2">
                      <button type="button" className="hover:text-stone-700" onClick={(e) => toggleSort("type", e.shiftKey)}>
                        类型 {sortIndicator("type")}
                      </button>
                    </th>
                    <th className="w-20 px-2 py-2">
                      <button type="button" className="hover:text-stone-700" onClick={(e) => toggleSort("status", e.shiftKey)}>
                        状态 {sortIndicator("status")}
                      </button>
                    </th>
                    <th className="w-20 px-2 py-2">
                      <button type="button" className="hover:text-stone-700" onClick={(e) => toggleSort("schedule", e.shiftKey)}>
                        调度 {sortIndicator("schedule")}
                      </button>
                    </th>
                    <th className="w-28 px-2 py-2">记录</th>
                    <th className="w-40 px-2 py-2">
                      <button type="button" className="hover:text-stone-700" onClick={(e) => toggleSort("proxy", e.shiftKey)}>
                        代理 / 出口 {sortIndicator("proxy")}
                      </button>
                    </th>
                    <th className="w-28 px-2 py-2">
                      <button type="button" className="hover:text-stone-700" onClick={(e) => toggleSort("created_at", e.shiftKey)}>
                        创建时间 {sortIndicator("created_at")}
                      </button>
                    </th>
                    <th className="w-20 px-2 py-2">
                      <button type="button" className="hover:text-stone-700" onClick={(e) => toggleSort("quota", e.shiftKey)}>
                        额度 {sortIndicator("quota")}
                      </button>
                    </th>
                    <th className="w-36 px-2 py-2">恢复时间</th>
                    <th className="w-14 px-2 py-2">在途</th>
                    <th className="w-20 px-2 py-2">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {tableBlocks.map((block) => {
                    if (block.kind === "group") {
                      const binding = nurtureBindings[block.key];
                      const presetId = binding?.preset_id || nurturePresets[0]?.id || "";
                      const weights = weightsForBinding(block.key, nurturePresets, nurtureBindings);
                      const saving = bindingSaveBusy.has(block.key);
                      return (
                        <tr
                          key={`group-${block.key}`}
                          className="border-b border-stone-200 bg-stone-50/90 text-sm text-stone-600"
                        >
                          <td colSpan={TABLE_COLUMN_COUNT} className="px-3 py-2">
                            <div className="flex flex-wrap items-center gap-3">
                              <div className="min-w-28">
                                <div className="text-xs font-semibold text-stone-800">{block.label}</div>
                                <div className="text-[10px] text-stone-500">
                                  {block.accounts.length} 账号 · {block.key.slice(0, 12)}
                                  {block.key.length > 12 ? "…" : ""}
                                </div>
                              </div>
                              <EgressDriftLights days={aggregateEgressDays(block.accounts)} />
                              <CfStatusLight days={aggregateCfDays(block.accounts)} />
                              <div className="flex items-end gap-2">
                                <div className="space-y-1">
                                  <div className="text-[10px] text-stone-500">养号日历</div>
                                  <Select
                                    value={presetId}
                                    disabled={saving || nurturePresets.length === 0}
                                    onValueChange={(value) => {
                                      void saveBindingPreset(block.key, value);
                                    }}
                                  >
                                    <SelectTrigger className="h-7 w-[132px] rounded-lg border-stone-200 bg-white text-xs">
                                      <SelectValue placeholder="选择预设" />
                                    </SelectTrigger>
                                    <SelectContent>
                                      {nurturePresets.map((preset) => (
                                        <SelectItem key={preset.id} value={preset.id}>
                                          {preset.label}
                                        </SelectItem>
                                      ))}
                                    </SelectContent>
                                  </Select>
                                </div>
                                <BindingActivityHeatmaps matrices={bindingUsageSlots[block.key] || {}} />
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="outline"
                                  className="h-7 rounded-lg px-2 text-xs"
                                  disabled={saving || nurturePresets.length === 0}
                                  onClick={() => {
                                    setWeightEditKey(block.key);
                                    setWeightEditPreset(presetId);
                                    setWeightEditMatrix(weights);
                                  }}
                                >
                                  编辑权重
                                </Button>
                              </div>
                            </div>
                          </td>
                        </tr>
                      );
                    }

                    const account = block.account;
                    const rowNo = block.rowNo;
                    const status = statusMeta[account.status];
                    const StatusIcon = status.icon;
                    const terminalOutlook = isOutlookRecoveryTerminal(account);
                    const recoverOutlook = isOutlookRecoveryCandidate(account);
                    const rowRefreshing = isRefreshing || refreshingTokens.has(account.access_token);

                    return (
                      <tr
                        key={account.access_token}
                        className="border-b border-stone-100/80 text-sm text-stone-600 transition-colors hover:bg-stone-50/70"
                      >
                        <td className="px-2 py-2 text-xs tabular-nums text-stone-400">{rowNo}</td>
                        <td className="px-2 py-2">
                          <Checkbox
                            checked={selectedIds.includes(account.access_token)}
                            onCheckedChange={(checked) => {
                              setSelectedIds((prev) =>
                                checked
                                  ? Array.from(new Set([...prev, account.access_token]))
                                  : prev.filter((item) => item !== account.access_token),
                              );
                            }}
                          />
                        </td>
                        <td className="px-2 py-2">
                          <div className="space-y-1">
                            <div className="flex items-center gap-2">
                              <span className="font-medium tracking-tight text-stone-700">
                                {maskToken(account.access_token)}
                              </span>
                              <button
                                type="button"
                                className="rounded-lg p-1 text-stone-400 transition hover:bg-stone-100 hover:text-stone-700"
                                onClick={() => {
                                  void navigator.clipboard.writeText(account.access_token);
                                  toast.success("token 已复制");
                                }}
                              >
                                <Copy className="size-4" />
                              </button>
                            </div>
                            <div className="truncate text-xs leading-5 text-stone-500">{account.email ?? "—"}</div>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex flex-col items-start gap-1">
                            <Badge variant="secondary" className="rounded-md bg-stone-100 text-stone-700">
                              {displayAccountType(account)}
                            </Badge>
                            <Badge variant="outline" className="rounded-md border-stone-200 text-stone-600">
                              {displayAccountSource(account)}
                            </Badge>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <Badge
                            variant={status.badge}
                            className="inline-flex items-center gap-1 rounded-md px-2 py-1"
                          >
                            <StatusIcon className="size-3.5" />
                            {account.status}
                          </Badge>
                        </td>
                        <td className="px-4 py-3">
                          {(() => {
                            const inSchedule = isManualSchedulingEnabled(account);
                            const busy = schedulingBusyTokens.has(account.access_token);
                            return (
                              <button
                                type="button"
                                className={cn(
                                  "inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium transition",
                                  inSchedule
                                    ? "bg-emerald-50 text-emerald-700 hover:bg-emerald-100"
                                    : "bg-amber-50 text-amber-700 hover:bg-amber-100",
                                )}
                                onClick={() => void handleToggleScheduling(account)}
                                disabled={busy || isBulkScheduling}
                                title={
                                  inSchedule
                                    ? "当前在调度池；点击退出调度（隔离观察）"
                                    : "当前隔离观察；点击进入调度"
                                }
                              >
                                {busy ? (
                                  <LoaderCircle className="size-3.5 animate-spin" />
                                ) : inSchedule ? (
                                  <Play className="size-3.5" />
                                ) : (
                                  <Pause className="size-3.5" />
                                )}
                                {inSchedule ? "调度中" : "已隔离"}
                              </button>
                            );
                          })()}
                        </td>
                        <td className="px-4 py-3">
                          <AccountUsageHeatstrip
                            days={
                              usageByEmail[String(account.email || "").trim().toLowerCase()] ||
                              usageDates.map((date) => ({ date, images: 0, dialogues: 0 }))
                            }
                          />
                        </td>
                        <td className="px-4 py-3">
                          {(() => {
                            const proxy = proxyDisplay(account);
                            const pandaError = formatPandaInlineError(
                              account.panda_probe_last_error
                                || account.panda_verify_last_error
                                || account.last_quota_refresh_error
                                || account.last_refresh_error,
                            );
                            return (
                              <div
                                className="max-w-48 space-y-0.5 text-xs leading-5"
                                title={[proxy.endpoint, proxy.detail, pandaError ? `Panda: ${pandaError}` : ""]
                                  .filter(Boolean)
                                  .join("\n")}
                              >
                                <div className="flex min-w-0 items-baseline gap-1.5">
                                  <span className="truncate font-medium text-stone-700">{proxy.endpoint}</span>
                                  {proxy.provider ? (
                                    <span className="shrink-0 text-stone-400">{proxy.provider}</span>
                                  ) : null}
                                </div>
                                <EgressDriftLights days={egressDaysForAccount(account)} />
                                <CfStatusLight days={cfDaysForAccount(account)} />
                                {pandaError ? (
                                  <div className="truncate text-[11px] text-rose-500">{pandaError}</div>
                                ) : null}
                              </div>
                            );
                          })()}
                        </td>
                        <td className="px-4 py-3 text-xs leading-5 text-stone-500">
                          {(() => {
                            const raw = account.created_at;
                            if (!raw) return "—";
                            try {
                              const d = new Date(raw + "Z");
                              if (isNaN(d.getTime())) return String(raw).slice(0, 10);
                              return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
                            } catch { return String(raw).slice(0, 10); }
                          })()}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1.5">
                            <span
                              className="shrink-0 text-[10px] text-stone-400"
                              title={
                                account.last_quota_refresh_at
                                  ? `额度核对：${account.last_quota_refresh_at}`
                                  : "尚未远程核对额度"
                              }
                            >
                              {formatQuotaRefreshAge(account)}
                            </span>
                            <Badge
                              variant={accountQuotaBadgeVariant(account)}
                              className="rounded-md"
                              title={formatAccountQuotaHint(account)}
                            >
                              {formatAccountQuotaValue(account)}
                            </Badge>
                          </div>
                        </td>
                        <td className="px-4 py-3 text-xs leading-5 text-stone-500">
                          {(() => {
                            const restore = formatRestoreAt(account.restore_at);
                            return (
                              <div
                                className="space-y-0.5"
                                title="上游额度恢复时刻；懒刷新会在恢复后按账号错峰再拉 limits，避免齐刷"
                              >
                                <div className="flex items-center gap-1">
                                  {restore.relative ? (
                                    <div className="font-medium text-stone-700">{restore.relative}</div>
                                  ) : null}
                                  <ScheduleCountdownIcons account={account} showText={false} />
                                </div>
                                <div>{restore.absolute}</div>
                              </div>
                            );
                          })()}
                        </td>
                        <td className="px-4 py-3">
                          {(() => {
                            const inflight = account.image_inflight ?? 0;
                            return (
                              <span
                                className={
                                  inflight > 0
                                    ? "font-semibold text-amber-600"
                                    : "text-stone-400"
                                }
                                title={
                                  inflight > 0
                                    ? "当前正在生成的图片数。号池空闲时此值持续 > 0，说明并发槽位泄漏、该账号已被静默排除出调度"
                                    : "当前无在途生图任务"
                                }
                              >
                                {inflight}
                              </span>
                            );
                          })()}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1 text-stone-400">
                            <ScheduleCountdownIcons account={account} showLazy={false} />
                            <button
                              type="button"
                              className="rounded-lg p-2 transition hover:bg-sky-50 hover:text-sky-700"
                              title="立即对该账号发起一条真实文本对话（同步执行，非仅入队）"
                              onClick={() => {
                                const email = String(account.email || "").trim();
                                if (!email) {
                                  toast.error("该账号无邮箱，无法定向对话");
                                  return;
                                }
                                const toastId = toast.loading(`正在对话：${email}…`);
                                void (async () => {
                                  try {
                                    const status = await fetchNurtureStatus().catch(() => null);
                                    const result = await processNurtureOne({
                                      email,
                                      source: "accounts_ui",
                                    });
                                    const ms = Number(result.latency_ms || 0);
                                    const queueDepth = Number(
                                      (status as { queue?: { depth?: number } } | null)?.queue?.depth ?? 0,
                                    );
                                    toast.success(
                                      `真实对话完成 ${email} · ${(ms / 1000).toFixed(1)}s · 输出 ${result.chars_out ?? 0} 字` +
                                        (queueDepth > 0 ? ` · 队列仍积压 ${queueDepth}` : ""),
                                      { id: toastId, duration: 6000 },
                                    );
                                    const usage = await fetchAccountsUsageRecent(6).catch(() => null);
                                    if (usage?.by_email) {
                                      setUsageByEmail(usage.by_email);
                                      if (usage.dates?.length) setUsageDates(usage.dates);
                                    }
                                  } catch (err) {
                                    const msg = humanizeUpstreamError(
                                      err instanceof Error ? err.message : String(err),
                                    );
                                    toast.error(
                                      `${msg} 队列：运维 →「养号」。`,
                                      { id: toastId, duration: 8000 },
                                    );
                                  }
                                })();
                              }}
                            >
                              <MessageSquare className="size-4" />
                            </button>
                            <button
                              type="button"
                              className="rounded-lg p-2 transition hover:bg-stone-100 hover:text-stone-700"
                              onClick={() => openEditDialog(account)}
                              disabled={isUpdating}
                            >
                              <Pencil className="size-4" />
                            </button>
                            <button
                              type="button"
                              className={cn(
                                "rounded-lg p-2 transition",
                                recoverOutlook
                                  ? "text-amber-600 hover:bg-amber-50 hover:text-amber-700"
                                  : terminalOutlook
                                    ? "text-stone-400 hover:bg-stone-100 hover:text-stone-600"
                                    : "hover:bg-stone-100 hover:text-stone-700",
                              )}
                              onClick={() => void handleAccountRefreshAction(account)}
                              disabled={rowRefreshing}
                              title={
                                terminalOutlook
                                  ? "OpenAI 账号已删除或停用，已停止自动恢复"
                                  : recoverOutlook
                                    ? "恢复异常 Outlook 账号"
                                    : "刷新账号信息和额度"
                              }
                              aria-label={
                                terminalOutlook
                                  ? "OpenAI 账号已停用"
                                  : recoverOutlook
                                    ? "恢复异常 Outlook 账号"
                                    : "刷新账号信息和额度"
                              }
                            >
                              <RefreshCw className={cn("size-4", rowRefreshing ? "animate-spin" : "")} />
                            </button>
                            <button
                              type="button"
                              className="rounded-lg p-2 transition hover:bg-rose-50 hover:text-rose-500"
                              onClick={() => void handleDeleteTokens([account.access_token])}
                              disabled={isDeleting}
                            >
                              <Trash2 className="size-4" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              {!isLoading && currentRows.length === 0 ? (
                <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
                  <div className="rounded-xl bg-stone-100 p-3 text-stone-500">
                    <Search className="size-5" />
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm font-medium text-stone-700">没有匹配的账户</p>
                    <p className="text-sm text-stone-500">调整筛选条件或搜索关键字后重试。</p>
                  </div>
                </div>
              ) : null}
            </div>

            <div className="border-t border-stone-100 px-4 py-4">
              <div className="flex items-center justify-center gap-3 overflow-x-auto whitespace-nowrap">
                <div className="shrink-0 text-sm text-stone-500">
                已载入 {accounts.length}/{accountTotal || accounts.length} 条，当前筛选显示第 {filteredAccounts.length === 0 ? 0 : startIndex + 1} -{" "}
                {Math.min(startIndex + Number(pageSize), filteredAccounts.length)} 条，共{" "}
                {filteredAccounts.length} 条
                </div>

                <span className="shrink-0 text-sm leading-none text-stone-500">
                  {safePage} / {pageCount} 页
                </span>
                <Select
                  value={pageSize}
                  onValueChange={(value) => {
                    setPageSize(value);
                    setPage(1);
                  }}
                >
                  <SelectTrigger className="h-10 w-[108px] shrink-0 rounded-lg border-stone-200 bg-white text-sm leading-none">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="10">10 / 页</SelectItem>
                    <SelectItem value="20">20 / 页</SelectItem>
                    <SelectItem value="50">50 / 页</SelectItem>
                    <SelectItem value="100">100 / 页</SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  variant="outline"
                  size="icon"
                  className="size-10 shrink-0 rounded-lg border-stone-200 bg-white"
                  disabled={safePage <= 1}
                  onClick={() => setPage((prev) => Math.max(1, prev - 1))}
                >
                  <ChevronLeft className="size-4" />
                </Button>
                {paginationItems.map((item, index) =>
                  item === "..." ? (
                    <span key={`ellipsis-${index}`} className="px-1 text-sm text-stone-400">
                      ...
                    </span>
                  ) : (
                    <Button
                      key={item}
                      variant={item === safePage ? "default" : "outline"}
                      className={cn(
                        "h-10 min-w-10 shrink-0 rounded-lg px-3",
                        item === safePage
                          ? "bg-stone-950 text-white hover:bg-stone-800"
                          : "border-stone-200 bg-white text-stone-700",
                      )}
                      onClick={() => setPage(item)}
                    >
                      {item}
                    </Button>
                  ),
                )}
                <Button
                  variant="outline"
                  size="icon"
                  className="size-10 shrink-0 rounded-lg border-stone-200 bg-white"
                  disabled={safePage >= pageCount}
                  onClick={() => setPage((prev) => Math.min(pageCount, prev + 1))}
                >
                  <ChevronRight className="size-4" />
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </section>

      <Dialog
        open={Boolean(weightEditKey)}
        onOpenChange={(open) => {
          if (!open) setWeightEditKey(null);
        }}
      >
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>养号时段权重</DialogTitle>
            <DialogDescription>点击格子循环调整 0 → 0.25 → 0.5 → 0.75 → 1（Asia/Singapore）</DialogDescription>
          </DialogHeader>
          <div className="py-2">
            <BindingSgHeatmap
              weights={weightEditMatrix}
              editable
              onChange={(next) => setWeightEditMatrix(next)}
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setWeightEditKey(null)}>
              取消
            </Button>
            <Button
              type="button"
              disabled={!weightEditKey}
              onClick={() => {
                if (!weightEditKey) return;
                void saveBindingPreset(
                  weightEditKey,
                  weightEditPreset || nurturePresets[0]?.id || "default",
                  weightEditMatrix,
                ).then(() => setWeightEditKey(null));
              }}
            >
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export default function AccountsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return <AccountsPageContent />;
}
