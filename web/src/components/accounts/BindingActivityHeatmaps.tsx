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

const METRIC_COLORS: Record<ActivityMetric, string[]> = {
  images_api: ["bg-stone-100", "bg-violet-200", "bg-violet-400", "bg-violet-700"],
  images_chat: ["bg-stone-100", "bg-fuchsia-200", "bg-fuchsia-400", "bg-fuchsia-700"],
  dialogues_nurture: ["bg-stone-100", "bg-amber-200", "bg-amber-400", "bg-amber-700"],
  dialogues_real: ["bg-stone-100", "bg-sky-200", "bg-sky-400", "bg-sky-700"],
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

function colorForCount(count: number, metric: ActivityMetric) {
  const palette = METRIC_COLORS[metric];
  if (count <= 0) return palette[0];
  if (count === 1) return palette[1];
  if (count <= 5) return palette[2];
  return palette[3];
}

function ActivityHeatmap({ metric, matrix, days }: { metric: ActivityMetric; matrix: number[][]; days: number }) {
  return (
    <div className="inline-flex flex-col gap-0.5">
      <div className="text-[10px] font-medium text-stone-600">{METRIC_LABELS[metric]}</div>
      <div className="flex items-start gap-1">
        <div className="flex flex-col gap-px pt-3">
          {SLOT_LABELS.map((slot) => (
            <div key={slot} className="flex h-2.5 items-center text-[7px] leading-none text-stone-400">
              {slot}
            </div>
          ))}
        </div>
        <div>
          <div className="mb-0.5 grid grid-cols-7 gap-px text-center text-[8px] text-stone-500">
            {DAY_LABELS.map((day) => (
              <span key={day} className="w-2.5">
                {day}
              </span>
            ))}
          </div>
          <div className="grid grid-cols-7 gap-px">
            {matrix.map((row, day) =>
              row.map((count, slot) => (
                <div
                  key={`${day}-${slot}`}
                  title={`${DAY_LABELS[day]} ${SLOT_LABELS[slot]} · ${METRIC_LABELS[metric]} ${count} 次 · 近${days}天`}
                  className={cn("size-3 rounded-[2px]", colorForCount(count, metric))}
                />
              )),
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export function BindingActivityHeatmaps({ matrices, days = 28, className }: Props) {
  const normalized = useMemo(
    () =>
      (Object.keys(METRIC_LABELS) as ActivityMetric[]).map((metric) => ({
        metric,
        matrix: normalizeMatrix(matrices[metric]),
      })),
    [matrices],
  );
  const hasData = normalized.some((item) => item.matrix.some((row) => row.some((v) => v > 0)));

  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <div className="flex flex-wrap items-start gap-3">
        {normalized.map((item) => (
          <ActivityHeatmap key={item.metric} metric={item.metric} matrix={item.matrix} days={days} />
        ))}
      </div>
      <div className="text-[9px] text-stone-400">
        活动热力 · 近 {days} 天 · Asia/Singapore{hasData ? " · 悬停格子看次数" : " · 暂无记录（需分组视图且近 28 天有调用日志）"}
      </div>
    </div>
  );
}
