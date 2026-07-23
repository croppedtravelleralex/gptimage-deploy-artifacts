"use client";

import { useMemo } from "react";

import { cn } from "@/lib/utils";

const DAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"] as const;
const SLOT_LABELS = [
  "00-02",
  "02-04",
  "04-06",
  "06-08",
  "08-10",
  "10-12",
  "12-14",
  "14-16",
  "16-18",
  "18-20",
  "20-22",
  "22-24",
] as const;

const WEIGHT_CYCLE = [0, 0.25, 0.5, 0.75, 1] as const;

type Props = {
  weights: number[][];
  label?: string;
  editable?: boolean;
  onChange?: (weights: number[][]) => void;
  className?: string;
};

function clampWeight(value: number) {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function weightColor(weight: number) {
  const w = clampWeight(weight);
  if (w <= 0) return "bg-stone-100";
  if (w < 0.25) return "bg-emerald-200/90";
  if (w < 0.5) return "bg-emerald-400/90";
  if (w < 0.75) return "bg-emerald-600/90";
  return "bg-emerald-800/90";
}

function normalizeWeights(weights: number[][]) {
  const matrix = Array.from({ length: 7 }, (_, day) =>
    Array.from({ length: 12 }, (_, slot) => clampWeight(Number(weights?.[day]?.[slot] ?? 0))),
  );
  return matrix;
}

function nextWeight(current: number) {
  const w = clampWeight(current);
  const idx = WEIGHT_CYCLE.findIndex((v) => Math.abs(v - w) < 0.001);
  const next = idx < 0 ? 0 : (idx + 1) % WEIGHT_CYCLE.length;
  return WEIGHT_CYCLE[next];
}

/** 新加坡时区 7×12（2h 槽）软权重热力图，用于 IP 绑定组头。 */
export function BindingSgHeatmap({ weights, label, editable = false, onChange, className }: Props) {
  const matrix = useMemo(() => normalizeWeights(weights), [weights]);

  const handleCellClick = (day: number, slot: number) => {
    if (!editable || !onChange) return;
    const next = matrix.map((row) => [...row]);
    next[day][slot] = nextWeight(next[day][slot]);
    onChange(next);
  };

  return (
    <div className={cn("inline-flex flex-col gap-0.5", className)}>
      {label ? <div className="text-[10px] font-medium text-stone-600">{label}</div> : null}
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
              row.map((weight, slot) => {
                const tip = `${DAY_LABELS[day]} ${SLOT_LABELS[slot]} · 权重 ${clampWeight(weight).toFixed(2)} · Asia/Singapore`;
                return (
                  <button
                    key={`${day}-${slot}`}
                    type="button"
                    title={tip}
                    disabled={!editable}
                    onClick={() => handleCellClick(day, slot)}
                    className={cn(
                      "size-2.5 rounded-[2px] transition",
                      weightColor(weight),
                      editable ? "cursor-pointer hover:ring-1 hover:ring-stone-300" : "cursor-default",
                    )}
                  />
                );
              }),
            )}
          </div>
        </div>
      </div>
      <div className="text-[9px] text-stone-400">时区 Asia/Singapore</div>
    </div>
  );
}

export { DAY_LABELS, SLOT_LABELS, normalizeWeights as normalizeBindingWeights };
