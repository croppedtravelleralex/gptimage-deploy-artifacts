"use client";

import { cn } from "@/lib/utils";

export type EgressDayPoint = {
  date: string;
  status?: "ok" | "warn" | "error" | "none" | string;
  ip?: string;
};

type Props = {
  days?: EgressDayPoint[] | null;
  className?: string;
};

const STATUS_CLASS: Record<string, string> = {
  ok: "bg-emerald-500",
  warn: "bg-amber-400",
  error: "bg-rose-500",
  none: "bg-stone-300",
};

/** 近 7 日 IP 漂移指示灯：绿无漂移 / 灰无数据 / 黄漂移 / 红错误 */
export function EgressDriftLights({ days, className }: Props) {
  const points = Array.isArray(days) && days.length ? days : Array.from({ length: 7 }, (_, i) => ({ date: `d${i}`, status: "none" }));
  const seven = points.slice(-7);
  while (seven.length < 7) {
    seven.unshift({ date: `pad-${seven.length}`, status: "none" });
  }

  return (
    <div className={cn("flex items-center gap-0.5", className)} aria-label="近7日IP漂移监测">
      {seven.map((d, i) => {
        const st = String(d.status || "none").toLowerCase();
        const color = STATUS_CLASS[st] || STATUS_CLASS.none;
        const label = `${d.date || "—"} · ${st}${d.ip ? ` · ${d.ip}` : ""}`;
        return (
          <span
            key={`${d.date}-${i}`}
            title={label}
            className={cn("inline-block size-1.5 rounded-full", color)}
          />
        );
      })}
    </div>
  );
}
