from __future__ import annotations

from services.config import DEFAULT_IMAGE_PIPELINE, DEFAULT_IMAGE_TASK_QUEUE, config


def test_attempt_budget_defaults_in_code() -> None:
    assert int(DEFAULT_IMAGE_PIPELINE["ss_stage_wall_timeout_secs"]) == 120
    assert int(DEFAULT_IMAGE_TASK_QUEUE["generation_poll_timeout_secs"]) == 60
    assert int(DEFAULT_IMAGE_TASK_QUEUE["timeout_pending_max_attempts"]) == 0


def test_timeout_pending_max_attempts_zero_is_preserved(monkeypatch) -> None:
    monkeypatch.setitem(config.data, "image_task_queue", {"timeout_pending_max_attempts": 0})
    normalized = config.get_image_task_queue_settings()
    assert normalized.get("timeout_pending_max_attempts") == 0


def test_attempt_budget_properties_respect_explicit_override(monkeypatch) -> None:
    monkeypatch.setitem(config.data, "image_attempt_sse_phase_secs", 100)
    monkeypatch.setitem(config.data, "image_attempt_poll_phase_secs", 80)
    monkeypatch.setitem(config.data, "newapi_image_attempt_budget_secs", 180)
    assert config.image_attempt_sse_phase_secs == 100.0
    assert config.image_attempt_poll_phase_secs == 80.0
    assert config.newapi_image_attempt_budget_secs == 180.0
