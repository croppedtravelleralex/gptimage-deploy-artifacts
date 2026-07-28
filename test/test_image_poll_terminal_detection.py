"""Fast-fail terminal upstream states during image poll."""

from __future__ import annotations

from unittest import mock

import pytest

from services.config import config
from services.openai_backend_api import (
    ImageContentPolicyError,
    ImagePollTimeoutError,
    ImageUpstreamTerminalError,
    OpenAIBackendAPI,
    _classify_terminal_upstream_text,
)


def _assistant_mapping(text: str) -> dict:
    return {
        "mapping": {
            "node-1": {
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": [text]},
                }
            }
        }
    }


def _poll_backend(conversation: dict) -> OpenAIBackendAPI:
    backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
    backend.access_token = "tok-terminal"
    backend.cancel_event = None
    backend._get_conversation = mock.Mock(return_value=conversation)
    backend._query_backend_tasks = mock.Mock(return_value=[])
    return backend


def _patch_poll_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(config.data, "image_poll_initial_wait_secs", 0)
    monkeypatch.setitem(config.data, "image_poll_interval_secs", 0)
    monkeypatch.setitem(config.data, "image_poll_max_upstream_gets", 3)
    monkeypatch.setitem(config.data, "image_poll_max_tasks_gets", 4)
    monkeypatch.setitem(config.data, "image_poll_tasks_every_n_attempts", 1)
    monkeypatch.setitem(config.data, "image_poll_cf_abort_streak", 99)
    monkeypatch.setitem(config.data, "image_poll_429_abort_streak", 99)
    monkeypatch.setitem(config.data, "image_settle_enabled", False)
    monkeypatch.setitem(config.data, "image_check_before_hit_enabled", False)


def test_classify_missing_reference_image() -> None:
    text = "请上传人物参考图，目前对话中还没有可用的人物图片。"
    assert _classify_terminal_upstream_text(text) == ("missing_reference_image", text)


def test_classify_instant_limit() -> None:
    text = (
        "Image creation will be available again when your Instant limit resets. "
        "Do you want to try something else for now?"
    )
    assert _classify_terminal_upstream_text(text)[0] == "image_instant_limit"


def test_find_terminal_block_from_title() -> None:
    data = {"title": "Image Creation Limit", "mapping": {}}
    assert OpenAIBackendAPI._find_terminal_upstream_block_in_conversation(data) == (
        "image_instant_limit",
        "Image Creation Limit",
    )


def test_poll_aborts_on_missing_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_poll_config(monkeypatch)
    backend = _poll_backend(_assistant_mapping("请上传人物参考图，目前对话中还没有可用的人物图片。"))
    with mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None):
        with pytest.raises(ImageUpstreamTerminalError) as caught:
            OpenAIBackendAPI._poll_image_results(backend, "conv-missing-ref", timeout_secs=1.0)
    assert caught.value.code == "missing_reference_image"


def test_poll_aborts_on_instant_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_poll_config(monkeypatch)
    conversation = {
        "title": "Image Creation Limit",
        "mapping": {
            "node-1": {
                "message": {
                    "author": {"role": "assistant"},
                    "content": {
                        "content_type": "text",
                        "parts": [
                            "Image creation will be available again when your Instant limit resets."
                        ],
                    },
                }
            }
        },
    }
    backend = _poll_backend(conversation)
    with mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None):
        with pytest.raises(ImageUpstreamTerminalError) as caught:
            OpenAIBackendAPI._poll_image_results(backend, "conv-limit", timeout_secs=1.0)
    assert caught.value.code == "image_instant_limit"


def test_poll_aborts_on_task_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_poll_config(monkeypatch)
    backend = _poll_backend({"mapping": {}})
    backend._query_backend_tasks = mock.Mock(
        return_value=[
            {
                "image_gen_message": {
                    "author": {"role": "assistant"},
                    "metadata": {"is_error": True},
                    "content": {
                        "content_type": "text",
                        "parts": ["请上传人物参考图后再试。"],
                    },
                }
            }
        ]
    )
    with mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None):
        with pytest.raises(ImageUpstreamTerminalError) as caught:
            OpenAIBackendAPI._poll_image_results(backend, "conv-task-error", timeout_secs=1.0)
    assert caught.value.code == "missing_reference_image"


def test_poll_keeps_waiting_when_image_gen_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_poll_config(monkeypatch)
    conversation = _assistant_mapping("请上传人物参考图后再继续。")
    conversation["mapping"]["node-2"] = {
        "message": {
            "author": {"role": "assistant"},
            "metadata": {"async_task_type": "image_gen"},
            "content": {"content_type": "text", "parts": [""]},
        }
    }
    backend = _poll_backend(conversation)
    with mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None):
        with pytest.raises(ImagePollTimeoutError):
            OpenAIBackendAPI._poll_image_results(backend, "conv-in-flight", timeout_secs=0.05)


def test_poll_still_raises_content_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_poll_config(monkeypatch)
    backend = _poll_backend(_assistant_mapping("抱歉，我不能生成违反内容政策的内容。"))
    with mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None):
        with pytest.raises(ImageContentPolicyError):
            OpenAIBackendAPI._poll_image_results(backend, "conv-policy", timeout_secs=1.0)
