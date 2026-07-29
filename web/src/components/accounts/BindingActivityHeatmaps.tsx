"use client";

import { memo, useMemo } from "react";

import { SLOT_LABELS } from "@/components/accounts/BindingSgHeatmap";
import { cn } from "@/lib/utils";

export type ActivityMetric = "images_api" | "images_chat" | "dialogues_nurture" | "dialogues_real";

const METRIC_LABELS: Record<ActivityMetric, string> = {
  images_api: "api生图",
  images_chat: "对话生图",
  dialogues_nurture: "拟人对话",
  dialogues_real: "真实对话",
};

const METRIC_RGB: Record<ActivityMetric, [number, number, number]> = {
  images_api: [139, 92, 246],
  images_chat: [217, 70, 239],
  dialogues_nurture: [245, 158, 11],
  dialogues_real: [14, 165, 233],
};

const ALL_METRICS = Object.keys(METRIC_LABELS) as ActivityMetric[];

type Props = {
  matrices: Partial<Record<ActivityMetric, number[][]>>;
  weekLabel?: string;
  weekdayLabels?: string[];
  dayLabels?: string[];
  timezoneLabel?: string;
  compact?: boolean;
  className?: string;
};

function normalizeMatrix(matrix?: number[][]) {
  return Array.from({ length: 7 }, (_, day) =>
    Array.from({ length: 12 }, (_, slot) => Math.max(0, Number(matrix?.[day]?.[slot] ?? 0))),
  );
}

function cellStyle(count: number, max: number, metric: ActivityMetric) {
  if (count <= 0 || max <= 0) {
    return { backgroundColor: "#f5f5f4" };
  }
  const ratio = Math.min(1, count / max);
  const alpha = 0.2 + ratio * 0.8;
  const [r, g, b] = METRIC_RGB[metric];
  return { backgroundColor: `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})` };
}

const ActivityHeatmap = memo(function ActivityHeatmap({
  metric,
  matrix,
  total,
  weekdayLabels,
  dayLabels,
  weekLabel,
  compact,
}: {
  metric: ActivityMetric;
  matrix: number[][];
  total: number;
  weekdayLabels: string[];
  dayLabels: string[];
  weekLabel: string;
  compact: boolean;
}) {
  const max = useMemo(() => Math.max(0, ...matrix.flat()), [matrix]);
  const cellSize = compact ? "size-3" : "size-3.5";

  return (
    <div
      className={cn(
        "inline-flex min-w-0 flex-col gap-1 rounded-lg border border-stone-200/80 bg-white/70 p-1.5",
        compact ? "max-w-[148px]" : "max-w-[168px]",
      )}
    >
      <div className="flex items-baseline justify-between gap-1">
        <div className="truncate text-[10px] font-semibold text-stone-700">{METRIC_LABELS[metric]}</div>
        <div className="shrink-0 text-[9px] font-medium text-stone-500">Σ{total}</div>
      </div>
      <div className="flex items-start gap-1">
        <div className="flex flex-col gap-px pt-5">
          {SLOT_LABELS.map((slot) => (
            <div
              key={slot}
              className={cn(
                "flex items-center text-[7px] leading-none text-stone-400",
                compact ? "h-3" : "h-3.5",
              )}
            >
              {compact ? slot.slice(0, 2) : slot}
            </div>
          ))}
        </div>
        <div className="min-w-0">
          <div className="mb-px grid grid-cols-7 gap-px text-center text-[8px] font-medium text-stone-500">
            {weekdayLabels.map((day, index) => (
              <div key={`${day}-${index}`} className="flex w-3.5 flex-col leading-none">
                <span>{day}</span>
                {dayLabels[index] ? (
                  <span className="text-[7px] font-normal text-stone-400">{dayLabels[index]}</span>
                ) : null}
              </div>
            ))}
          </div>
          <div className="grid grid-cols-7 gap-px">
            {matrix.map((row, day) =>
              row.map((count, slot) => (
                <div
                  key={`${day}-${slot}`}
                  title={`${weekLabel} ${weekdayLabels[day] || ""} ${dayLabels[day] || ""} ${SLOT_LABELS[slot]} · ${METRIC_LABELS[metric]} ${count} 次`}
                  style={cellStyle(count, max, metric)}
                  className={cn(
                    "flex items-center justify-center rounded-[2px] border border-stone-200/40 text-[7px] font-semibold leading-none",
                    cellSize,
                    count > 0 ? "text-stone-800" : "text-transparent",
                  )}
                >
                  {count > 0 ? (count > 9 ? "9+" : count) : ""}
                </div>
              )),
            )}
          </div>
        </div>
      </div>
    </div>
  );
});

export const BindingActivityHeatmaps = memo(function BindingActivityHeatmaps({
  matrices,
  weekLabel = "",
  weekdayLabels = ["一", "二", "三", "四", "五", "六", "日"],
  dayLabels = [],
  timezoneLabel = "",
  compact = true,
  className,
}: Props) {
  const normalized = useMemo(() => {
    const items = ALL_METRICS.map((metric) => {
      const matrix = normalizeMatrix(matrices[metric]);
      const total = matrix.flat().reduce((sum, value) => sum + value, 0);
      return { metric, matrix, total };
    });
    const active = items.filter((item) => item.total > 0);
    return active.length > 0 ? active : items;
  }, [matrices]);

  const hasData = normalized.some((item) => item.total > 0);
  const labels = dayLabels.length === 7 ? dayLabels : Array.from({ length: 7 }, () => "");

  return (
    <div className={cn("flex flex-col gap-1", className)} style={{ contentVisibility: "auto" }}>
      <div className="grid grid-cols-2 gap-1.5 xl:grid-cols-4">
        {normalized.map((item) => (
          <ActivityHeatmap
            key={item.metric}
            metric={item.metric}
            matrix={item.matrix}
            total={item.total}
            weekdayLabels={weekdayLabels}
            dayLabels={labels}
            weekLabel={weekLabel}
            compact={compact}
          />
        ))}
      </div>
      <div className="text-[9px] text-stone-500">
        {weekLabel ? `${weekLabel}` : "本周"}
        {timezoneLabel ? ` · ${timezoneLabel}` : ""}
        {hasData ? " · 仅显示有数据的路（全空时展示四路）" : " · 本周暂无记录"}
      </div>
    </div>
  );
});
