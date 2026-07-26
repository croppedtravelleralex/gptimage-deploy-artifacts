export type ImageGanttSegment = {
  key: string;
  label: string;
  label_zh: string;
  label_en: string;
  ms: number;
};

export type ImagePhaseTimingsMs = {
  admit_queue_ms?: number;
  upload_queue_ms?: number;
  ps_queue_ms?: number;
  ss_queue_ms?: number;
  account_queue_ms?: number;
  download_queue_ms?: number;
  upload_ms?: number;
  ps_ms?: number;
  ss_ms?: number;
  sse_stream_ms?: number;
  download_ms?: number;
  wall_clock_ms?: number;
};

const SEGMENT_COLORS: Record<string, string> = {
  queue_wait: "bg-stone-300",
  sse_active: "bg-sky-500",
  poll_resolve: "bg-indigo-400",
  download_ms: "bg-emerald-500",
};

export function segmentColor(key: string): string {
  return SEGMENT_COLORS[key] || "bg-stone-400";
}

export function buildImageTaskGanttSegments(
  phase: ImagePhaseTimingsMs | null | undefined,
  opts?: { createdTs?: number; startedTs?: number },
): ImageGanttSegment[] {
  const pt = phase || {};
  let admit = Number(pt.admit_queue_ms || 0);
  let ssQueue = Number(pt.ss_queue_ms || 0);
  let accountQueue = Number(pt.account_queue_ms || 0);
  let sseStream = Number(pt.sse_stream_ms || 0);
  const ssMs = Number(pt.ss_ms || 0);
  const download = Number(pt.download_ms || 0);
  const createdTs = opts?.createdTs;
  const startedTs = opts?.startedTs;

  if (sseStream <= 0 && createdTs && startedTs) {
    const toResolve = Math.max(0, Math.round((startedTs - createdTs) * 1000));
    const knownQueue = admit + ssQueue + accountQueue;
    if (toResolve > knownQueue) {
      sseStream = Math.max(0, Math.min(ssMs, toResolve - knownQueue));
    }
    if (accountQueue <= 0 && toResolve > admit + ssQueue + sseStream) {
      accountQueue = Math.max(0, toResolve - admit - ssQueue - sseStream);
    }
  }

  const queueWait = admit + ssQueue + accountQueue;
  const pollResolve = Math.max(0, ssMs - accountQueue - sseStream);
  const segments: ImageGanttSegment[] = [];
  const push = (key: string, labelZh: string, labelEn: string, ms: number) => {
    const value = Math.max(0, Math.round(ms));
    if (value <= 0) return;
    segments.push({
      key,
      label: `${labelZh} ${labelEn}`,
      label_zh: labelZh,
      label_en: labelEn,
      ms: value,
    });
  };
  push("queue_wait", "排队", "Queue", queueWait);
  push("sse_active", "SSE生图", "SSE", sseStream);
  push("poll_resolve", "轮询", "Poll", pollResolve);
  push("download_ms", "下载", "Download", download);
  return segments;
}
