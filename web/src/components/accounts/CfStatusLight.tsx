"use client";

import { cn } from "@/lib/utils";

export type CfDayPoint = {
  date?: string;
  ok?: number;
  cf?: number;
  image_fail?: number;
};

type Props = {
  days?: CfDayPoint[] | null;
  className?: string;
};

export type CfLightStatus = "none" | "ok" | "warn" | "error";

export function summarizeCfDay(row?: CfDayPoint | null): {
  ok: number;
  cf: number;
  image_fail: number;
  status: CfLightStatus;
} {
  if (!row || typeof row !== "object") {
    return { ok: 0, cf: 0, image_fail: 0, status: "none" };
  }
  const ok = Math.max(0, Number(row.ok) || 0);
  const cf = Math.max(0, Number(row.cf) || 0);
  const image_fail = Math.max(0, Number(row.image_fail) || 0);
  const total = ok + cf + image_fail;
  let status: CfLightStatus = "none";
  if (total > 0) {
    if (cf + image_fail > ok) {
      status = "error";
    } else if (cf > 0) {
      status = "warn";
    } else {
      status = "ok";
    }
  }
  return { ok, cf, image_fail, status };
}

export function summarizeCfDays(days?: CfDayPoint[] | null): {
  ok: number;
  cf: number;
  image_fail: number;
  status: CfLightStatus;
} {
  let ok = 0;
  let cf = 0;
  let image_fail = 0;
  for (const row of days || []) {
    const day = summarizeCfDay(row);
    ok += day.ok;
    cf += day.cf;
    image_fail += day.image_fail;
  }
  const total = ok + cf + image_fail;
  let status: CfLightStatus = "none";
  if (total > 0) {
    if (cf + image_fail > ok) {
      status = "error";
    } else if (cf > 0) {
      status = "warn";
    } else {
      status = "ok";
    }
  }
  return { ok, cf, image_fail, status };
}

const STATUS_CLASS: Record<CfLightStatus, string> = {
  ok: "bg-emerald-500",
  warn: "bg-amber-400",
  error: "bg-rose-500",
  none: "bg-stone-300",
};

const STATUS_LABEL: Record<CfLightStatus, string> = {
  ok: "无 CF 403",
  warn: "有 CF 但少于成功",
  error: "CF/生图失败多于成功",
  none: "无业务样本",
};

/** 近 7 日 CF/生图被动计数指示灯：绿无403 / 灰无数据 / 黄偶发 / 红偏多 */
export function CfStatusLight({ days, className }: Props) {
  const points =
    Array.isArray(days) && days.length
      ? days
      : Array.from({ length: 7 }, (_, i) => ({ date: `d${i}` }));
  const seven = points.slice(-7);
  while (seven.length < 7) {
    seven.unshift({ date: `pad-${seven.length}` });
  }

  const totals = summarizeCfDays(seven);

  return (
    <div className={cn("flex items-center gap-0.5", className)} aria-label="近7日CF状态">
      {seven.map((row, i) => {
        const { ok, cf, image_fail, status } = summarizeCfDay(row);
        const color = STATUS_CLASS[status];
        const label = `${row.date || "—"} · ${STATUS_LABEL[status]} · ok=${ok} cf=${cf} fail=${image_fail}`;
        return (
          <span
            key={`${row.date}-${i}`}
            title={label}
            className={cn("inline-block size-1.5 rounded-full", color)}
          />
        );
      })}
      <span
        className="ml-0.5 text-[10px] text-stone-400"
        title={`近7日合计 · ok=${totals.ok} cf=${totals.cf} fail=${totals.image_fail}`}
      >
        CF
      </span>
    </div>
  );
}
