"use client";

import { useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

export type UsageDayPoint = {
  date: string;
  images: number;
  dialogues: number;
  images_api?: number;
  images_chat?: number;
  dialogues_real?: number;
  dialogues_nurture?: number;
};

type Props = {
  days: UsageDayPoint[];
  className?: string;
};

/** 近几日生图/对话双色柱：进入视口后再渲染，减少大表重绘。 */
export function AccountUsageHeatstrip({ days, className }: Props) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(false);
  const [hover, setHover] = useState<string | null>(null);

  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true);
          io.disconnect();
        }
      },
      { rootMargin: "80px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  const points = days.length > 0 ? days : [];
  const maxVal = Math.max(
    1,
    ...points.map((d) => Math.max(Number(d.images || 0), Number(d.dialogues || 0))),
  );

  if (!visible) {
    return <div ref={rootRef} className={cn("h-6 w-16 rounded bg-stone-100/80", className)} />;
  }

  if (points.length === 0) {
    return (
      <div ref={rootRef}>
        <span className="text-xs text-stone-400">—</span>
      </div>
    );
  }

  return (
    <div
      ref={rootRef}
      className={cn("relative flex items-end gap-0.5", className)}
      aria-label="近几日生图与对话记录"
      onMouseLeave={() => setHover(null)}
    >
      {points.map((day) => {
        const images = Number(day.images || 0);
        const dialogues = Number(day.dialogues || 0);
        const imgH = images <= 0 ? 0 : Math.max(14, (images / maxVal) * 100);
        const dlgH = dialogues <= 0 ? 0 : Math.max(14, (dialogues / maxVal) * 100);
        const showTip = hover === day.date;
        return (
          <div
            key={day.date}
            className="relative h-6 w-3"
            onMouseEnter={() => setHover(day.date)}
          >
            <div className="absolute inset-0 overflow-hidden rounded-[3px] bg-stone-100">
              <div
                className="absolute bottom-0 left-0 w-[55%] rounded-sm bg-violet-500/85"
                style={{ height: `${imgH}%` }}
              />
              <div
                className="absolute bottom-0 right-0 w-[55%] rounded-sm bg-amber-500/90"
                style={{ height: `${dlgH}%` }}
              />
            </div>
            {showTip ? (
              <div className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-1 -translate-x-1/2 whitespace-nowrap rounded-md border border-stone-200 bg-white px-2 py-1 text-[10px] text-stone-700 shadow-md">
                <div className="font-medium text-stone-800">{day.date}</div>
                <div>
                  <span className="text-violet-600">api生图 {Number(day.images_api || 0)}</span>
                  {" · "}
                  <span className="text-fuchsia-700">对话生图 {Number(day.images_chat || 0)}</span>
                </div>
                <div>
                  <span className="text-sky-700">真实对话 {Number(day.dialogues_real || 0)}</span>
                  {" · "}
                  <span className="text-amber-700">拟人 {Number(day.dialogues_nurture || 0)}</span>
                </div>
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
