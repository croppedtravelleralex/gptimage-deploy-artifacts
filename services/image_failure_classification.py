from __future__ import annotations

from typing import Any

FAILURE_PHASE_SSE = "sse"
FAILURE_PHASE_POLL = "poll"
FAILURE_PHASE_SCHEDULE = "schedule"
FAILURE_PHASE_UNKNOWN = "unknown"

FAILURE_REASON_SSE_FAILED = "sse_failed"
FAILURE_REASON_SSE_SLOW = "sse_slow"
FAILURE_REASON_SSE_NO_CONVERSATION = "sse_no_conversation_id"
FAILURE_REASON_POLL_TIMEOUT = "poll_timeout"
FAILURE_REASON_POLL_BUG = "poll_bug"
FAILURE_REASON_POLL_ESTUARY_404 = "poll_estuary_not_ready"
FAILURE_REASON_POLL_RATE_LIMITED = "poll_rate_limited"
FAILURE_REASON_POLL_UPSTREAM_TERMINAL = "poll_upstream_terminal"
FAILURE_REASON_SCHEDULE_QUEUE = "schedule_queue_timeout"
FAILURE_REASON_UNKNOWN = "unknown"


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _phase_ms(phase_timings_ms: dict[str, Any] | None, key: str) -> int | None:
    if not isinstance(phase_timings_ms, dict):
        return None
    return _positive_int(phase_timings_ms.get(key))


def classify_image_failure(
    *,
    error_message: str,
    code: str = "",
    conversation_id: str = "",
    phase_timings_ms: dict[str, Any] | None = None,
    poll_budget: dict[str, Any] | None = None,
    sse_had_file_ids: bool = False,
    sse_had_sediment_ids: bool = False,
    schedule_queue_ms: int | None = None,
    exc: BaseException | None = None,
) -> dict[str, Any]:
    """Build operator-facing failure observability for terminal image tasks."""
    message = str(error_message or "").strip()
    lower = message.lower()
    code_text = str(code or getattr(exc, "code", "") or "").strip()
    conv_id = str(conversation_id or getattr(exc, "conversation_id", "") or "").strip()
    if poll_budget is None and exc is not None:
        raw_budget = getattr(exc, "poll_budget", None)
        poll_budget = raw_budget if isinstance(raw_budget, dict) else None

    sse_ms = _phase_ms(phase_timings_ms, "sse_stream_ms")
    poll_ms = _phase_ms(phase_timings_ms, "poll_resolve_ms")
    ss_queue_ms = _phase_ms(phase_timings_ms, "ss_queue_ms")
    task_queue_ms = _phase_ms(phase_timings_ms, "task_queue_ms")

    try:
        from services.config import config

        sse_budget_ms = int(float(config.image_attempt_sse_phase_secs) * 1000)
        poll_budget_ms = int(float(config.image_attempt_poll_phase_secs) * 1000)
    except Exception:
        sse_budget_ms = 120_000
        poll_budget_ms = 60_000

    failure_phase = FAILURE_PHASE_UNKNOWN
    failure_reason = FAILURE_REASON_UNKNOWN
    failure_detail = message[:500]

    if code_text == "image_poll_bug" or "poll_bug" in code_text:
        failure_phase = FAILURE_PHASE_POLL
        failure_reason = FAILURE_REASON_POLL_BUG
    elif code_text == "image_poll_estuary_not_ready" or "file link not found" in lower:
        failure_phase = FAILURE_PHASE_POLL
        failure_reason = FAILURE_REASON_POLL_ESTUARY_404
    elif code_text == "image_poll_rate_limited":
        failure_phase = FAILURE_PHASE_POLL
        failure_reason = FAILURE_REASON_POLL_RATE_LIMITED
    elif "sS stage wall timeout" in message or "ss stage wall timeout" in lower:
        failure_phase = FAILURE_PHASE_SSE
        failure_reason = FAILURE_REASON_SSE_SLOW
    elif code_text in {"image_sse_failed", "image_sse_slow"}:
        failure_phase = FAILURE_PHASE_SSE
        failure_reason = code_text.replace("image_", "")
    elif task_queue_ms and task_queue_ms > 30_000:
        failure_phase = FAILURE_PHASE_SCHEDULE
        failure_reason = FAILURE_REASON_SCHEDULE_QUEUE
    elif ss_queue_ms and ss_queue_ms > 30_000:
        failure_phase = FAILURE_PHASE_SCHEDULE
        failure_reason = FAILURE_REASON_SCHEDULE_QUEUE
    elif not conv_id and (
        "timeout" in lower
        or "timed out" in lower
        or "超时" in message
        or code_text.startswith("image_sse")
    ):
        failure_phase = FAILURE_PHASE_SSE
        failure_reason = FAILURE_REASON_SSE_NO_CONVERSATION if "conversation" in lower else FAILURE_REASON_SSE_FAILED
    elif code_text in {"image_poll_timeout", "image_timeout_pending"} and conv_id:
        failure_phase = FAILURE_PHASE_POLL
        failure_reason = (
            FAILURE_REASON_POLL_BUG
            if sse_had_file_ids or sse_had_sediment_ids
            else FAILURE_REASON_POLL_TIMEOUT
        )
    elif conv_id and (
        isinstance(exc, Exception)
        and exc.__class__.__name__ in {"ImagePollTimeoutError", "ImagePollRateLimitedError"}
    ):
        failure_phase = FAILURE_PHASE_POLL
        failure_reason = (
            FAILURE_REASON_POLL_BUG
            if sse_had_file_ids or sse_had_sediment_ids
            else FAILURE_REASON_POLL_TIMEOUT
        )
    elif conv_id and ("poll" in lower or poll_ms):
        failure_phase = FAILURE_PHASE_POLL
        failure_reason = FAILURE_REASON_POLL_TIMEOUT
    elif "upstream" in lower and conv_id:
        failure_phase = FAILURE_PHASE_POLL
        failure_reason = FAILURE_REASON_POLL_UPSTREAM_TERMINAL

    if failure_reason == FAILURE_REASON_SSE_SLOW and sse_ms is not None and sse_ms < sse_budget_ms * 0.85:
        failure_reason = FAILURE_REASON_SSE_FAILED

    observability: dict[str, Any] = {
        "failure_phase": failure_phase,
        "failure_reason": failure_reason,
        "failure_detail": failure_detail,
        "conversation_id": conv_id or None,
        "sse_stream_ms": sse_ms,
        "poll_resolve_ms": poll_ms,
        "task_queue_ms": task_queue_ms,
        "ss_queue_ms": ss_queue_ms,
        "schedule_queue_ms": schedule_queue_ms,
        "sse_phase_budget_ms": sse_budget_ms,
        "poll_phase_budget_ms": poll_budget_ms,
        "sse_had_file_ids": bool(sse_had_file_ids),
        "sse_had_sediment_ids": bool(sse_had_sediment_ids),
        "error_code": code_text or None,
    }
    if isinstance(poll_budget, dict) and poll_budget:
        observability["poll_budget"] = poll_budget
        observability["poll_conversation_gets"] = poll_budget.get("conversation_gets")
        observability["poll_elapsed_wall_secs"] = poll_budget.get("elapsed_wall_secs")
        observability["poll_exhausted_reason"] = poll_budget.get("effective_exhausted_reason")
    return observability


def user_message_for_failure(observability: dict[str, Any]) -> str:
    from services.protocol.user_facing_errors import map_user_facing_image_error

    reason = str(observability.get("failure_reason") or "")
    if reason in {
        FAILURE_REASON_POLL_BUG,
        FAILURE_REASON_POLL_ESTUARY_404,
    }:
        return map_user_facing_image_error(
            "Image generation completed upstream but result retrieval failed. Please retry."
        )
    return map_user_facing_image_error(str(observability.get("failure_detail") or ""))
