"""Poll loop aborts after consecutive CF edge blocks."""

from __future__ import annotations

from unittest import mock

import pytest

from services.config import config
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import UpstreamHTTPError


def test_image_poll_aborts_on_cf_streak(monkeypatch):
    backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
    backend.access_token = "tok-poll-cf"
    backend.cancel_event = None
    backend._query_backend_tasks = mock.Mock(
        side_effect=UpstreamHTTPError(
            "/backend-api/tasks",
            403,
            "cloudflare_or_edge_html_block: blocked",
        )
    )
    backend._get_conversation = mock.Mock(
        side_effect=UpstreamHTTPError(
            "/backend-api/conversation/x",
            403,
            "cloudflare_or_edge_html_block: blocked",
        )
    )
    backend._extract_image_tool_records = lambda _conv: []
    backend._find_content_policy_error_in_conversation = lambda _conv: ""

    monkeypatch.setitem(config.data, "image_poll_initial_wait_secs", 0)
    monkeypatch.setitem(config.data, "image_poll_interval_secs", 0)
    monkeypatch.setitem(config.data, "image_poll_cf_abort_streak", 2)
    monkeypatch.setitem(config.data, "image_poll_max_upstream_gets", 10)
    monkeypatch.setitem(config.data, "image_poll_max_tasks_gets", 4)
    monkeypatch.setitem(config.data, "image_poll_tasks_every_n_attempts", 1)
    monkeypatch.setitem(config.data, "image_settle_enabled", False)
    monkeypatch.setitem(config.data, "image_check_before_hit_enabled", False)

    with mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None):
        with pytest.raises(UpstreamHTTPError) as caught:
            OpenAIBackendAPI._poll_image_results(backend, "conv-cf", timeout_secs=30)
    assert "cloudflare_or_edge_html_block" in str(caught.value).lower()
    assert getattr(caught.value, "cf_abort", False) is True
    assert backend._get_conversation.call_count >= 1
    assert backend._get_conversation.call_count <= 3


def test_resolver_does_not_send_attachment_request_after_cf_abort(monkeypatch):
    backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
    cf_error = UpstreamHTTPError(
        "image_poll/conversation",
        403,
        "cloudflare_or_edge_html_block: image poll aborted",
    )
    setattr(cf_error, "cf_abort", True)
    backend._poll_image_results = mock.Mock(side_effect=cf_error)
    backend._resolve_image_urls = mock.Mock(return_value=[])

    monkeypatch.setitem(config.data, "image_check_before_hit_enabled", True)
    monkeypatch.setitem(config.data, "image_settle_enabled", False)

    with pytest.raises(UpstreamHTTPError) as caught:
        backend.resolve_conversation_image_urls(
            "conv-cf",
            [],
            ["sediment-from-sse"],
            poll=True,
            poll_timeout_secs=30,
        )

    assert getattr(caught.value, "cf_abort", False) is True
    backend._resolve_image_urls.assert_not_called()
