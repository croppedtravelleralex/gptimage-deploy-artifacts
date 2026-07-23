"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { DateRangeControls, InteractiveLineChart } from "@/components/charts/InteractiveLineChart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  enqueueNurture,
  fetchHumanlikeDashboard,
  fetchNurtureStatus,
  fetchRiskCalendar,
  fetchRiskChecks,
  runOpsAgent,
  runRiskCheck,
  setNurtureEnabled,
} from "@/lib/api";
import { humanizeRiskFinding } from "@/lib/chat-format";
import { useAuthGuard } from "@/lib/use-auth-guard";

type TabId = "rca" | "nurture" | "risk";

const SCOPE_LABELS: Record<string, string> = {
  health_breakdown: "健康归因",
  receive_sticky_soft: "接收/粘性/软带",
  scheduler_workload_discipline: "调度纪律",
  lazy_quota_freshness: "懒刷新额度",
  admission_queue_ewma_burst: "准入/队列/突发",
  streak_cooldown_cohort: "失败冷却/群组",
  nurture_persist_llm_ops: "养号/LLM日志",
  maturity_gaps_proxy: "成熟度与代理缺口",
};

const GAP_LABELS: Record<string, string> = {
  maturity_stage_mostly_empty: "成熟度多为空",
  workload_shadow: "工作负载仍为影子模式",
  proxy_nodes_table_missing: "缺代理节点表",
  maturity_auto_fsm_missing: "缺成熟度自动状态机",
};

const BUCKET_LABELS: Record<string, string> = {
  schedulable: "生图候选",
  excluded_by_receive_state: "接收态排除",
  excluded_by_quota: "额度排除",
  excluded_by_quota_freshness: "额度新鲜度排除",
  excluded_by_status: "状态排除",
  excluded_by_failure_evidence: "失败证据排除",
  excluded_by_soft_cap: "软熔断排除",
  excluded_by_cooldown: "冷却排除",
  excluded_by_interval: "间隔未到",
  excluded_by_cohort: "群组暂停",
  excluded_by_inflight: "在途占用",
  excluded_by_preflight: "预检失败",
  excluded_by_identity: "身份门禁",
  excluded_by_dup_binding: "重复绑定排除",
  excluded_by_backoff: "退避排除",
  excluded_by_sticky_fault: "粘性故障排除",
};

const RECEIVE_STATE_LABELS: Record<string, string> = {
  verified_ready: "已验证可调度",
  verified: "已验证",
  local_verified: "本地已验证",
  identity_isolated: "身份隔离",
  incoming: "入库观察",
  sticky_fault: "粘性故障",
  soft_capped: "软熔断",
  unknown: "未知",
};

const POLL_LABELS: Record<string, string> = {
  wall: "总墙钟",
  conversation_get: "会话拉取",
  tasks: "任务轮询",
};

const LEVEL_LABELS: Record<string, string> = {
  ok: "正常",
  low: "偏低",
  medium: "中等",
  high: "偏高",
  critical: "严重",
};

const OUTCOME_LABELS: Record<string, string> = {
  ok: "完成",
  error: "失败",
  reject: "拒绝",
  skipped: "跳过",
};

function levelClass(level: string) {
  const v = level.toLowerCase();
  if (v === "ok") return "bg-emerald-100 text-emerald-800";
  if (v === "low") return "bg-sky-100 text-sky-800";
  if (v === "medium") return "bg-amber-100 text-amber-900";
  if (v === "high" || v === "critical") return "bg-rose-100 text-rose-800";
  return "bg-stone-100 text-stone-700";
}

function formatScope(scope: unknown): string {
  const items = Array.isArray(scope) ? scope.map(String) : String(scope || "").split(/[·,]/).map((s) => s.trim()).filter(Boolean);
  if (!items.length) return "全量检查";
  return items.map((item) => SCOPE_LABELS[item] || item).join(" · ");
}

function formatGaps(gaps: unknown): string {
  if (!Array.isArray(gaps) || gaps.length === 0) return "无";
  return gaps.map((g) => GAP_LABELS[String(g)] || String(g)).join(" · ");
}

function formatBucketReason(reason: string): string {
  if (BUCKET_LABELS[reason]) return BUCKET_LABELS[reason];
  const key = reason.replace(/^excluded_by_/, "");
  const fallback: Record<string, string> = {
    quota_freshness: "额度新鲜度排除",
    failure_evidence: "失败证据排除",
    dup_binding: "重复绑定排除",
    backoff: "退避排除",
    sticky_fault: "粘性故障排除",
  };
  return fallback[key] || (reason.startsWith("excluded_by_") ? `排除: ${key}` : reason);
}

function formatReceiveState(key: string): string {
  return RECEIVE_STATE_LABELS[key] || key;
}

function boolZh(v: unknown): string {
  if (v === true || v === "true") return "是";
  if (v === false || v === "false") return "否";
  return String(v ?? "—");
}

function formatCheckTime(raw: unknown): string {
  const text = String(raw || "").trim();
  if (!text) return "—";
  const d = new Date(text);
  if (Number.isNaN(d.getTime())) return text;
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function formatModelsLine(models: unknown, outcome: unknown): string {
  const parts: string[] = [];
  const m = models && typeof models === "object" ? (models as Record<string, unknown>) : {};
  const ds = m.deepseek && typeof m.deepseek === "object" ? (m.deepseek as Record<string, unknown>) : null;
  if (ds) {
    const name = String(ds.model || "DeepSeek");
    const ms = Number(ds.latency_ms || 0);
    parts.push(ms > 0 ? `DeepSeek ${name} ${(ms / 1000).toFixed(1)}s` : `DeepSeek ${name}`);
  }
  const gpt = m.gpt_l0 && typeof m.gpt_l0 === "object" ? (m.gpt_l0 as Record<string, unknown>) : null;
  if (gpt) {
    const ms = Number(gpt.latency_ms || 0);
    parts.push(ms > 0 ? `GPT总结 ${(ms / 1000).toFixed(1)}s` : "GPT总结");
  }
  const outcomeKey = String(outcome || "").toLowerCase();
  parts.push(`结果${OUTCOME_LABELS[outcomeKey] || String(outcome || "-")}`);
  return parts.join(" · ");
}

function formatSummary(raw: unknown): string {
  const text = String(raw || "").trim();
  if (!text) return "";
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.toUpperCase().startsWith("RISK="))
    .join("\n");
}

function seriesDay(point: Record<string, unknown>): string {
  const ts = String(point.ts || point.t || "");
  if (ts.length >= 10) return ts.slice(0, 10);
  return "";
}

function seriesLabel(point: Record<string, unknown>): string {
  const ts = String(point.ts || point.t || "");
  if (/^\d{2}-\d{2} \d{2}:\d{2}/.test(ts)) return ts.slice(0, 11);
  if (ts.length >= 16 && /^\d{4}-\d{2}-\d{2}/.test(ts)) {
    return ts.slice(5, 16).replace("T", " ");
  }
  if (ts.length >= 10) return ts.slice(5, 10);
  return ts || "—";
}

function SmoothSeriesPanel({ series }: { series: Array<Record<string, unknown>> }) {
  const allDays = useMemo(() => {
    const days = Array.from(new Set(series.map(seriesDay).filter(Boolean))).sort();
    return days;
  }, [series]);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  useEffect(() => {
    if (!allDays.length) return;
    if (!from || !to) {
      const startDay = allDays[Math.max(0, allDays.length - 7)] || allDays[0];
      setFrom(startDay);
      setTo(allDays[allDays.length - 1]);
    }
  }, [allDays, from, to]);

  const filtered = useMemo(() => {
    return series.filter((p) => {
      const d = seriesDay(p);
      if (!d) return false;
      if (from && d < from) return false;
      if (to && d > to) return false;
      return true;
    });
  }, [series, from, to]);

  if (filtered.length < 2) {
    return <p className="text-sm text-stone-500">尚无半小时采样点（巡检开启或手动跑一次后会出现）</p>;
  }

  const keys = [
    { key: "schedulable", label: "生图候选", color: "#166534" },
    { key: "soft_capped_count", label: "软熔断", color: "#b45309" },
    { key: "admission_inflight", label: "同步占用", color: "#0369a1" },
    { key: "llm_ops_error", label: "业务LLM失败（≠巡检文案助手）", color: "#be123c" },
    { key: "image_queue_depth", label: "生图队列", color: "#57534e" },
  ] as const;

  return (
    <div className="space-y-3">
      <DateRangeControls
        from={from}
        to={to}
        min={allDays[0]}
        max={allDays[allDays.length - 1]}
        onChange={(a, b) => {
          setFrom(a);
          setTo(b);
        }}
        presets={[
          { label: "3天", days: 3 },
          { label: "7天", days: 7 },
          { label: "14天", days: 14 },
        ]}
      />
      <InteractiveLineChart
        dates={filtered.map((p) => seriesLabel(p))}
        series={keys.map((k) => ({
          key: k.key,
          label: k.label,
          color: k.color,
          values: filtered.map((p) => Number(p[k.key] || 0)),
        }))}
        yLabel="数量"
        xLabel="时间"
        sharedScale={false}
      />
    </div>
  );
}

function AutomationTrendPanel({
  rows,
  weights,
}: {
  rows: Array<Record<string, unknown>>;
  weights: Record<string, number>;
}) {
  const days = rows.map((r) => String(r.date || "")).filter(Boolean);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  useEffect(() => {
    if (!days.length) return;
    if (!from || !to) {
      setFrom(days[Math.max(0, days.length - 14)] || days[0]);
      setTo(days[days.length - 1]);
    }
  }, [days, from, to]);

  const filtered = useMemo(
    () =>
      rows.filter((r) => {
        const d = String(r.date || "");
        return d && (!from || d >= from) && (!to || d <= to);
      }),
    [rows, from, to],
  );

  const series = [
    { key: "composite", label: "综合风险", color: "#0f172a" },
    { key: "detection", label: "官方检测压力", color: "#be123c" },
    { key: "soft_risk", label: "软熔断风控", color: "#b45309" },
    { key: "fail_risk", label: "失败连击", color: "#7c3aed" },
    { key: "cohort_risk", label: "群组风控", color: "#0369a1" },
    { key: "edge_risk", label: "边缘/限流", color: "#57534e" },
  ] as const;

  return (
    <div className="space-y-3">
      <DateRangeControls
        from={from}
        to={to}
        min={days[0]}
        max={days[days.length - 1]}
        onChange={(a, b) => {
          setFrom(a);
          setTo(b);
        }}
      />
      <InteractiveLineChart
        dates={filtered.map((r) => String(r.date))}
        series={series.map((s) => ({
          key: s.key,
          label: s.label,
          color: s.color,
          values: filtered.map((r) => {
            const v = r[s.key];
            return v == null || v === "" ? 0 : Number(v);
          }),
        }))}
        yLabel="百分比"
        xLabel="日期"
        sharedScale
        fixedMax={100}
      />
      <p className="text-[11px] text-stone-400">
        权重（分越高风险越大）：检测 {(weights.detection ?? 0.3) * 100}% · 软熔断 {(weights.soft_risk ?? 0.25) * 100}% · 连击{" "}
        {(weights.fail_risk ?? 0.2) * 100}% · 群组 {(weights.cohort_risk ?? 0.15) * 100}% · 边缘 {(weights.edge_risk ?? 0.1) * 100}%
      </p>
    </div>
  );
}

type CalendarCell = {
  date: string;
  intensity: number;
  color_level?: number;
  risk_level?: string;
  score?: number;
  detail?: Record<string, number>;
};

function RiskHeatmap({ cells }: { cells: CalendarCell[] }) {
  const [hover, setHover] = useState<CalendarCell | null>(null);
  const weeks: Array<CalendarCell[]> = [];
  for (let i = 0; i < cells.length; i += 7) {
    weeks.push(cells.slice(i, i + 7));
  }

  const colorFor = (c: CalendarCell) => {
    const level = Math.max(0, Math.min(4, Number(c.color_level ?? c.intensity ?? 0)));
    const risk = String(c.risk_level || "ok").toLowerCase();
    const palette: Record<string, string[]> = {
      ok: ["#e7e5e4", "#bbf7d0", "#86efac", "#4ade80", "#16a34a"],
      low: ["#e7e5e4", "#bae6fd", "#7dd3fc", "#38bdf8", "#0284c7"],
      medium: ["#e7e5e4", "#fde68a", "#fbbf24", "#f59e0b", "#d97706"],
      high: ["#e7e5e4", "#fdba74", "#fb923c", "#f97316", "#ea580c"],
      critical: ["#e7e5e4", "#fecaca", "#f87171", "#ef4444", "#b91c1c"],
    };
    const colors = palette[risk] || palette.medium;
    return colors[level] || colors[0];
  };

  return (
    <div className="relative space-y-2">
      <div className="flex flex-wrap gap-1">
        {weeks.map((week, wi) => (
          <div key={wi} className="flex flex-col gap-1">
            {week.map((c) => (
              <div
                key={c.date}
                className="size-2.5 rounded-[2px] ring-1 ring-black/5"
                style={{ background: colorFor(c) }}
                onMouseEnter={() => setHover(c)}
                onMouseLeave={() => setHover(null)}
              />
            ))}
          </div>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-stone-500">
        <span>浅=信息少/风险低</span>
        <span>深=信息多/风险高</span>
        <span className="inline-flex items-center gap-1">
          <span className="size-2 rounded-[2px] bg-emerald-400" />
          正常
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="size-2 rounded-[2px] bg-amber-400" />
          中等
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="size-2 rounded-[2px] bg-rose-500" />
          偏高
        </span>
      </div>
      {hover ? (
        <div className="rounded-xl border border-stone-200 bg-white px-3 py-2 text-xs text-stone-600 shadow-sm">
          <div className="font-medium text-stone-800">
            {hover.date} · {LEVEL_LABELS[String(hover.risk_level || "").toLowerCase()] || "正常"}
          </div>
          <div className="mt-1">
            评分 {hover.score ?? 0} · 信息强度 {hover.intensity ?? 0} · 色阶 {hover.color_level ?? hover.intensity ?? 0}
          </div>
          <div className="mt-1 text-stone-500">
            负载 {Number(hover.detail?.load || 0).toFixed(1)} · 失败 {Number(hover.detail?.fail || 0).toFixed(1)} · LLM失败{" "}
            {Number(hover.detail?.llm_err || 0).toFixed(1)} · 采样 {Number(hover.detail?.points || 0)}
          </div>
        </div>
      ) : (
        <p className="text-xs text-stone-400">鼠标悬停格子可查看当日详情</p>
      )}
    </div>
  );
}

function RiskTab() {
  const [dash, setDash] = useState<Record<string, unknown> | null>(null);
  const [calendar, setCalendar] = useState<CalendarCell[]>([]);
  const [checks, setChecks] = useState<Array<Record<string, unknown>>>([]);
  const [auditStatus, setAuditStatus] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [d, cal, ch] = await Promise.all([
        fetchHumanlikeDashboard(),
        fetchRiskCalendar(84),
        fetchRiskChecks(24),
      ]);
      setDash(d as unknown as Record<string, unknown>);
      setCalendar((cal.cells || []) as CalendarCell[]);
      setChecks(ch.items || []);
      setAuditStatus((ch.status || null) as Record<string, unknown> | null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载风控看板失败");
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 30000);
    return () => window.clearInterval(id);
  }, [load]);

  const snap = (dash?.snapshot || {}) as Record<string, unknown>;
  const kpi = (dash?.kpi || {}) as Record<string, unknown>;
  const derived = (snap.derived || {}) as Record<string, unknown>;
  const admission = (snap.admission || {}) as Record<string, unknown>;
  const pipeline = (snap.pipeline || {}) as Record<string, unknown>;
  const cohort = (snap.cohort || {}) as Record<string, unknown>;
  const llm = (snap.llm_ops || {}) as Record<string, unknown>;
  const nurture = (snap.nurture || {}) as Record<string, unknown>;
  const burst = (snap.burst || {}) as Record<string, unknown>;
  const breakdown = (snap.breakdown || {}) as Record<string, unknown>;
  const buckets = (breakdown.buckets || {}) as Record<string, number>;
  // 与号池「可调度」一致：人工进调度且状态正常（不用生图 breakdown 候选数）
  const schedulableKpi = kpi.schedulable;
  const admissionKpi =
    admission.admission_inflight != null && admission.admission_max != null
      ? `${admission.admission_inflight}/${admission.admission_max}`
      : kpi.admission;
  const trafficTop = (
    Array.isArray(derived.traffic_top) ? derived.traffic_top : []
  ) as Array<{ email_mask?: string; traffic_total_bytes?: number }>;
  const maxTraffic = Math.max(
    1,
    ...trafficTop.map((r) => Number(r.traffic_total_bytes || 0)),
  );
  const series = (dash?.series || []) as Array<Record<string, unknown>>;
  const automationDaily = (dash?.automation_daily || []) as Array<Record<string, unknown>>;
  const automationWeights = (dash?.automation_weights || {}) as Record<string, number>;
  const receiveState = (derived.receive_state || {}) as Record<string, number>;
  const streakHist = (Array.isArray(derived.streak_hist) ? derived.streak_hist : []) as Array<{
    bucket: string;
    n: number;
  }>;
  const cohortRows = (Array.isArray(cohort.cohorts) ? cohort.cohorts : []) as Array<Record<string, unknown>>;
  const pollEx = (admission.poll_exhausted || {}) as Record<string, number>;
  const llmHourly = (Array.isArray(llm.hourly) ? llm.hourly : []) as Array<Record<string, unknown>>;
  const maturitySet = Number(derived.maturity_set || 0);
  const maturityEmpty = Number(derived.maturity_empty || 0);
  const maturityTotal = Math.max(1, maturitySet + maturityEmpty);

  const bucketRows = useMemo(
    () =>
      Object.entries(buckets)
        .filter(([k]) => k.startsWith("excluded_") || k === "schedulable")
        .map(([reason, count]) => ({ reason, count: Number(count || 0) })),
    [buckets],
  );

  const runNow = async () => {
    setBusy(true);
    try {
      const report = await runRiskCheck();
      const lv = String(report.risk_level || "ok").toLowerCase();
      toast.success(`巡检完成 · ${LEVEL_LABELS[lv] || lv}`);
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "巡检失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-stone-500">
          只读看板 · 半小时主动巡检（DeepSeek→GPT）默认关闭，见配置 risk_audit.enabled
        </p>
        <div className="flex gap-2">
          <Button variant="ghost" className="h-8 rounded-lg px-2" onClick={() => void load()}>
            <RefreshCw className="size-4" />
          </Button>
          <Button className="rounded-xl bg-stone-950 text-white" disabled={busy} onClick={() => void runNow()}>
            {busy ? <LoaderCircle className="size-4 animate-spin" /> : null}
            立即巡检
          </Button>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {[
          ["生图候选", dash ? schedulableKpi : "…"],
          ["隔离观察", dash ? kpi.identity_isolated : "…"],
          ["软熔断", dash ? kpi.soft_capped : "…"],
          ["同步准入", dash ? admissionKpi : "…"],
          ["工作负载", dash ? (kpi.workload_mode === "shadow" ? "影子" : kpi.workload_mode === "live" ? "正式" : kpi.workload_mode) : "…"],
        ].map(([label, value]) => (
          <Card key={String(label)} className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
            <CardContent className="p-4">
              <div className="text-xs text-stone-500">{String(label)}</div>
              <div className="mt-1 text-xl font-semibold text-stone-900">{String(value ?? "-")}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-3 p-4">
            <div className="text-sm font-medium text-stone-700">拟人 / 指纹 / 软熔断摘要</div>
            <div className="grid grid-cols-2 gap-2 text-sm">
              <div className="rounded-xl border border-stone-100 bg-stone-50/70 px-3 py-2">
                <div className="text-[11px] text-stone-400">软熔断</div>
                <div className="text-xl font-semibold text-amber-700">{String(derived.soft_capped_count ?? 0)}</div>
              </div>
              <div className="rounded-xl border border-stone-100 bg-stone-50/70 px-3 py-2">
                <div className="text-[11px] text-stone-400">指纹完备</div>
                <div className="text-xl font-semibold text-stone-800">
                  {String(derived.fp_complete ?? 0)}/{String(derived.fp_total ?? 0)}
                </div>
              </div>
              <div className="rounded-xl border border-stone-100 bg-stone-50/70 px-3 py-2">
                <div className="text-[11px] text-stone-400">成熟度有阶段</div>
                <div className="text-xl font-semibold text-emerald-700">{String(derived.maturity_set ?? 0)}</div>
              </div>
              <div className="rounded-xl border border-stone-100 bg-stone-50/70 px-3 py-2">
                <div className="text-[11px] text-stone-400">成熟度空</div>
                <div className="text-xl font-semibold text-stone-500">{String(derived.maturity_empty ?? 0)}</div>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-3 p-4">
            <div className="text-sm font-medium text-stone-700">流量 Top</div>
            {!trafficTop.length || trafficTop.every((r) => Number(r.traffic_total_bytes || 0) <= 0) ? (
              <p className="text-sm text-stone-500">暂无应用层流量样本</p>
            ) : (
              <div className="space-y-2">
                {trafficTop.slice(0, 8).map((row) => {
                  const bytes = Number(row.traffic_total_bytes || 0);
                  const label = String(row.email_mask || "—");
                  return (
                    <div key={label} className="space-y-1">
                      <div className="flex items-center justify-between gap-2 text-xs text-stone-600">
                        <span className="truncate">{label}</span>
                        <span className="shrink-0 font-medium text-stone-800">
                          {bytes >= 1024 * 1024
                            ? `${(bytes / (1024 * 1024)).toFixed(1)} MB`
                            : bytes >= 1024
                              ? `${(bytes / 1024).toFixed(1)} KB`
                              : `${bytes} B`}
                        </span>
                      </div>
                      <div className="h-1.5 overflow-hidden rounded-full bg-stone-100">
                        <div
                          className="h-full rounded-full bg-stone-700"
                          style={{ width: `${Math.max(4, (bytes / maxTraffic) * 100)}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-3 p-5">
          <div className="text-sm font-medium text-stone-800">平滑多序列（半小时点）</div>
          <SmoothSeriesPanel series={series} />
        </CardContent>
      </Card>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-3 p-5">
          <div className="text-sm font-medium text-stone-800">自动化风险趋势（按日百分比）</div>
          <p className="text-xs text-stone-500">被官方检测 / 被风控相关压力：越高越危险</p>
          <AutomationTrendPanel rows={automationDaily} weights={automationWeights} />
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-3 p-5">
            <div className="text-sm font-medium text-stone-800">风险日历</div>
            <RiskHeatmap cells={calendar} />
          </CardContent>
        </Card>
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-3 p-5">
            <div className="text-sm font-medium text-stone-800">为什么不能派单</div>
            <div className="space-y-2">
              {bucketRows.map((row) => (
                <div key={row.reason} className="flex items-center justify-between text-sm">
                  <span className="text-stone-600">{formatBucketReason(row.reason)}</span>
                  <Badge className="rounded-md">{row.count}</Badge>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-2 p-5 text-sm">
            <div className="font-medium text-stone-800">同步准入 / 预计等待</div>
            <div>
              {String(admission.admission_inflight ?? "-")}/{String(admission.admission_max ?? "-")}
            </div>
            <div className="text-stone-500">
              预计等待 {String(admission.eta_secs ?? "-")} 秒 · 忙拒 {String(admission.busy_429_count ?? 0)}
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-stone-100">
              <div
                className="h-full bg-sky-700"
                style={{
                  width: `${Math.min(
                    100,
                    (Number(admission.admission_inflight || 0) / Math.max(1, Number(admission.admission_max || 1))) * 100,
                  )}%`,
                }}
              />
            </div>
          </CardContent>
        </Card>
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-2 p-5 text-sm">
            <div className="font-medium text-stone-800">队列 / 耗时均值 / 突发</div>
            <div>深度 {String(pipeline.image_queue_depth ?? "-")}</div>
            <div className="text-stone-500">
              成功耗时均值 {Number(pipeline.ewma_success_secs || 0).toFixed(1)} 秒
            </div>
            <div className="inline-flex items-center gap-2">
              <span
                className={`inline-block size-2 rounded-full ${burst.burst_active ? "bg-amber-500" : "bg-stone-300"}`}
              />
              突发 {String(burst.burst_enabled ? "开" : "关")}
              {burst.burst_active ? "（生效中）" : ""} · 有效并发上限{" "}
              {String(burst.effective_per_user_running_max ?? "-")}
            </div>
          </CardContent>
        </Card>
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-2 p-5 text-sm">
            <div className="font-medium text-stone-800">养号水位</div>
            <div>
              队列深度 {String(nurture.depth ?? "-")} · 今日每号{" "}
              {String(nurture.completed_in_day ?? nurture.completed_in_hour ?? 0)}/
              {String(nurture.max_per_account_per_day ?? nurture.max_per_hour ?? 0)}
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-stone-100">
              <div
                className="h-full bg-stone-800"
                style={{
                  width: `${Math.min(
                    100,
                    (Number(nurture.completed_in_day ?? nurture.completed_in_hour ?? 0) /
                      Math.max(1, Number(nurture.max_per_account_per_day ?? nurture.max_per_hour ?? 1))) *
                      100,
                  )}%`,
                }}
              />
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-2 p-5 text-sm">
            <div className="font-medium text-stone-800">轮询预算耗尽（进程累计）</div>
            {[
              ["wall", Number(pollEx.wall || 0)],
              ["conversation_get", Number(pollEx.conversation_get || 0)],
              ["tasks", Number(pollEx.tasks || 0)],
            ].map(([key, n]) => {
              const maxN = Math.max(1, Number(pollEx.wall || 0), Number(pollEx.conversation_get || 0), Number(pollEx.tasks || 0));
              const label = POLL_LABELS[String(key)] || String(key);
              return (
                <div key={String(key)} className="space-y-1">
                  <div className="flex justify-between text-xs text-stone-600">
                    <span>{label}</span>
                    <span>{n}</span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-stone-100">
                    <div className="h-full bg-rose-600/80" style={{ width: `${(Number(n) / maxN) * 100}%` }} />
                  </div>
                </div>
              );
            })}
          </CardContent>
        </Card>
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-2 p-5 text-sm">
            <div className="font-medium text-stone-800">失败连击直方图</div>
            {streakHist.length === 0 ? (
              <p className="text-stone-500">无样本</p>
            ) : (
              streakHist.map((row) => {
                const maxN = Math.max(1, ...streakHist.map((r) => Number(r.n || 0)));
                return (
                  <div key={row.bucket} className="space-y-1">
                    <div className="flex justify-between text-xs">
                      <span>连击 {row.bucket}</span>
                      <span>{row.n}</span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-stone-100">
                      <div className="h-full bg-amber-600" style={{ width: `${(Number(row.n || 0) / maxN) * 100}%` }} />
                    </div>
                  </div>
                );
              })
            )}
          </CardContent>
        </Card>
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-2 p-5 text-sm">
            <div className="font-medium text-stone-800">接收态漏斗</div>
            {Object.entries(receiveState).map(([k, v]) => (
              <div key={k} className="flex items-center justify-between text-xs">
                <span className="text-stone-600">{formatReceiveState(k)}</span>
                <Badge className="rounded-md">{Number(v || 0)}</Badge>
              </div>
            ))}
            <div className="pt-2">
              <div className="mb-1 text-xs text-stone-500">成熟度 有值 / 空</div>
              <div className="flex h-2 overflow-hidden rounded-full bg-stone-100">
                <div className="h-full bg-emerald-600" style={{ width: `${(maturitySet / maturityTotal) * 100}%` }} />
                <div className="h-full bg-stone-300" style={{ width: `${(maturityEmpty / maturityTotal) * 100}%` }} />
              </div>
              <div className="mt-1 text-xs text-stone-500">
                {maturitySet} / {maturityEmpty}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-2 p-5 text-sm">
            <div className="font-medium text-stone-800">群组 / 连击 / 软熔断</div>
            <div>暂停群组: {String(cohort.paused_cohort_count ?? 0)}</div>
            <div>终态命中合计: {String(cohort.cohort_terminal_hits_sum ?? 0)}</div>
            <div>
              软熔断 {String(derived.soft_capped_count ?? 0)} · 冷却{" "}
              {String(derived.cooldown_account_count ?? 0)} · 连击≥3 {String(derived.fail_streak_ge3 ?? 0)}
            </div>
            <div>
              粘性一对一绑定 {String(derived.sticky_unique_bindings ?? 0)} · 重复绑定组{" "}
              {String(derived.dup_binding_groups ?? 0)}
            </div>
            <div>
              成熟度有/空 {String(derived.maturity_set ?? 0)}/{String(derived.maturity_empty ?? 0)} · 指纹完整{" "}
              {String(derived.fp_complete ?? 0)}/{String(derived.fp_total ?? 0)}
            </div>
            {cohortRows.length ? (
              <div className="mt-2 max-h-40 overflow-auto rounded-lg border border-stone-100">
                <table className="w-full text-left text-xs">
                  <thead className="bg-stone-50 text-stone-500">
                    <tr>
                      <th className="px-2 py-1">群组</th>
                      <th className="px-2 py-1">账号数</th>
                      <th className="px-2 py-1">终态</th>
                      <th className="px-2 py-1">暂停</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cohortRows.slice(0, 12).map((row) => (
                      <tr key={String(row.cohort_id)} className="border-t border-stone-50">
                        <td className="px-2 py-1 font-mono">{String(row.cohort_id).slice(0, 10)}</td>
                        <td className="px-2 py-1">{String(row.accounts ?? 0)}</td>
                        <td className="px-2 py-1">{String(row.terminals ?? 0)}</td>
                        <td className="px-2 py-1">{row.paused ? "暂停" : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </CardContent>
        </Card>
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-2 p-5 text-sm">
            <div className="font-medium text-stone-800">LLM 运维近窗（6h）</div>
            <div>
              成功 {String(llm.ok ?? 0)} · 业务失败 {String(llm.error_pool ?? llm.error ?? 0)} · 巡检失败{" "}
              {String(llm.error_ops ?? 0)} · 拒绝 {String(llm.reject ?? 0)}
            </div>
            {llmHourly.length ? (
              <div className="flex h-16 items-end gap-1">
                {llmHourly.map((h) => {
                  const ok = Number(h.ok || 0);
                  const err = Number(h.error_pool || h.error || 0);
                  const rej = Number(h.reject || 0);
                  const sum = Math.max(1, ok + err + rej);
                  return (
                    <div key={String(h.hour)} className="flex h-full w-3 flex-col justify-end" title={`${h.hour}时`}>
                      <div className="w-full bg-rose-500" style={{ height: `${(err / sum) * 100}%` }} />
                      <div className="w-full bg-amber-400" style={{ height: `${(rej / sum) * 100}%` }} />
                      <div className="w-full bg-emerald-600" style={{ height: `${(ok / sum) * 100}%` }} />
                    </div>
                  );
                })}
              </div>
            ) : null}
            <div className="text-stone-500">
              巡检进程: {auditStatus?.worker_alive ? "运行中" : "未运行"} · 开关{" "}
              {auditStatus?.enabled ? "开" : "关"} · DeepSeek {auditStatus?.deepseek_configured ? "已配置" : "未配置"}
            </div>
            <div className="text-xs text-stone-500">缺口: {formatGaps(snap.gaps)}</div>
          </CardContent>
        </Card>
      </div>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-3 p-5">
          <div className="text-sm font-medium text-stone-800">主动巡检时间线</div>
          <div className="space-y-3">
            {checks.length === 0 ? (
              <p className="text-sm text-stone-500">暂无报告。点「立即巡检」或开启半小时主动巡检。</p>
            ) : (
              checks.map((c) => (
                <div key={String(c.id || c.finished_at)} className="rounded-xl border border-stone-100 bg-stone-50/80 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="text-sm font-medium text-stone-800">
                      {formatCheckTime(c.finished_at || c.started_at)}
                    </div>
                    <span
                      className={`rounded-md px-2 py-0.5 text-xs ${levelClass(String(c.risk_level || ""))}`}
                      title={
                        "最终等级=号池硬指标优先。" +
                        "中等常见于：群组暂停/重复绑定/连击≥3；" +
                        "DeepSeek JSON 解析失败本身不算中等风险。"
                      }
                    >
                      {LEVEL_LABELS[String(c.risk_level || "").toLowerCase()] || String(c.risk_level || "-")}
                    </span>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-2 text-xs text-stone-500">
                    <span>检查项: {formatScope(c.scope)}</span>
                    {c.risk_level_deterministic ? (
                      <span>
                        号池判定:{" "}
                        {LEVEL_LABELS[String(c.risk_level_deterministic).toLowerCase()] ||
                          String(c.risk_level_deterministic)}
                      </span>
                    ) : null}
                    {c.risk_level_gpt ? (
                      <span>
                        文案提示: {LEVEL_LABELS[String(c.risk_level_gpt).toLowerCase()] || String(c.risk_level_gpt)}
                      </span>
                    ) : null}
                  </div>
                  <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-stone-700">{formatSummary(c.summary)}</p>
                  {Array.isArray(c.findings) && c.findings.length > 0 ? (
                    <ul className="mt-2 space-y-1 rounded-lg border border-stone-200 bg-white px-3 py-2 text-xs text-stone-600">
                      <li className="font-medium text-stone-700">风险点（findings）</li>
                      {c.findings.slice(0, 8).map((f, i) => {
                        const item = f && typeof f === "object" ? f : {};
                        const name = String((item as { item?: string }).item || `finding-${i}`);
                        const detail = String((item as { detail?: string }).detail || "");
                        return (
                          <li key={`${name}-${i}`}>
                            {humanizeRiskFinding(name, detail)}
                          </li>
                        );
                      })}
                    </ul>
                  ) : (
                    <p className="mt-2 text-xs text-stone-400">
                      无结构化 findings。若仅见「中等」徽章：点开上方 title 看判定规则，或看「号池判定/文案提示」两列。
                    </p>
                  )}
                  <div className="mt-1 text-xs text-stone-500">{formatModelsLine(c.models, c.outcome)}</div>
                </div>
              ))
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function OpsContent() {
  const [tab, setTab] = useState<TabId>("risk");
  const [query, setQuery] = useState("为什么空池不可调度？");
  const [summary, setSummary] = useState("");
  const [plan, setPlan] = useState<string[]>([]);
  const [stepsJson, setStepsJson] = useState("");
  const [nurture, setNurture] = useState<Record<string, unknown> | null>(null);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);

  const loadNurture = async () => {
    try {
      const data = await fetchNurtureStatus();
      setNurture(data as unknown as Record<string, unknown>);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载养号状态失败");
    }
  };

  useEffect(() => {
    void loadNurture();
  }, []);

  const runRca = async () => {
    setBusy(true);
    try {
      const data = await runOpsAgent(query);
      setSummary(data.summary);
      setPlan(data.plan);
      setStepsJson(JSON.stringify(data.steps, null, 2));
      toast.success(`RCA 完成 ${data.latency_ms}ms`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "RCA 失败");
    } finally {
      setBusy(false);
    }
  };

  const toggleNurture = async (enabled: boolean) => {
    setBusy(true);
    try {
      const data = await setNurtureEnabled(enabled);
      setNurture(data);
      toast.success(enabled ? "养号已开启（默认仍需持久化账号）" : "养号已关闭");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "切换失败");
    } finally {
      setBusy(false);
    }
  };

  const enqueue = async () => {
    setBusy(true);
    try {
      const data = await enqueueNurture({ prompt: prompt.trim() || undefined, source: "ops_ui" });
      toast.success(`已入队 ${data.item_id.slice(0, 8)}… 深度=${data.queue.depth}`);
      setPrompt("");
      await loadNurture();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "入队失败");
    } finally {
      setBusy(false);
    }
  };

  const dayPct = Math.min(
    100,
    (Number(nurture?.completed_in_day ?? nurture?.completed_in_hour ?? 0) /
      Math.max(1, Number(nurture?.max_per_account_per_day ?? nurture?.max_per_hour ?? 1))) *
      100,
  );

  return (
    <section className="space-y-5">
      <div className="space-y-1">
        <div className="text-xs font-semibold tracking-[0.18em] text-stone-500 uppercase">运维</div>
        <h1 className="text-2xl font-semibold tracking-tight">运维 / 养号 / 风控拟人</h1>
        <p className="text-sm text-stone-500">L2 只读工具编排；风控看板只读；巡检写报告与运维 LLM 日志。</p>
      </div>

      <div className="flex flex-wrap gap-2">
        {(
          [
            ["risk", "风控拟人"],
            ["rca", "RCA"],
            ["nurture", "养号"],
          ] as Array<[TabId, string]>
        ).map(([id, label]) => (
          <Button
            key={id}
            variant={tab === id ? "default" : "outline"}
            className={`rounded-xl ${tab === id ? "bg-stone-950 text-white" : ""}`}
            onClick={() => setTab(id)}
          >
            {label}
          </Button>
        ))}
      </div>

      {tab === "risk" ? <RiskTab /> : null}

      {tab === "rca" ? (
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-3 p-5">
            <div className="text-sm font-medium text-stone-800">L2 Agent（确定性 playbook）</div>
            <Textarea value={query} onChange={(e) => setQuery(e.target.value)} className="min-h-24 rounded-xl" />
            <Button onClick={() => void runRca()} disabled={busy || !query.trim()} className="rounded-xl bg-stone-950 text-white">
              {busy ? <LoaderCircle className="size-4 animate-spin" /> : null}
              运行 RCA
            </Button>
            {plan.length ? (
              <div className="flex flex-wrap gap-2">
                {plan.map((name) => (
                  <Badge key={name} variant="secondary" className="rounded-md">
                    {name}
                  </Badge>
                ))}
              </div>
            ) : null}
            {summary ? <p className="text-sm leading-6 text-stone-700">{summary}</p> : null}
            {stepsJson ? (
              <pre className="max-h-64 overflow-auto rounded-xl bg-stone-50 p-3 text-xs text-stone-600">{stepsJson}</pre>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {tab === "nurture" ? (
        <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
          <CardContent className="space-y-3 p-5">
            <div className="flex items-center justify-between gap-2">
              <div className="text-sm font-medium text-stone-800">文本养号</div>
              <Button variant="ghost" className="h-8 rounded-lg px-2" onClick={() => void loadNurture()}>
                <RefreshCw className="size-4" />
              </Button>
            </div>
            <p className="text-xs leading-5 text-stone-500">
              这是<strong>真实</strong> L0 文本队列（不是假聊）。关闭时号池「对话」按钮仍可强制同步执行一条；
              开启后 worker 才会自动消化队列。队列深度见下方；成功后号池「记录」柱状会增加对话计数。
            </p>
            <div className="grid grid-cols-2 gap-2 text-sm text-stone-600">
              <div>
                已开启: <Badge className="rounded-md">{boolZh(nurture?.enabled)}</Badge>
              </div>
              <div>运行中: {boolZh(nurture?.running)}</div>
              <div>
                队列: 深度 {String((nurture?.queue as { depth?: number } | undefined)?.depth ?? 0)} · 最老{" "}
                {String((nurture?.queue as { oldest_age_sec?: number } | undefined)?.oldest_age_sec ?? 0)} 秒
              </div>
              <div>
                今日每号: {String(nurture?.completed_in_day ?? nurture?.completed_in_hour ?? 0)}/
                {String(nurture?.max_per_account_per_day ?? nurture?.max_per_hour ?? 0)}
                {typeof nurture?.turns_per_session === "number"
                  ? ` · 每会话 ${String(nurture.turns_per_session)} 轮`
                  : ""}
              </div>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-stone-100">
              <div className="h-full bg-stone-800" style={{ width: `${dayPct}%` }} />
            </div>
            {nurture?.last_error ? <p className="text-xs text-rose-600">{String(nurture.last_error)}</p> : null}
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" className="rounded-xl" disabled={busy} onClick={() => void toggleNurture(true)}>
                开启
              </Button>
              <Button variant="outline" className="rounded-xl" disabled={busy} onClick={() => void toggleNurture(false)}>
                关闭
              </Button>
            </div>
            <Input
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="真实短问（可空=随机题库）；勿写生图指令"
              className="rounded-xl"
            />
            <Button onClick={() => void enqueue()} disabled={busy} className="rounded-xl bg-stone-950 text-white">
              入队一条真实文本
            </Button>
          </CardContent>
        </Card>
      ) : null}
    </section>
  );
}

export default function OpsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || !session) return null;
  return <OpsContent />;
}
