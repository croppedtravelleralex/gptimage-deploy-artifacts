from __future__ import annotations

import base64
import time
import unittest
from unittest import mock

from services.config import config
from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    extract_conversation_ids,
    is_tls_connection_error,
    prefer_stream_for_multi_image,
    stream_image_outputs,
    stream_image_outputs_with_pool,
)
from services.protocol.openai_v1_response import stream_image_response
from utils.helper import UpstreamHTTPError


def _conversation(file_ids: list[str], sediment_ids: list[str] | None = None) -> dict:
    parts: list[object] = [
        {"content_type": "image_asset_pointer", "asset_pointer": f"file-service://{file_id}"}
        for file_id in file_ids
    ]
    parts.extend(f"sediment://{sediment_id}" for sediment_id in (sediment_ids or []))
    return {
        "mapping": {
            "tool": {
                "message": {
                    "author": {"role": "tool"},
                    "create_time": 1,
                    "metadata": {"async_task_type": "image_gen"},
                    "content": {"content_type": "multimodal_text", "parts": parts},
                }
            }
        }
    }


class FakeBackend(OpenAIBackendAPI):
    def __init__(self, conversations: list[dict] | None = None) -> None:
        self.conversations = conversations or []
        self.calls = 0
        self.file_urls: dict[str, str] = {}
        self.sediment_urls: dict[str, str] = {}

    def _get_conversation(self, conversation_id: str) -> dict:
        self.calls += 1
        index = min(self.calls - 1, len(self.conversations) - 1)
        return self.conversations[index]

    def _get_file_download_url(self, file_id: str) -> str:
        return self.file_urls.get(file_id, "")

    def _get_attachment_download_url(self, conversation_id: str, attachment_id: str) -> str:
        return self.sediment_urls.get(attachment_id, "")


class MultiImageResultTests(unittest.TestCase):
    def test_stream_id_extractor_keeps_full_file_ids(self) -> None:
        payload = (
            '{"conversation_id":"conv-1"} '
            'file-service://file-first_123-extra sediment://sed-second_456-extra'
        )

        conversation_id, file_ids, sediment_ids = extract_conversation_ids(payload)

        self.assertEqual(conversation_id, "conv-1")
        self.assertEqual(file_ids, ["file-first_123-extra"])
        self.assertEqual(sediment_ids, ["sed-second_456-extra"])

    def test_conversation_record_extractor_finds_all_generated_assets(self) -> None:
        backend = FakeBackend()
        conversation = {
            "mapping": {
                "user": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["file-service://file-user-input"]},
                    }
                },
                "tool": {
                    "message": {
                        "author": {"role": "tool"},
                        "create_time": 1,
                        "metadata": {
                            "async_task_type": "image_gen",
                            "nested": {"asset": "file-service://file-second"},
                        },
                        "content": {
                            "content_type": "text",
                            "parts": [
                                {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-first"},
                                "sediment://sed-first",
                            ],
                        },
                    }
                },
                "assistant": {
                    "message": {
                        "author": {"role": "assistant"},
                        "create_time": 2,
                        "metadata": {},
                        "content": {
                            "parts": [
                                {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-third"}
                            ]
                        },
                    }
                },
            }
        }

        records = backend._extract_image_tool_records(conversation)
        file_ids = [file_id for record in records for file_id in record["file_ids"]]
        sediment_ids = [sediment_id for record in records for sediment_id in record["sediment_ids"]]

        self.assertEqual(file_ids, ["file-first", "file-second", "file-third"])
        self.assertEqual(sediment_ids, ["sed-first"])

    def test_poll_waits_for_generated_asset_ids_to_settle(self) -> None:
        backend = FakeBackend([
            _conversation(["file-one"]),
            _conversation(["file-one", "file-two"], ["sed-one"]),
            _conversation(["file-one", "file-two"], ["sed-one"]),
        ])

        with (
            mock.patch.dict(config.data, {"image_poll_initial_wait_secs": 0, "image_poll_interval_secs": 0.5}),
            mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None),
        ):
            file_ids, sediment_ids = backend._poll_image_results("conv-1", timeout_secs=10)

        self.assertEqual(file_ids, ["file-one", "file-two"])
        self.assertEqual(sediment_ids, ["sed-one"])
        self.assertEqual(backend.calls, 3)

    def test_resolver_uses_file_and_sediment_urls(self) -> None:
        backend = FakeBackend()
        backend.file_urls = {"file-one": "https://files.test/one.png"}
        backend.sediment_urls = {
            "sed-one": "https://attachments.test/one.png",
            "sed-two": "https://attachments.test/two.png",
        }

        urls = backend._resolve_image_urls("conv-1", ["file-one"], ["sed-one", "sed-two"])

        self.assertEqual(urls, [
            "https://files.test/one.png",
            "https://attachments.test/one.png",
            "https://attachments.test/two.png",
        ])

    def test_resolver_raises_invalid_token_when_all_download_urls_fail_with_401(self) -> None:
        backend = FakeBackend()

        def raise_token_revoked(_file_id: str) -> str:
            raise UpstreamHTTPError(
                "/backend-api/files/file-one/download",
                401,
                {"error": {"code": "token_revoked", "message": "Encountered invalidated oauth token"}},
            )

        backend._get_file_download_url = raise_token_revoked  # type: ignore[method-assign]

        with self.assertRaises(InvalidAccessTokenError):
            backend._resolve_image_urls("conv-1", ["file-one"], [])

    def test_resolver_keeps_sediment_url_when_file_download_token_check_fails(self) -> None:
        backend = FakeBackend()
        backend.sediment_urls = {"sed-one": "https://attachments.test/one.png"}

        def raise_token_revoked(_file_id: str) -> str:
            raise UpstreamHTTPError(
                "/backend-api/files/file-one/download",
                401,
                {"error": {"code": "token_revoked", "message": "Encountered invalidated oauth token"}},
            )

        backend._get_file_download_url = raise_token_revoked  # type: ignore[method-assign]

        urls = backend._resolve_image_urls("conv-1", ["file-one"], ["sed-one"])

        self.assertEqual(urls, ["https://attachments.test/one.png"])

    def test_upstream_connect_reset_is_retryable_tls_error(self) -> None:
        self.assertTrue(is_tls_connection_error(
            "bootstrap failed: status=503, body=upstream connect error or disconnect/reset before headers. "
            "reset reason: connection termination"
        ))

    def test_resolver_keeps_stream_ids_when_poll_extension_fails(self) -> None:
        backend = FakeBackend()
        backend.file_urls = {"file-one": "https://files.test/one.png"}
        backend._get_conversation = mock.Mock(side_effect=RuntimeError("poll failed"))

        with mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None):
            urls = backend.resolve_conversation_image_urls("conv-1", ["file-one"], [], poll=True)

        self.assertEqual(urls, ["https://files.test/one.png"])

    def test_responses_stream_emits_all_image_output_items(self) -> None:
        first = base64.b64encode(b"first").decode("ascii")
        second = base64.b64encode(b"second").decode("ascii")
        events = list(stream_image_response(
            [ImageOutput(
                kind="result",
                model="gpt-image-2",
                index=1,
                total=1,
                data=[{"b64_json": first}, {"b64_json": second}],
            )],
            "draw two options",
            "gpt-image-2",
        ))

        done_events = [event for event in events if event.get("type") == "response.output_item.done"]
        completed = next(event["response"] for event in events if event.get("type") == "response.completed")

        self.assertEqual([event["output_index"] for event in done_events], [0, 1])
        self.assertEqual([item["result"] for item in completed["output"]], [first, second])

    def test_prefer_stream_for_multi_image_defaults_to_sse(self) -> None:
        self.assertTrue(prefer_stream_for_multi_image({"n": 2})["stream"])
        self.assertNotIn("stream", prefer_stream_for_multi_image({"n": 1}))
        self.assertFalse(prefer_stream_for_multi_image({"n": 2, "stream": False})["stream"])

    def test_parallel_generation_yields_as_each_future_completes(self) -> None:
        def fake_generate(_request: ConversationRequest, index: int, total: int) -> list[ImageOutput]:
            time.sleep(0.05 * (4 - index))
            return [ImageOutput(
                kind="result",
                model="gpt-image-2",
                index=index,
                total=total,
                data=[{"b64_json": f"img-{index}"}],
            )]

        request = ConversationRequest(prompt="draw", model="gpt-image-2", n=3)
        with (
            mock.patch.dict(config.data, {"image_parallel_generation": True}),
            mock.patch("services.protocol.conversation._generate_single_image", side_effect=fake_generate),
        ):
            outputs = [
                output
                for output in stream_image_outputs_with_pool(request)
                if output.kind == "result"
            ]

        self.assertEqual([output.index for output in outputs], [3, 2, 1])

    def test_poll_raises_invalid_access_token_on_401(self) -> None:
        backend = FakeBackend([_conversation([])])

        def raise_unauthorized(_conversation_id: str) -> dict:
            raise UpstreamHTTPError("/backend-api/conversation/x", 401, {"error": {"code": "token_revoked"}})

        backend._get_conversation = raise_unauthorized  # type: ignore[method-assign]

        with (
            mock.patch.dict(config.data, {"image_poll_initial_wait_secs": 0, "image_poll_interval_secs": 0}),
            mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None),
        ):
            with self.assertRaises(InvalidAccessTokenError):
                backend._poll_image_results("conv-1", timeout_secs=5)

    def test_edit_request_uses_longer_dynamic_poll_timeout(self) -> None:
        class PollRecordingBackend:
            def __init__(self) -> None:
                self.poll_timeout_secs: float | None = None

            def resolve_conversation_image_urls(self, _conversation_id, _file_ids, _sediment_ids, poll_timeout_secs=None):
                self.poll_timeout_secs = poll_timeout_secs
                return ["https://example.test/image.png"]

            def download_image_bytes(self, _urls):
                return [b"fake-image"]

        backend = PollRecordingBackend()

        with (
            mock.patch(
                "services.protocol.conversation.conversation_events",
                return_value=iter([
                    {
                        "type": "conversation.event",
                        "conversation_id": "conv-edit",
                        "file_ids": [],
                        "sediment_ids": [],
                        "turn_use_case": "image gen",
                    }
                ]),
            ),
            mock.patch.dict(config.data, {"image_task_queue": {"edit_poll_timeout_secs": 300}}),
        ):
            outputs = list(stream_image_outputs(
                backend,  # type: ignore[arg-type]
                ConversationRequest(prompt="edit", model="gpt-image-2", images=["aW1n"]),
            ))

        results = [output for output in outputs if output.kind == "result"]
        self.assertEqual(len(results), 1)
        self.assertGreaterEqual(float(backend.poll_timeout_secs or 0), 300.0)

    def test_pre_conversation_http2_internal_error_retry_is_limited(self) -> None:
        tokens = ["token-1", "token-2", "token-3"]
        token_iter = iter(tokens)
        attempts: list[str] = []

        def fake_stream_image_outputs(_backend, _request, _index, _total):
            attempts.append(getattr(_backend, "access_token", ""))
            raise RuntimeError("curl: (92) HTTP/2 stream 1 was not closed cleanly: INTERNAL_ERROR")

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token

        with (
            mock.patch.dict(
                config.data,
                {
                    "image_task_queue": {
                        "pre_conversation_max_attempts": 2,
                        "pre_conversation_retry_backoff_secs": 0,
                    }
                },
            ),
            mock.patch("services.protocol.conversation.OpenAIBackendAPI", FakeBackend),
            mock.patch("services.protocol.conversation.stream_image_outputs", side_effect=fake_stream_image_outputs),
            mock.patch("services.protocol.conversation.account_service.get_available_access_token", side_effect=lambda **_kw: next(token_iter)),
            mock.patch("services.protocol.conversation.account_service.get_account", return_value={}),
            mock.patch("services.protocol.conversation.account_service.mark_image_result"),
            mock.patch("services.protocol.conversation.account_service.record_image_transient_backoff") as transient_backoff,
        ):
            with self.assertRaises(ImageGenerationError):
                list(stream_image_outputs_with_pool(ConversationRequest(prompt="draw", model="gpt-image-2")))

        self.assertEqual(attempts, ["token-1", "token-2"])
        self.assertEqual(transient_backoff.call_count, 2)


if __name__ == "__main__":
    unittest.main()
