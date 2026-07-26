"""Pure SSE consume / failure-class helpers for SPA image bench (P4-D1~D5)."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class SseConsumeResult:
    chunks: int = 0
    sse_bytes: int = 0
    cid: str = ""
    has_image_gen: bool = False
    has_image_gen_within_gate: bool = False
    gate_failed: bool = False
    late_image_gen_seen: bool = False
    image_gen_ms: int | None = None
    first_event_ms: int | None = None
    file_ids: list[str] = field(default_factory=list)
    sediment_ids: list[str] = field(default_factory=list)
    event_timeline: list[dict[str, Any]] = field(default_factory=list)
    last_payloads: list[str] = field(default_factory=list)
    extra_chunks_after_gate: int = 0
    diagnostic_stopped_reason: str = ""
    gate_ms: int | None = None
    diagnostic_ms: int | None = None
    total_ms: int = 0
    tool_args_like_seen: bool = False
    quiet_stream: bool = True


_TOOL_ARGS_MARKERS = ('"size"', '"prompt"', '"n"', '"quality"', "1024x1024", "1792x1024")


def _payload_hint(payload: str) -> str:
    lower = payload.lower()
    if "image_gen" in lower:
        return "image_gen"
    if any(m in payload for m in _TOOL_ARGS_MARKERS):
        return "tool_args"
    if "file_id" in lower or "file-service://" in lower:
        return "file_id"
    if "sediment://" in lower:
        return "sediment"
    if '"recipient"' in lower and "image" in lower:
        return "tool_recipient"
    if lower in ("{}", "") or "ping" in lower or "heartbeat" in lower:
        return "ping"
    return "other"


def _extract_event_meta(payload: str) -> dict[str, str]:
    meta: dict[str, str] = {
        "author": "",
        "name": "",
        "recipient": "",
        "content_type": "",
        "event": "",
        "status": "",
    }
    try:
        data = json.loads(payload)
    except Exception:
        if "image_gen" in payload:
            meta["recipient"] = "image_gen"
        return meta
    if not isinstance(data, dict):
        return meta
    meta["event"] = str(data.get("event") or data.get("type") or "")
    message = data.get("message")
    if isinstance(message, dict):
        author = message.get("author")
        if isinstance(author, dict):
            meta["author"] = str(author.get("role") or "")
            meta["name"] = str(author.get("name") or "")
        meta["recipient"] = str(message.get("recipient") or "")
        content = message.get("content")
        if isinstance(content, dict):
            meta["content_type"] = str(content.get("content_type") or "")
        metadata = message.get("metadata")
        if isinstance(metadata, dict):
            meta["status"] = str(metadata.get("status") or metadata.get("async_task_type") or "")
    return meta


def redact_sse_timeline_event(payload: str, arrival_ms: int, *, max_hint: int = 120) -> dict[str, Any]:
    meta = _extract_event_meta(payload)
    hint = _payload_hint(payload)
    return {
        "arrival_ms": int(arrival_ms),
        "author": meta["author"],
        "name": meta["name"],
        "recipient": meta["recipient"],
        "content_type": meta["content_type"],
        "event": meta["event"],
        "status": meta["status"],
        "payload_hint": hint,
        "payload_chars": min(len(payload), max_hint),
    }


def classify_image_sse_failure(
    *,
    has_image_gen_within_gate: bool,
    gate_failed: bool,
    late_image_gen_seen: bool,
    tool_args_like_seen: bool,
    quiet_stream: bool,
    chunks: int,
) -> str | None:
    if has_image_gen_within_gate:
        return None
    if late_image_gen_seen:
        return "late_image_gen_after_gate"
    if tool_args_like_seen:
        return "tool_args_as_text"
    if chunks == 0 or quiet_stream:
        return "no_image_gen_quiet_stream"
    if gate_failed:
        return "no_image_gen_within_gate"
    return "no_image_gen_within_gate"


def consume_image_sse(
    lines: Iterator[bytes | str],
    *,
    t0: float | None = None,
    gate_secs: float = 45.0,
    total_read_secs: float = 90.0,
    max_timeline: int = 80,
    max_last_payloads: int = 4,
) -> SseConsumeResult:
    """Parse SSE lines; check gate only after each parsed data line (P4-D1).

    ``total_read_secs`` is wall clock from ``t0`` for the entire SSE read (P4-D5, default 90s).
    After ``gate_secs`` without ``image_gen``, continue read-only until ``total_read_secs``.
    """
    start = t0 if t0 is not None else time.time()
    result = SseConsumeResult()
    gate_triggered_at: float | None = None
    diagnostic_enabled = total_read_secs > gate_secs

    for line in lines:
        if not line:
            continue
        now = time.time()
        elapsed = now - start
        if elapsed > total_read_secs:
            result.diagnostic_stopped_reason = "wall"
            break

        if isinstance(line, bytes):
            result.sse_bytes += len(line)
            text = line.decode("utf-8", errors="ignore")
        else:
            text = str(line)
            result.sse_bytes += len(text.encode("utf-8", errors="ignore"))

        if not text.startswith("data:"):
            continue

        payload = text[5:].strip()
        if payload == "[DONE]":
            result.diagnostic_stopped_reason = "done"
            break

        arrival_ms = int(elapsed * 1000)
        if result.first_event_ms is None:
            result.first_event_ms = arrival_ms

        result.chunks += 1
        if result.gate_failed:
            result.extra_chunks_after_gate += 1

        hint = _payload_hint(payload)
        if hint in ("tool_args", "tool_recipient"):
            result.tool_args_like_seen = True
            result.quiet_stream = False
        elif hint not in ("ping",):
            result.quiet_stream = False

        if len(result.event_timeline) < max_timeline:
            result.event_timeline.append(redact_sse_timeline_event(payload, arrival_ms))

        snippet = payload[:200]
        result.last_payloads.append(snippet)
        if len(result.last_payloads) > max_last_payloads:
            result.last_payloads.pop(0)

        if not result.cid:
            m = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
            if m:
                result.cid = m.group(1)

        saw_image_gen = "image_gen" in payload
        if saw_image_gen and result.image_gen_ms is None:
            result.image_gen_ms = arrival_ms
        if saw_image_gen:
            result.has_image_gen = True
            if elapsed <= gate_secs:
                result.has_image_gen_within_gate = True
            else:
                result.late_image_gen_seen = True

        for m in re.finditer(r"file-service://(file-[A-Za-z0-9_-]+)", payload):
            if m.group(1) not in result.file_ids:
                result.file_ids.append(m.group(1))
        for m in re.finditer(r'"file_id"\s*:\s*"(file-[A-Za-z0-9_-]+)"', payload):
            if m.group(1) not in result.file_ids:
                result.file_ids.append(m.group(1))
        for m in re.finditer(r"sediment://([A-Za-z0-9_-]+)", payload):
            if m.group(1) not in result.sediment_ids:
                result.sediment_ids.append(m.group(1))

        if not result.has_image_gen_within_gate and elapsed > gate_secs:
            if not result.gate_failed:
                result.gate_failed = True
                gate_triggered_at = now
                result.gate_ms = arrival_ms

        if result.has_image_gen_within_gate and result.chunks >= 5:
            if result.chunks >= 40 or result.file_ids:
                result.diagnostic_stopped_reason = "success_early"
                break
        if result.chunks >= 80:
            result.diagnostic_stopped_reason = "chunk_limit"
            break

        if result.gate_failed and not diagnostic_enabled:
            result.diagnostic_stopped_reason = "gate_no_diagnostic"
            break

    if not result.diagnostic_stopped_reason:
        result.diagnostic_stopped_reason = "stream_end"

    end = time.time()
    result.total_ms = int((end - start) * 1000)
    if gate_triggered_at is not None:
        result.diagnostic_ms = int((end - gate_triggered_at) * 1000)
    return result


def empty_cf_observability() -> dict[str, Any]:
    return {
        "home_403_soft_fail": False,
        "home_status": None,
        "requirements_cf403": 0,
        "start_cf403": 0,
        "tasks_cf403": 0,
        "propagated_cf": 0,
    }


def merge_propagated_cf(cf_obs: dict[str, Any]) -> dict[str, Any]:
    propagated = int(cf_obs.get("requirements_cf403") or 0) > 0
    propagated = propagated or int(cf_obs.get("start_cf403") or 0) > 0
    propagated = propagated or int(cf_obs.get("tasks_cf403") or 0) > 0
    cf_obs["propagated_cf"] = int(propagated)
    return cf_obs
