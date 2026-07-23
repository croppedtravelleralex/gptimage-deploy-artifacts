"use client";

import { useMemo, useState } from "react";

export type ChartSeries = {
  key: string;
  label: string;
  color: string;
  values: number[];
};

type Props = {
  dates: string[];
  series: ChartSeries[];
  yLabel?: string;
  xLabel?: string;
  /** false = 各序列独立归一化（形状对比）；true = 共用 Y（数量对比） */
  sharedScale?: boolean;
  /** 固定 Y 轴最大值（如百分比 100） */
  fixedMax?: number;
  height?: number;
  emptyText?: string;
};

function buildSmoothPath(values: number[], maxValue: number, width: number, height: number) {
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

function formatTick(date: string) {
  const raw = String(date || "").trim();
  // Already MM-DD HH:MM or MM-DD HH:MM:SS → keep MM-DD HH:MM
  if (/^\d{2}-\d{2} \d{2}:\d{2}/.test(raw)) return raw.slice(0, 11);
  // YYYY-MM-DDTHH:MM:SS… or YYYY-MM-DD HH:MM:SS…
  if (raw.length >= 16 && /^\d{4}-\d{2}-\d{2}/.test(raw)) {
    return raw.slice(5, 16).replace("T", " ");
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw.slice(5, 10);
  if (raw.length >= 10 && raw.includes("T")) return raw.slice(5, 16).replace("T", " ");
  return raw;
}

/** Pick sparse tick indices; never place last tick too close to previous. */
function pickTickIndices(n: number, maxTicks = 7): number[] {
  if (n <= 0) return [];
  if (n <= maxTicks) return Array.from({ length: n }, (_, i) => i);
  const indices = new Set<number>([0, n - 1]);
  const step = (n - 1) / (maxTicks - 1);
  for (let i = 1; i < maxTicks - 1; i += 1) {
    indices.add(Math.round(i * step));
  }
  const sorted = Array.from(indices).sort((a, b) => a - b);
  // Drop middle ticks that crowd the end (within ~4% of span)
  const minGap = Math.max(1, Math.floor(n / (maxTicks * 2)));
  const out: number[] = [];
  for (const idx of sorted) {
    if (!out.length || idx - out[out.length - 1] >= minGap) {
      out.push(idx);
    }
  }
  if (out[out.length - 1] !== n - 1) {
    while (out.length > 1 && n - 1 - out[out.length - 2] < minGap) {
      out.splice(out.length - 2, 1);
    }
    if (out[out.length - 1] !== n - 1) out.push(n - 1);
  }
  return out;
}

export function InteractiveLineChart({
  dates,
  series,
  yLabel = "数量",
  xLabel = "日期",
  sharedScale = true,
  fixedMax,
  height = 200,
  emptyText = "暂无数据",
}: Props) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  const chart = useMemo(() => {
    const n = dates.length;
    if (n === 0 || series.length === 0) return null;
    const plotW = 640;
    const plotH = 120;
    const paths = series.map((s) => {
      const maxY = fixedMax ?? (sharedScale ? Math.max(1, ...series.flatMap((x) => x.values)) : Math.max(1, ...s.values));
      return {
        ...s,
        path: buildSmoothPath(s.values, maxY, plotW, plotH),
        maxY,
      };
    });
    const sharedMax = fixedMax ?? (sharedScale ? Math.max(1, ...series.flatMap((x) => x.values)) : 100);
    return { paths, sharedMax, plotW, plotH, n };
  }, [dates, series, sharedScale, fixedMax]);

  if (!chart) {
    return <p className="text-sm text-stone-500">{emptyText}</p>;
  }

  const { paths, sharedMax, plotW, plotH, n } = chart;
  const tipIndex = hoverIndex != null && hoverIndex >= 0 && hoverIndex < n ? hoverIndex : null;

  return (
    <div className="relative">
      <div className="mb-2 flex flex-wrap items-center gap-3 text-xs text-stone-500">
        {series.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1">
            <span className="size-2 rounded-full" style={{ background: s.color }} />
            {s.label}
          </span>
        ))}
      </div>
      <div className="overflow-x-auto">
        <svg
          viewBox="0 0 700 180"
          className="w-full min-w-[700px]"
          style={{ height }}
          onMouseLeave={() => setHoverIndex(null)}
        >
          <line x1="48" y1="12" x2="48" y2="140" stroke="#a8a29e" strokeWidth="1" />
          <line x1="48" y1="140" x2="688" y2="140" stroke="#a8a29e" strokeWidth="1" />
          {[0, 1, 2, 3, 4].map((line) => {
            const y = 12 + line * 32;
            const value = Math.round(sharedMax * (1 - line / 4));
            return (
              <g key={line}>
                <line x1="48" x2="688" y1={y} y2={y} stroke="#e7e5e4" strokeWidth="1" />
                <text x="42" y={y + 3} textAnchor="end" className="fill-stone-400 text-[10px]">
                  {sharedScale || fixedMax != null ? `${value}${fixedMax === 100 ? "%" : ""}` : ""}
                </text>
              </g>
            );
          })}
          <g transform="translate(48 12)">
            {paths.map((p) => (
              <path
                key={p.key}
                d={p.path}
                fill="none"
                stroke={p.color}
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            ))}
            {tipIndex != null
              ? paths.map((p) => {
                  const maxY = sharedScale || fixedMax != null ? sharedMax : p.maxY;
                  const x = n <= 1 ? 0 : (tipIndex / (n - 1)) * plotW;
                  const y = plotH - (Math.max(0, p.values[tipIndex] || 0) / Math.max(1, maxY)) * plotH;
                  return <circle key={`dot-${p.key}`} cx={x} cy={y} r="3.5" fill={p.color} stroke="#fff" strokeWidth="1.5" />;
                })
              : null}
          </g>
          {(() => {
            const tickIdx = pickTickIndices(n, 7);
            return tickIdx.map((index) => {
              const date = dates[index];
              const x = 48 + (n <= 1 ? 0 : (index / (n - 1)) * plotW);
              return (
                <text
                  key={`tick-${index}`}
                  x={x}
                  y="158"
                  textAnchor={index === 0 ? "start" : index === n - 1 ? "end" : "middle"}
                  className="fill-stone-500 text-[10px]"
                >
                  {formatTick(date)}
                </text>
              );
            });
          })()}
          {/* invisible hit areas */}
          {dates.map((date, index) => {
            const x = 48 + (n <= 1 ? 0 : (index / (n - 1)) * plotW);
            const half = n <= 1 ? plotW / 2 : plotW / Math.max(2, (n - 1) * 2);
            return (
              <rect
                key={`hit-${date}-${index}`}
                x={x - half}
                y={12}
                width={half * 2}
                height={128}
                fill="transparent"
                onMouseEnter={() => setHoverIndex(index)}
              />
            );
          })}
          {tipIndex != null ? (
            <line
              x1={48 + (n <= 1 ? 0 : (tipIndex / (n - 1)) * plotW)}
              x2={48 + (n <= 1 ? 0 : (tipIndex / (n - 1)) * plotW)}
              y1={12}
              y2={140}
              stroke="#a8a29e"
              strokeDasharray="3 3"
              strokeWidth="1"
            />
          ) : null}
          <text x="368" y="176" textAnchor="middle" className="fill-stone-400 text-[10px]">
            {xLabel}
          </text>
          <text x="14" y="76" textAnchor="middle" className="fill-stone-400 text-[10px]" transform="rotate(-90 14 76)">
            {yLabel}
          </text>
        </svg>
      </div>
      {tipIndex != null ? (
        <div className="pointer-events-none absolute left-1/2 top-8 z-10 -translate-x-1/2 rounded-xl border border-stone-200 bg-white/95 px-3 py-2 text-xs shadow-lg">
          <div className="mb-1 font-medium text-stone-800">{formatTick(dates[tipIndex])}</div>
          <div className="space-y-0.5 text-stone-600">
            {series.map((s) => (
              <div key={s.key} className="flex items-center justify-between gap-4">
                <span className="inline-flex items-center gap-1">
                  <span className="size-1.5 rounded-full" style={{ background: s.color }} />
                  {s.label}
                </span>
                <span className="font-medium text-stone-900">
                  {Number(s.values[tipIndex] ?? 0).toLocaleString("zh-CN", { maximumFractionDigits: 1 })}
                  {fixedMax === 100 ? "%" : ""}
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {!sharedScale && fixedMax == null ? (
        <p className="mt-1 text-[11px] text-stone-400">各序列独立归一化；悬停显示真实数值</p>
      ) : null}
    </div>
  );
}

export function DateRangeControls({
  from,
  to,
  min,
  max,
  onChange,
  presets,
}: {
  from: string;
  to: string;
  min?: string;
  max?: string;
  onChange: (from: string, to: string) => void;
  presets?: Array<{ label: string; days: number }>;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-stone-600">
      <label className="inline-flex items-center gap-1">
        起
        <input
          type="date"
          value={from}
          min={min}
          max={to || max}
          onChange={(e) => onChange(e.target.value, to)}
          className="rounded-lg border border-stone-200 bg-white px-2 py-1"
        />
      </label>
      <label className="inline-flex items-center gap-1">
        止
        <input
          type="date"
          value={to}
          min={from || min}
          max={max}
          onChange={(e) => onChange(from, e.target.value)}
          className="rounded-lg border border-stone-200 bg-white px-2 py-1"
        />
      </label>
      {(presets || [
        { label: "7天", days: 7 },
        { label: "14天", days: 14 },
        { label: "30天", days: 30 },
      ]).map((p) => (
        <button
          key={p.label}
          type="button"
          className="rounded-lg border border-stone-200 bg-white px-2 py-1 hover:bg-stone-50"
          onClick={() => {
            const end = max || to;
            if (!end) return;
            const endDate = new Date(`${end}T00:00:00`);
            const startDate = new Date(endDate);
            startDate.setDate(endDate.getDate() - (p.days - 1));
            const startStr = startDate.toISOString().slice(0, 10);
            onChange(min && startStr < min ? min : startStr, end);
          }}
        >
          {p.label}
        </button>
      ))}
    </div>
  );
}
