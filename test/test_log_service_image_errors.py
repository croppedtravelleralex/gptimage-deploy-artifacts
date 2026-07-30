from __future__ import annotations

import json

import pytest

from services.log_service import _image_error_response


def _status_and_code(resp) -> tuple[int, str | None]:
    body = json.loads(resp.body.decode("utf-8"))
    code = body.get("error", {}).get("code")
    return int(resp.status_code), code


def test_content_policy_returns_400() -> None:
    exc = RuntimeError("This request was rejected by the content policy filter.")
    status, code = _status_and_code(_image_error_response(exc))
    assert status == 400
    assert code == "content_policy_violation"


def test_instant_limit_returns_429() -> None:
    exc = RuntimeError("Instant limit reached; limit resets in 2h.")
    status, code = _status_and_code(_image_error_response(exc))
    assert status == 429
    assert code == "image_instant_limit"


def test_duplicate_prompt_returns_429_with_retry_after() -> None:
    exc = RuntimeError("duplicate prompt within 120s window")
    resp = _image_error_response(exc)
    status, code = _status_and_code(resp)
    assert status == 429
    assert code == "duplicate_prompt"
    assert resp.headers.get("retry-after") == "30"


def test_generic_upstream_failure_stays_502() -> None:
    exc = RuntimeError("upstream connection reset")
    status, _ = _status_and_code(_image_error_response(exc))
    assert status == 502


def test_internal_scheduler_error_maps_to_504_user_message() -> None:
    exc = RuntimeError("续轮询预算不足（同步调用方等待预算已耗尽）")
    resp = _image_error_response(exc)
    status, code = _status_and_code(resp)
    body = json.loads(resp.body.decode("utf-8"))
    assert status == 504
    assert code == "image_generation_timeout"
    assert "续轮询" not in body["error"]["message"]
    assert "longer than expected" in body["error"]["message"].lower()

