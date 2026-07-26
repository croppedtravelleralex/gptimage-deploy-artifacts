from __future__ import annotations

from typing import Any


def _segment(key: str, label_zh: str, label_en: str, ms: int) -> dict[str, Any]:
    value = max(0, int(ms or 0))
    return {
        "key": key,
        "label": f"{label_zh} {label_en}",
        "label_zh": label_zh,
        "label_en": label_en,
        "ms": value,
    }


def build_image_task_gantt_segments(
    phase: dict[str, Any] | None,
    *,
    created_ts: float | None = None,
    started_ts: float | None = None,
) -> list[dict[str, Any]]:
    """Build non-overlapping wall-clock segments for image task Gantt charts."""
    pt = phase if isinstance(phase, dict) else {}
    admit = int(pt.get("admit_queue_ms") or 0)
    ss_queue = int(pt.get("ss_queue_ms") or 0)
    account_queue = int(pt.get("account_queue_ms") or 0)
    sse_stream = int(pt.get("sse_stream_ms") or 0)
    ss_ms = int(pt.get("ss_ms") or 0)
    download = int(pt.get("download_ms") or 0)

    if sse_stream <= 0 and created_ts and started_ts:
        to_resolve = max(0, int(round((float(started_ts) - float(created_ts)) * 1000)))
        known_queue = admit + ss_queue + account_queue
        if to_resolve > known_queue:
            sse_stream = max(0, min(ss_ms, to_resolve - known_queue))
        if account_queue <= 0 and to_resolve > admit + ss_queue + sse_stream:
            account_queue = max(0, to_resolve - admit - ss_queue - sse_stream)

    queue_wait = admit + ss_queue + account_queue
    poll_resolve = max(0, ss_ms - account_queue - sse_stream)

    segments: list[dict[str, Any]] = []
    if queue_wait > 0:
        segments.append(_segment("queue_wait", "排队", "Queue", queue_wait))
    if sse_stream > 0:
        segments.append(_segment("sse_active", "SSE生图", "SSE", sse_stream))
    if poll_resolve > 0:
        segments.append(_segment("poll_resolve", "轮询", "Poll", poll_resolve))
    if download > 0:
        segments.append(_segment("download_ms", "下载", "Download", download))
    return segments
