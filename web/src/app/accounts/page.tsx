"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ComponentProps } from "react";
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
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  UserRound,
} from "lucide-react";
import { toast } from "sonner";

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
  fetchAccountActivityDaily,
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
  testProxy,
  updateAccountMaintenanceLoop,
  updateOutlookAutoRecovery,
  updatePandaSyncSettings,
  updateAccount,
  type Account,
  type AccountActivityDailyResponse,
  type AccountMaintenanceLoopStatus,
  type AccountRefreshAllStatus,
  type AccountRefreshResponse,
  type AccountStatus,
  type Model,
  type OutlookAutoRecoveryStatus,
  type PandaAccountSyncResponse,
  type PandaSyncPublicSettings,
  type RefreshProgressResponse,
} from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

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
  { key: "limited", label: "限流账户", color: "text-orange-500", icon: CircleAlert },
  { key: "abnormal", label: "异常账户", color: "text-rose-500", icon: CircleOff },
  { key: "disabled", label: "禁用账户", color: "text-stone-500", icon: Ban },
  { key: "quota", label: "剩余额度", color: "text-blue-500", icon: RefreshCw },
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

function isUnlimitedImageQuotaAccount(account: Account) {
  return String(account.type || "").trim().toLowerCase() === "pro" || String(account.type || "").trim().toLowerCase() === "prolite";
}

function imageQuotaUnknown(account: Account) {
  return Boolean(account.image_quota_unknown);
}

function isUnknownImageQuotaAccount(account: Account) {
  return imageQuotaUnknown(account) && !isUnlimitedImageQuotaAccount(account);
}

function formatCompact(value: number) {
  if (value >= 1000) {
    return `${(value / 1000).toFixed(1)}k`;
  }
  return String(value);
}

function formatBytes(value?: number | null) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return "等待采集";
  }
  // 展示优先 MB / GB / TB，小于 1MB 仍用 KB
  const units = ["KB", "MB", "GB", "TB"] as const;
  let size = value / 1024;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const digits = size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[unitIndex]}`;
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
  const scope = String(account.proxy_scope ?? "").trim();
  const hash = String(account.proxy_egress_hash ?? "").trim();
  return {
    endpoint,
    detail: [provider, scope].filter(Boolean).join(" · ") || (rawProxy ? "账号级代理" : "运行时默认"),
    hash: hash ? hash.slice(0, 12) : "",
  };
}

function formatQuota(account: Account) {
  if (isUnlimitedImageQuotaAccount(account)) {
    return "∞";
  }
  if (isUnknownImageQuotaAccount(account)) {
    return "未知";
  }
  return String(Math.max(0, account.quota));
}

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

function buildLinePath(values: number[], maxValue: number, width: number, height: number) {
  if (values.length === 0) return "";
  const denominator = Math.max(1, values.length - 1);
  const max = Math.max(1, maxValue);
  const points = values.map((value, index) => ({
    x: (index / denominator) * width,
    y: height - (Math.max(0, value) / max) * height,
  }));
  if (points.length === 1) {
    return `M ${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`;
  }
  // Catmull-Rom → cubic Bezier，平滑曲线
  let path = `M ${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`;
  for (let i = 0; i < points.length - 1; i += 1) {
    const p0 = points[Math.max(0, i - 1)];
    const p1 = points[i];
    const p2 = points[i + 1];
    const p3 = points[Math.min(points.length - 1, i + 2)];
    const cp1x = p1.x + (p2.x - p0.x) / 6;
    const cp1y = p1.y + (p2.y - p0.y) / 6;
    const cp2x = p2.x - (p3.x - p1.x) / 6;
    const cp2y = p2.y - (p3.y - p1.y) / 6;
    path += ` C ${cp1x.toFixed(2)} ${cp1y.toFixed(2)}, ${cp2x.toFixed(2)} ${cp2y.toFixed(2)}, ${p2.x.toFixed(2)} ${p2.y.toFixed(2)}`;
  }
  return path;
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
    return "事故恢复隔离，需重登验证";
  }
  if (lower.includes("account_deactivated")) {
    return "OpenAI 账号已删除或停用";
  }
  if (lower.includes("/backend-api/") && lower.includes("403")) {
    return "Web 验证 403，需重登";
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
  const [accountActivity, setAccountActivity] = useState<AccountActivityDailyResponse | null>(null);
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

  const loadAccounts = async (silent = false) => {
    if (!silent) {
      setIsLoading(true);
    }
    try {
      const data = await fetchAccounts({ limit: accountListLimit });
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
  };

  const refreshAccountPage = async () => {
    await loadAccounts(true);
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

  useEffect(() => {
    if (didLoadRef.current) {
      return;
    }
    didLoadRef.current = true;
    void loadAccounts();
    void loadModels();
    void loadRefreshSettings();
    void loadRefreshAllStatus();
    void loadMaintenanceStatus();
    void loadOutlookAutoRecoveryStatus();
    void loadPandaSyncSettings();
    void loadAccountActivity();

    // 清理进度条定时器
    return () => {
      if (progressRef.current) clearInterval(progressRef.current);
      if (refreshAllPollRef.current) clearInterval(refreshAllPollRef.current);
      if (maintenancePollRef.current) clearInterval(maintenancePollRef.current);
      if (outlookAutoRecoveryPollRef.current) clearInterval(outlookAutoRecoveryPollRef.current);
      if (outlookAutoRecoveryCountdownRef.current) clearInterval(outlookAutoRecoveryCountdownRef.current);
    };
  }, []);

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

  const loadAccountActivity = async () => {
    try {
      const data = await fetchAccountActivityDaily(14);
      setAccountActivity(data);
      return data;
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
    return accounts.filter((account) => {
      const searchMatched =
        normalizedQuery.length === 0 || (account.email ?? "").toLowerCase().includes(normalizedQuery);
      const typeMatched = typeFilter === "all" || displayAccountType(account) === typeFilter;
      const statusMatched = statusFilter === "all" || account.status === statusFilter;
      return searchMatched && typeMatched && statusMatched;
    });
  }, [accounts, query, statusFilter, typeFilter]);

  const pageCount = Math.max(1, Math.ceil(filteredAccounts.length / Number(pageSize)));
  const safePage = Math.min(page, pageCount);
  const startIndex = (safePage - 1) * Number(pageSize);
  const currentRows = filteredAccounts.slice(startIndex, startIndex + Number(pageSize));
  const allCurrentSelected =
    currentRows.length > 0 && currentRows.every((row) => selectedIds.includes(row.access_token));

  const summary = useMemo(() => {
    if (accountStats) {
      const unlimitedQuotaCount = typeof accountStats.unlimited_quota_count === "number" ? accountStats.unlimited_quota_count : 0;
      const unknownQuotaCount = typeof accountStats.unknown_quota_count === "number" ? accountStats.unknown_quota_count : 0;
      return {
        total: accountStats.total,
        active: accountStats.active,
        limited: accountStats.limited,
        abnormal: accountStats.abnormal,
        disabled: accountStats.disabled,
        quota: unlimitedQuotaCount > 0 ? "∞" : unknownQuotaCount > 0 ? "未知" : formatCompact(accountStats.total_quota),
      };
    }
    const total = accounts.length;
    const active = accounts.filter((item) => item.status === "正常").length;
    const limited = accounts.filter((item) => item.status === "限流").length;
    const abnormal = accounts.filter((item) => item.status === "异常").length;
    const disabled = accounts.filter((item) => item.status === "禁用").length;
    const quota = formatQuotaSummary(accounts);

    return { total, active, limited, abnormal, disabled, quota };
  }, [accountStats, accounts]);

  const pandaUploadCards = useMemo(() => {
    const receiverMode = accountActivity?.sync_label === "接收";
    const countState = (field: keyof Account, value: string) =>
      accounts.filter((account) => String(account[field] || "").trim().toLowerCase() === value).length;
    const stat = (key: keyof AccountStats, fallback: number) => {
      const value = accountStats?.[key];
      return typeof value === "number" ? value : fallback;
    };
    const localReady = countState("panda_sync_state", "ready");
    const localSynced = countState("panda_sync_state", "synced");
    const localIncoming = countState("panda_receive_state", "incoming");
    const localVerified = accounts.filter((account) => {
      const state = String(account.panda_receive_state || "").trim().toLowerCase();
      return state === "verified" || state === "verified_ready" || state === "local_verified";
    }).length;
    const localRejected = countState("panda_receive_state", "rejected");

    return [
      {
        label: receiverMode ? "接收可用" : "可上传",
        value: stat("panda_upload_eligible_count", localReady),
        icon: CloudUpload,
        color: "text-sky-600",
      },
      {
        label: receiverMode ? "接收队列" : "上传队列",
        value: stat("panda_upload_queue_count", localReady),
        icon: RefreshCw,
        color: "text-blue-600",
      },
      {
        label: receiverMode ? "具备未接收" : "具备但未传",
        value: stat("panda_upload_unsynced_eligible_count", localReady),
        icon: CircleAlert,
        color: "text-amber-600",
      },
      {
        label: receiverMode ? "上传留存" : "已传本地留存",
        value: stat("panda_upload_retained_count", localSynced),
        icon: CheckCircle2,
        color: "text-emerald-600",
      },
      {
        label: "接收待验证",
        value: stat("panda_upload_remote_pending_count", localIncoming),
        icon: LoaderCircle,
        color: "text-orange-600",
      },
      {
        label: "接收已验证",
        value: stat("panda_upload_remote_verified_count", localVerified),
        icon: CheckCircle2,
        color: "text-teal-600",
      },
      {
        label: "接收拒绝",
        value: stat("panda_upload_remote_rejected_count", localRejected),
        icon: CircleOff,
        color: "text-rose-600",
      },
      {
        label: "ready 阻断",
        value: stat("panda_upload_blocked_count", 0),
        icon: Ban,
        color: "text-stone-600",
      },
    ];
  }, [accountActivity?.sync_label, accountStats, accounts]);

  const activityChart = useMemo(() => {
    const items = accountActivity?.items ?? [];
    const syncKey = accountActivity?.sync_label === "接收" ? "received" : "uploaded";
    const registered = items.map((item) => item.registered);
    const synced = items.map((item) => syncKey === "received" ? item.received : item.uploaded);
    const deleted = items.map((item) => item.deleted);
    const maxValue = Math.max(1, ...registered, ...synced, ...deleted);
    return {
      items,
      syncLabel: accountActivity?.sync_label ?? "上传",
      maxValue,
      registeredPath: buildLinePath(registered, maxValue, 640, 120),
      syncedPath: buildLinePath(synced, maxValue, 640, 120),
      deletedPath: buildLinePath(deleted, maxValue, 640, 120),
    };
  }, [accountActivity]);

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
      await loadAccountActivity();
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
      await loadAccountActivity();
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
      toast.success(status.enabled ? "Outlook 自动恢复已开启" : "Outlook 自动恢复已关闭");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新 Outlook 自动恢复失败");
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
      toast.error(error instanceof Error ? error.message : "Outlook 账号恢复失败");
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
      await loadAccountActivity();
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
    return new Promise<void>((resolve, reject) => {
      const timer = setInterval(async () => {
        try {
          const p = await fetchRefreshProgress(progressId);
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

  const toggleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedIds((prev) => Array.from(new Set([...prev, ...currentRows.map((item) => item.access_token)])));
      return;
    }
    setSelectedIds((prev) => prev.filter((id) => !currentRows.some((row) => row.access_token === id)));
  };

  return (
    <>
      <section className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
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
            onClick={() => void loadAccounts()}
            disabled={isLoading || isRefreshing || isDeleting || isSyncingPanda}
          >
            <RefreshCw className={cn("size-4", isLoading ? "animate-spin" : "")} />
            刷新
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
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => void handleToggleMaintenance()}
            disabled={isTogglingMaintenance}
          >
            {isTogglingMaintenance ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
            {maintenanceStatus?.enabled ? "关闭保活" : "开启保活"}
          </Button>
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
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => {
              if (activityChart.syncLabel === "接收") {
                void refreshAccountPage();
                void loadAccountActivity();
                return;
              }
              void handleSyncPanda();
            }}
            disabled={isLoading || isRefreshing || isDeleting || isSyncingPanda}
          >
            {isSyncingPanda ? <LoaderCircle className="size-4 animate-spin" /> : <CloudUpload className="size-4" />}
            {activityChart.syncLabel === "接收" ? "刷新接收状态" : "上传到 Panda"}
          </Button>
          {activityChart.syncLabel !== "接收" ? (
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

      {maintenanceStatus ? (
        <div className="overflow-hidden rounded-2xl border border-stone-200 bg-white/90 shadow-sm">
          <div className="px-4 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <div className="flex min-w-0 items-center gap-2 text-stone-700">
                {maintenanceStatus.state === "running_batch" ? (
                  <LoaderCircle className="size-4 animate-spin text-amber-500" />
                ) : (
                  <RefreshCw className="size-4 text-stone-500" />
                )}
                <span className="font-medium">panda 轻量保活</span>
                <Badge
                  variant={
                    maintenanceStatus.enabled
                      ? maintenanceStatus.state === "resource_paused" || maintenanceStatus.state === "traffic_paused"
                        ? "warning"
                        : "success"
                      : "secondary"
                  }
                >
                  {formatMaintenanceState(maintenanceStatus.state)}
                </Badge>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <div className="text-stone-500">下次 {formatMaintenanceTime(maintenanceStatus.next_run_at)}</div>
                <Button
                  variant={maintenanceStatus.enabled ? "outline" : "default"}
                  className={cn(
                    "h-8 rounded-xl px-3 text-xs",
                    maintenanceStatus.enabled
                      ? "border-stone-200 bg-white/80 text-stone-700 hover:bg-white"
                      : "bg-stone-900 text-white hover:bg-stone-800",
                  )}
                  onClick={() => void handleToggleMaintenance()}
                  disabled={isTogglingMaintenance}
                >
                  {isTogglingMaintenance ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                  {maintenanceStatus.enabled ? "关闭保活" : "开启保活"}
                </Button>
                <Button
                  variant="outline"
                  className="h-8 rounded-xl border-emerald-200 bg-emerald-50 px-3 text-xs text-emerald-700 hover:bg-emerald-100"
                  onClick={() => void handleApplyMaintenanceSafeSettings()}
                  disabled={isTogglingMaintenance}
                >
                  应用新版参数
                </Button>
              </div>
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-stone-500">
              <span>每批 {maintenanceStatus.settings?.batch_limit ?? 10}</span>
              <span>并发 {maintenanceStatus.settings?.concurrency ?? 1}</span>
              <span>批量 {maintenanceStatus.settings?.batch_size ?? 10}</span>
              <span>冷却 {maintenanceStatus.settings?.cooldown_sec ?? 30}s</span>
              <span>硬暂停 {maintenanceStatus.settings?.resource_pause_enabled ? "开启" : "关闭"}</span>
              <span>暂停阈值 {maintenanceStatus.settings?.pause_when_image_inflight ?? 0}</span>
              <span>慢速阈值 {maintenanceStatus.settings?.slow_when_image_inflight ?? 0}</span>
              <span>累计批次 {maintenanceStatus.totals?.batches ?? 0}</span>
              <span>处理 {maintenanceStatus.totals?.processed ?? 0}</span>
              <span>删除 {maintenanceStatus.totals?.removed ?? 0}</span>
              {maintenanceStatus.current_batch?.status ? (
                <span>
                  当前 {maintenanceStatus.current_batch.status.processed}/{maintenanceStatus.current_batch.status.total}
                </span>
              ) : null}
              {formatMaintenanceResource(maintenanceStatus) ? <span>{formatMaintenanceResource(maintenanceStatus)}</span> : null}
            </div>
            {maintenanceStatus.pause_reason ? (
              <div className="mt-2 text-xs text-amber-700">{maintenanceStatus.pause_reason}</div>
            ) : null}
          </div>
        </div>
      ) : null}

      {outlookAutoRecoveryStatus ? (
        <div className="overflow-hidden rounded-2xl border border-amber-200/80 bg-amber-50/40 shadow-sm">
          <div className="px-4 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <div className="flex min-w-0 items-center gap-2 text-stone-700">
                {outlookAutoRecoveryStatus.state === "recovering" || outlookAutoRecoveryStatus.state === "scanning" ? (
                  <LoaderCircle className="size-4 animate-spin text-amber-600" />
                ) : (
                  <RefreshCw className="size-4 text-amber-600" />
                )}
                <span className="font-medium">Outlook 自动恢复</span>
                <Badge
                  variant={
                    outlookAutoRecoveryStatus.enabled
                      ? outlookAutoRecoveryStatus.state === "paused"
                        ? "warning"
                        : "success"
                      : "secondary"
                  }
                >
                  {formatOutlookAutoRecoveryState(outlookAutoRecoveryStatus.state)}
                </Badge>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <div className="text-stone-600">
                  下次检测{" "}
                  <span className="font-medium tabular-nums text-stone-800">
                    {outlookAutoRecoveryStatus.enabled
                      ? formatCountdown(outlookAutoRecoveryCountdown)
                      : "—"}
                  </span>
                </div>
                <Button
                  variant={outlookAutoRecoveryStatus.enabled ? "outline" : "default"}
                  className={cn(
                    "h-8 rounded-xl px-3 text-xs",
                    outlookAutoRecoveryStatus.enabled
                      ? "border-amber-200 bg-white/80 text-amber-800 hover:bg-amber-50"
                      : "bg-amber-700 text-white hover:bg-amber-800",
                  )}
                  onClick={() => void handleToggleOutlookAutoRecovery()}
                  disabled={isTogglingOutlookAutoRecovery}
                >
                  {isTogglingOutlookAutoRecovery ? (
                    <LoaderCircle className="size-3.5 animate-spin" />
                  ) : (
                    <RefreshCw className="size-3.5" />
                  )}
                  {outlookAutoRecoveryStatus.enabled ? "关闭自动恢复" : "开启自动恢复"}
                </Button>
              </div>
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-stone-500">
              <span>间隔 {Math.round(Number(outlookAutoRecoveryStatus.settings?.interval_sec ?? 1800) / 60)} 分钟</span>
              <span>每轮最多 {outlookAutoRecoveryStatus.settings?.max_per_cycle ?? 1} 个</span>
              <span>候选 {outlookAutoRecoveryStatus.candidate_count ?? 0}</span>
              <span>终态隔离 {outlookAutoRecoveryStatus.terminal_count ?? 0}</span>
              <span>累计成功 {outlookAutoRecoveryStatus.totals?.succeeded ?? 0}</span>
              <span>失败 {outlookAutoRecoveryStatus.totals?.failed ?? 0}</span>
              <span>跳过忙 {outlookAutoRecoveryStatus.totals?.skipped_busy ?? 0}</span>
              {outlookAutoRecoveryStatus.current?.email ? (
                <span>
                  当前 {String(outlookAutoRecoveryStatus.current.email)} ·{" "}
                  {String(outlookAutoRecoveryStatus.current.stage || outlookAutoRecoveryStatus.current.message || "")}
                </span>
              ) : null}
              {outlookAutoRecoveryStatus.last_result?.email ? (
                <span>
                  最近 {String(outlookAutoRecoveryStatus.last_result.email)}{" "}
                  {outlookAutoRecoveryStatus.last_result.ok
                    ? `成功${typeof outlookAutoRecoveryStatus.last_result.quota === "number" ? ` · 额度 ${outlookAutoRecoveryStatus.last_result.quota}` : ""}`
                    : outlookAutoRecoveryStatus.last_result.skipped
                      ? "已跳过"
                      : "失败"}
                </span>
              ) : null}
            </div>
            {outlookAutoRecoveryStatus.pause_reason ? (
              <div className="mt-2 text-xs text-amber-700">{outlookAutoRecoveryStatus.pause_reason}</div>
            ) : null}
            {outlookAutoRecoveryStatus.last_result?.error ? (
              <div className="mt-2 max-w-full truncate text-xs text-rose-600">
                {String(outlookAutoRecoveryStatus.last_result.error)}
              </div>
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
        <div className="grid gap-3 md:grid-cols-3 xl:grid-cols-6">
          {metricCards.map((item) => {
            const Icon = item.icon;
            const value = (refreshSummary ?? summary)[item.key];
            return (
              <Card key={item.key} className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
                <CardContent className="p-4">
                  <div className="mb-4 flex items-start justify-between">
                    <span className="text-xs font-medium text-stone-400">{item.label}</span>
                    <Icon className="size-4 text-stone-400" />
                  </div>
                  <div className={cn("text-[1.75rem] font-semibold tracking-tight", item.color)}>
                    <span className={typeof value === "number" ? "" : "text-[1.1rem]"}>
                      {typeof value === "number" ? formatCompact(value) : value}
                    </span>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="text-sm font-medium text-stone-700">
                Panda {activityChart.syncLabel === "接收" ? "接收状态" : "上传状态"}
              </div>
              <Badge variant="outline" className="rounded-md border-stone-200 text-stone-500">
                {activityChart.syncLabel === "接收" ? "接收节点" : pandaSyncSettings?.enabled ? "自动上传开启" : "自动上传暂停"}
              </Badge>
            </div>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
              {pandaUploadCards.map((item) => {
                const Icon = item.icon;
                return (
                  <div key={item.label} className="rounded-xl border border-stone-100 bg-stone-50/70 px-3 py-2">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <span className="text-[11px] font-medium text-stone-400">{item.label}</span>
                      <Icon className="size-3.5 text-stone-400" />
                    </div>
                    <div className={cn("text-xl font-semibold tracking-tight", item.color)}>
                      {formatCompact(item.value)}
                    </div>
                  </div>
                );
              })}
            </div>
            {lastPandaSyncResult ? (
              <div className="mt-3 rounded-xl border border-stone-100 bg-white px-3 py-2 text-xs text-stone-500">
                最近上传：{formatPandaSyncDetails(lastPandaSyncResult)}
              </div>
            ) : null}
          </CardContent>
        </Card>
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <div className="text-sm font-medium text-stone-700">账号流水</div>
              <div className="flex flex-wrap items-center gap-3 text-xs text-stone-500">
                <span className="inline-flex items-center gap-1"><span className="size-2 rounded-full bg-emerald-500" />注册/入库</span>
                <span className="inline-flex items-center gap-1"><span className="size-2 rounded-full bg-blue-500" />{activityChart.syncLabel}</span>
                <span className="inline-flex items-center gap-1"><span className="size-2 rounded-full bg-rose-500" />删除</span>
              </div>
            </div>
            <div className="overflow-x-auto">
              <svg viewBox="0 0 700 180" className="h-[200px] min-w-[700px] w-full">
                {/* Y axis */}
                <line x1="48" y1="12" x2="48" y2="140" stroke="#a8a29e" strokeWidth="1" />
                {/* X axis */}
                <line x1="48" y1="140" x2="688" y2="140" stroke="#a8a29e" strokeWidth="1" />
                {[0, 1, 2, 3, 4].map((line) => {
                  const y = 12 + line * 32;
                  const value = Math.round(activityChart.maxValue * (1 - line / 4));
                  return (
                    <g key={line}>
                      <line x1="48" x2="688" y1={y} y2={y} stroke="#e7e5e4" strokeWidth="1" />
                      <text x="42" y={y + 3} textAnchor="end" className="fill-stone-400 text-[10px]">
                        {value}
                      </text>
                    </g>
                  );
                })}
                <g transform="translate(48 12)">
                  <path d={activityChart.registeredPath} fill="none" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
                  <path d={activityChart.syncedPath} fill="none" stroke="#3b82f6" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
                  <path d={activityChart.deletedPath} fill="none" stroke="#f43f5e" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
                </g>
                {activityChart.items.map((item, index) => {
                  const chartWidth = 640;
                  const x = 48 + (activityChart.items.length <= 1 ? 0 : (index / (activityChart.items.length - 1)) * chartWidth);
                  return (
                    <text key={item.date} x={x} y="158" textAnchor={index === 0 ? "start" : index === activityChart.items.length - 1 ? "end" : "middle"} className="fill-stone-500 text-[10px]">
                      {item.date.slice(5)}
                    </text>
                  );
                })}
                <text x="368" y="176" textAnchor="middle" className="fill-stone-400 text-[10px]">日期</text>
                <text x="14" y="76" textAnchor="middle" className="fill-stone-400 text-[10px]" transform="rotate(-90 14 76)">数量</text>
              </svg>
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-stone-500">
              <span>峰值 {activityChart.maxValue}</span>
              <span>窗口 {accountActivity?.days ?? 14} 天</span>
            </div>
          </CardContent>
        </Card>
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
              <table className="w-full min-w-[1100px] text-left">
                <thead className="border-b border-stone-100 text-[11px] text-stone-400 uppercase tracking-[0.18em]">
                  <tr>
                    <th className="w-12 px-4 py-3">
                      <Checkbox
                        checked={allCurrentSelected}
                        onCheckedChange={(checked) => toggleSelectAll(Boolean(checked))}
                      />
                    </th>
                    <th className="w-56 px-4 py-3">Token / 邮箱</th>
                    <th className="w-28 px-4 py-3">类型 / 来源</th>
                    <th className="w-24 px-4 py-3">状态</th>
                    <th className="w-48 px-4 py-3">代理 / 出口</th>
                    <th className="w-32 px-4 py-3">累计流量</th>
                    <th className="w-32 px-4 py-3">创建时间</th>
                    <th className="w-24 px-4 py-3">额度</th>
                    <th className="w-40 px-4 py-3">恢复时间</th>
                    <th className="w-18 px-4 py-3">在途</th>
                    <th className="w-24 px-4 py-3">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {currentRows.map((account) => {
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
                        <td className="px-4 py-3">
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
                        <td className="px-4 py-3">
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
                            const proxy = proxyDisplay(account);
                            const pandaError = formatPandaInlineError(
                              account.panda_probe_last_error || account.panda_verify_last_error,
                            );
                            return (
                              <div
                                className="max-w-48 space-y-0.5 text-xs leading-5"
                                title={[
                                  proxy.endpoint,
                                  proxy.detail,
                                  proxy.hash ? `出口哈希 ${proxy.hash}` : "",
                                  pandaError ? `Panda: ${pandaError}` : "",
                                ]
                                  .filter(Boolean)
                                  .join("\n")}
                              >
                                <div className="truncate font-medium text-stone-700">{proxy.endpoint}</div>
                                <div className="truncate text-stone-400">{proxy.detail}</div>
                                {proxy.hash ? <div className="font-mono text-[10px] text-stone-400">#{proxy.hash}</div> : null}
                                {pandaError ? (
                                  <div className="truncate text-[11px] text-rose-500">{pandaError}</div>
                                ) : null}
                              </div>
                            );
                          })()}
                        </td>
                        <td className="px-4 py-3">
                          <div
                            className="space-y-0.5 text-xs leading-5"
                            title={
                              typeof account.traffic_total_bytes === "number"
                                ? `上传 ${formatBytes(account.traffic_uploaded_bytes)}\n下载 ${formatBytes(account.traffic_downloaded_bytes)}\n更新时间 ${account.traffic_updated_at || "—"}`
                                : "历史账号尚未启用应用层流量统计"
                            }
                          >
                            <div className="font-medium text-stone-700">{formatBytes(account.traffic_total_bytes)}</div>
                            <div className="text-[10px] text-stone-400">
                              {typeof account.traffic_total_bytes === "number" ? "应用层已跟踪" : "等待采集"}
                            </div>
                          </div>
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
                          <Badge variant="info" className="rounded-md">
                            {formatQuota(account)}
                          </Badge>
                        </td>
                        <td className="px-4 py-3 text-xs leading-5 text-stone-500">
                          {(() => {
                            const restore = formatRestoreAt(account.restore_at);
                            return (
                              <div className="space-y-0.5">
                                {restore.relative ? <div className="font-medium text-stone-700">{restore.relative}</div> : null}
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
