"use client";

import { useEffect, useState } from "react";

import { fetchImagePipelineSnapshot, type ImagePipelineSnapshot } from "@/lib/api";

function laneRows(snapshot: ImagePipelineSnapshot | null) {
  const psLimit = snapshot?.ps?.limit ?? 10;
  const ssLimit = snapshot?.ss?.limit ?? 10;
  const rows: Array<{ key: string; label: string; stage: string; slot: number }> = [];
  for (let slot = 0; slot < psLimit; slot += 1) {
    rows.push({ key: `ps-${slot}`, label: `pS ${slot + 1}`, stage: "ps", slot });
  }
  for (let slot = 0; slot < ssLimit; slot += 1) {
    rows.push({ key: `ss-${slot}`, label: `sS ${slot + 1}`, stage: "sse", slot });
  }
  return rows;
}

export function ImagePipelineGantt() {
  const [snapshot, setSnapshot] = useState<ImagePipelineSnapshot | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await fetchImagePipelineSnapshot();
        if (!cancelled) setSnapshot(data);
      } catch {
        if (!cancelled) setSnapshot(null);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const segments = snapshot?.segments ?? [];
  const now = Date.now() / 1000;
  const windowStart = now - 120;

  return (
    <div className="rounded-xl border border-stone-200 bg-white p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-stone-900">生图双槽流水线</h3>
        <div className="text-xs text-stone-500">
          pS {snapshot?.ps?.active ?? 0}/{snapshot?.ps?.limit ?? 10} · sS {snapshot?.ss?.active ?? 0}/{snapshot?.ss?.limit ?? 10}
          {" · "}排队 pS:{snapshot?.ps?.queued ?? 0} sS:{snapshot?.ss?.queued ?? 0}
        </div>
      </div>
      <div className="space-y-1">
        {laneRows(snapshot).map((row) => {
          const isPs = row.stage === "ps";
          const active = segments.filter(
            (segment) => segment.stage === row.stage && segment.slot === row.slot && !segment.ended_at,
          );
          return (
            <div key={row.key} className="grid grid-cols-[72px_1fr] items-center gap-2 text-xs">
              <div className="text-stone-500">{row.label}</div>
              <div className="relative h-5 rounded bg-stone-100">
                {active.map((segment) => {
                  const left = Math.max(0, ((segment.started_at - windowStart) / 120) * 100);
                  const width = Math.max(4, ((now - segment.started_at) / 120) * 100);
                  return (
                    <div
                      key={`${segment.task_key}-${segment.started_at}`}
                      className={`absolute top-0.5 h-4 rounded ${isPs ? "bg-amber-400/80" : "bg-sky-500/80"}`}
                      style={{ left: `${left}%`, width: `${Math.min(100 - left, width)}%` }}
                      title={segment.task_key}
                    />
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
      {snapshot?.ready_buffer ? (
        <div className="mt-3 text-xs text-stone-500">
          READY 缓冲 {(snapshot.ready_buffer.bytes / (1024 * 1024)).toFixed(1)}MB /{" "}
          {(snapshot.ready_buffer.max_bytes / (1024 * 1024)).toFixed(0)}MB
          {snapshot.ready_buffer.ss_paused ? " · sS 已反压暂停" : ""}
        </div>
      ) : null}
    </div>
  );
}
