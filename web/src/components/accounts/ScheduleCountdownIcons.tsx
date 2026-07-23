"use client";

import { useEffect, useRef, useState } from "react";
import { MessageSquare, RefreshCw } from "lucide-react";

import type { Account } from "@/lib/api";

function formatRemain(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec)) return "—";
  const s = Math.max(0, Math.floor(sec));
  if (s <= 0) return "可执行";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${r}s`;
  return `${r}s`;
}

function formatAbs(iso: string | null | undefined): string {
  if (!iso) return "无排期";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

type IconTone = "ready" | "wait" | "none";

function toneClass(tone: IconTone): string {
  if (tone === "ready") return "text-emerald-600";
  if (tone === "wait") return "text-amber-600";
  return "text-stone-300";
}

export function ScheduleCountdownIcons({
  account,
  showLazy = true,
  showText = true,
}: {
  account: Account;
  showLazy?: boolean;
  showText?: boolean;
}) {
  const loadedAtRef = useRef(Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const elapsed = Math.floor((now - loadedAtRef.current) / 1000);
  const lazySecRaw = account.lazy_refresh_in_sec;
  const textSecRaw = account.text_next_ok_in_sec;
  const lazyAt = account.lazy_refresh_eligible_at;
  const textAt = account.text_next_ok_at;

  const lazySec = lazySecRaw == null ? null : Math.max(0, Number(lazySecRaw) - elapsed);
  const textSec = textSecRaw == null ? null : Math.max(0, Number(textSecRaw) - elapsed);

  const quota = Number(account.quota || 0);
  const lazyRelevant = account.status === "正常" && quota <= 0 && Boolean(lazyAt);
  const lazyTone: IconTone = !lazyRelevant
    ? "none"
    : lazySec != null && lazySec > 0
      ? "wait"
      : "ready";
  const textTone: IconTone =
    textAt == null && textSecRaw == null
      ? "none"
      : textSec != null && textSec > 0
        ? "wait"
        : "ready";

  return (
    <span className="inline-flex items-center gap-0.5">
      {showLazy ? (
        <span
          className={`inline-flex rounded-md p-1 ${toneClass(lazyTone)}`}
          title={
            lazyRelevant
              ? `懒刷新（错峰后拉 limits）\n${formatAbs(lazyAt)}\n剩余 ${formatRemain(lazySec)}`
              : "懒刷新：额度未耗尽或不适用"
          }
        >
          <RefreshCw className="size-3.5" />
        </span>
      ) : null}
      {showText ? (
        <span
          className={`inline-flex rounded-md p-1 ${toneClass(textTone)}`}
          title={
            textTone === "none"
              ? "拟人对话：尚无下次排期"
              : `拟人对话下次可执行\n${formatAbs(textAt)}\n剩余 ${formatRemain(textSec)}`
          }
        >
          <MessageSquare className="size-3.5" />
        </span>
      ) : null}
    </span>
  );
}
