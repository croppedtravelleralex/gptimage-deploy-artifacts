"use client";

import { useMemo } from "react";

import { DAY_LABELS, SLOT_LABELS } from "@/components/accounts/BindingSgHeatmap";
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

type Props = {
  matrices: Partial<Record<ActivityMetric, number[][]>>;
  days?: number;
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
  const alpha = 0.18 + ratio * 0.82;
  const [r, g, b] = METRIC_RGB[metric];
  return { backgroundColor: `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})` };
}

function ActivityHeatmap({
  metric,
  matrix,
  days,
  total,
}: {
  metric: ActivityMetric;
  matrix: number[][];
  days: number;
  total: number;
}) {
  const max = useMemo(() => Math.max(0, ...matrix.flat()), [matrix]);

  return (
    <div className="inline-flex min-w-[168px] flex-col gap-1 rounded-lg border border-stone-200/80 bg-white/70 p-2">
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-[11px] font-semibold text-stone-700">{METRIC_LABELS[metric]}</div>
        <div className="text-[10px] font-medium text-stone-500">Σ {total}</div>
      </div>
      <div className="flex items-start gap-1.5">
        <div className="flex flex-col gap-0.5 pt-4">
          {SLOT_LABELS.map((slot) => (
            <div key={slot} className="flex h-3.5 items-center text-[8px] leading-none text-stone-400">
              {slot}
            </div>
          ))}
        </div>
        <div>
          <div className="mb-0.5 grid grid-cols-7 gap-0.5 text-center text-[9px] font-medium text-stone-500">
            {DAY_LABELS.map((day) => (
              <span key={day} className="w-4">
                {day}
              </span>
            ))}
          </div>
          <div className="grid grid-cols-7 gap-0.5">
            {matrix.map((row, day) =>
              row.map((count, slot) => (
                <div
                  key={`${day}-${slot}`}
                  title={`${DAY_LABELS[day]} ${SLOT_LABELS[slot]} · ${METRIC_LABELS[metric]} ${count} 次 · 近${days}天`}
                  style={cellStyle(count, max, metric)}
                  className={cn(
                    "flex size-4 items-center justify-center rounded-[3px] border border-stone-200/50 text-[8px] font-semibold leading-none",
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
      <div className="flex items-center gap-1 text-[8px] text-stone-400">
        <span>少</span>
        <div className="flex gap-0.5">
          {[0.15, 0.35, 0.55, 0.85].map((alpha) => {
            const [r, g, b] = METRIC_RGB[metric];
            return (
              <span
                key={alpha}
                className="size-2.5 rounded-[2px] border border-stone-200/60"
                style={{ backgroundColor: `rgba(${r}, ${g}, ${b}, ${alpha})` }}
              />
            );
          })}
        </div>
        <span>多</span>
      </div>
    </div>
  );
}

export function BindingActivityHeatmaps({ matrices, days = 28, className }: Props) {
  const normalized = useMemo(
    () =>
      (Object.keys(METRIC_LABELS) as ActivityMetric[]).map((metric) => {
        const matrix = normalizeMatrix(matrices[metric]);
        const total = matrix.flat().reduce((sum, value) => sum + value, 0);
        return { metric, matrix, total };
      }),
    [matrices],
  );
  const hasData = normalized.some((item) => item.total > 0);

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <div className="flex flex-wrap items-start gap-2">
        {normalized.map((item) => (
          <ActivityHeatmap
            key={item.metric}
            metric={item.metric}
            matrix={item.matrix}
            days={days}
            total={item.total}
          />
        ))}
      </div>
      <div className="text-[9px] text-stone-500">
        活动热力 · 近 {days} 天 · Asia/Singapore
        {hasData ? " · 格子数字=该时段次数，颜色按本路最大值加深" : " · 暂无记录"}
      </div>
    </div>
  );
}
