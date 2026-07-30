from __future__ import annotations

import pytest

from services.image_failure_classification import (
    FAILURE_PHASE_POLL,
    FAILURE_PHASE_SSE,
    FAILURE_REASON_POLL_BUG,
    FAILURE_REASON_POLL_TIMEOUT,
    FAILURE_REASON_SSE_SLOW,
    classify_image_failure,
)


def test_classify_sse_slow_wall_timeout() -> None:
    obs = classify_image_failure(
        error_message="sS stage wall timeout (120s, elapsed 121.0s)",
        code="image_sse_slow",
        conversation_id="conv-1",
        phase_timings_ms={"sse_stream_ms": 121000},
    )
    assert obs["failure_phase"] == FAILURE_PHASE_SSE
    assert obs["failure_reason"] == FAILURE_REASON_SSE_SLOW


def test_classify_poll_timeout_after_sse() -> None:
    obs = classify_image_failure(
        error_message="ChatGPT 生图超时",
        code="image_poll_timeout",
        conversation_id="conv-2",
        phase_timings_ms={"sse_stream_ms": 68000, "poll_resolve_ms": 61000},
        poll_budget={"conversation_gets": 4, "elapsed_wall_secs": 60.0},
    )
    assert obs["failure_phase"] == FAILURE_PHASE_POLL
    assert obs["failure_reason"] == FAILURE_REASON_POLL_TIMEOUT
    assert obs["poll_conversation_gets"] == 4


def test_classify_poll_bug_when_sse_had_file_ids() -> None:
    obs = classify_image_failure(
        error_message="File link not found",
        code="image_poll_timeout",
        conversation_id="conv-3",
        phase_timings_ms={"sse_stream_ms": 70000, "poll_resolve_ms": 12000},
        sse_had_file_ids=True,
    )
    assert obs["failure_phase"] == FAILURE_PHASE_POLL
    assert obs["failure_reason"] == "poll_estuary_not_ready"
