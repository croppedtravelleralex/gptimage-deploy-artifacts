"""Poll loop aborts after consecutive upstream HTTP 429."""

from __future__ import annotations

from unittest import mock

import pytest

from services.config import config
from services.openai_backend_api import ImagePollRateLimitedError, OpenAIBackendAPI
from utils.helper import UpstreamHTTPError


def test_image_poll_aborts_on_429_streak(monkeypatch):
    backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
    backend.access_token = "tok-poll-429"
    backend.cancel_event = None
    backend._query_backend_tasks = mock.Mock(return_value=[])
    backend._get_conversation = mock.Mock(
        side_effect=UpstreamHTTPError("/backend-api/conversation/x", 429, "rate_limit_exceeded")
    )
    backend._extract_image_tool_records = lambda _conv: []
    backend._find_content_policy_error_in_conversation = lambda _conv: ""

    monkeypatch.setitem(config.data, "image_poll_initial_wait_secs", 0)
    monkeypatch.setitem(config.data, "image_poll_interval_secs", 0)
    monkeypatch.setitem(config.data, "image_poll_429_abort_streak", 3)
    monkeypatch.setitem(config.data, "image_poll_max_upstream_gets", 10)
    monkeypatch.setitem(config.data, "image_poll_max_tasks_gets", 0)
    monkeypatch.setitem(config.data, "image_poll_tasks_every_n_attempts", 99)
    monkeypatch.setitem(config.data, "image_settle_enabled", False)
    monkeypatch.setitem(config.data, "image_check_before_hit_enabled", False)

    with mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None):
        with pytest.raises(ImagePollRateLimitedError) as caught:
            OpenAIBackendAPI._poll_image_results(backend, "conv-429", timeout_secs=30)
    assert caught.value.status_code == 429
    assert backend._get_conversation.call_count == 3


def test_poll_initial_wait_extends_for_early_sse(monkeypatch):
    backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
    monkeypatch.setitem(config.data, "image_poll_initial_wait_secs", 20)
    monkeypatch.setitem(config.data, "image_poll_early_sse_ms", 5000)
    monkeypatch.setitem(config.data, "image_poll_early_sse_initial_wait_secs", 25)
    assert backend._resolve_poll_initial_wait_secs(500.0) == 25.0
    assert backend._resolve_poll_initial_wait_secs(8000.0) == 20.0
