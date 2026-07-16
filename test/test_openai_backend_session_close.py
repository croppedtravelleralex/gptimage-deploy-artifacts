from __future__ import annotations

import unittest
from unittest import mock

from services.openai_backend_api import ImageStreamCancelledError, OpenAIBackendAPI
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest


class BackendSessionCloseTests(unittest.TestCase):
    def test_close_is_idempotent_and_shuts_down_stream_executor(self) -> None:
        backend = object.__new__(OpenAIBackendAPI)
        executor = mock.Mock()
        session = mock.Mock()
        session._executor = executor
        backend.session = session
        backend._closed = False

        backend.close()
        backend.close()

        session.close.assert_called_once_with()
        executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)


class StreamTextSessionCloseTests(unittest.TestCase):
    def test_stream_text_deltas_reuses_and_closes_owner_backend(self) -> None:
        class FakeBackend:
            def __init__(self, access_token: str = ""):
                self.access_token = access_token
                self.closed = False

            def close(self):
                self.closed = True

        owner_backend = FakeBackend("token-1")

        def fake_events(active_backend, **_kwargs):
            self.assertIs(active_backend, owner_backend)
            yield {"type": "conversation.delta", "delta": "ok"}

        with mock.patch.object(conversation, "OpenAIBackendAPI") as backend_factory, mock.patch.object(
            conversation,
            "conversation_events",
            side_effect=fake_events,
        ), mock.patch.object(conversation.account_service, "mark_text_used"):
            chunks = list(conversation.stream_text_deltas(owner_backend, ConversationRequest(model="auto")))

        self.assertEqual(chunks, ["ok"])
        self.assertTrue(owner_backend.closed)
        backend_factory.assert_not_called()

    def test_stream_text_deltas_creates_backend_only_after_token_switch(self) -> None:
        class FakeBackend:
            def __init__(self, access_token: str) -> None:
                self.access_token = access_token
                self.closed = False

            def close(self) -> None:
                self.closed = True

        owner_backend = FakeBackend("token-1")
        retry_backend = FakeBackend("token-2")
        seen_backends: list[FakeBackend] = []

        def fake_events(active_backend, **_kwargs):
            seen_backends.append(active_backend)
            if active_backend is owner_backend:
                raise RuntimeError("token invalidated")
            yield {"type": "conversation.delta", "delta": "retry-ok"}

        with mock.patch.object(
            conversation,
            "OpenAIBackendAPI",
            return_value=retry_backend,
        ) as backend_factory, mock.patch.object(
            conversation,
            "conversation_events",
            side_effect=fake_events,
        ), mock.patch.object(
            conversation.account_service,
            "refresh_access_token",
            return_value="token-2",
        ), mock.patch.object(conversation.account_service, "mark_text_used"):
            chunks = list(conversation.stream_text_deltas(owner_backend, ConversationRequest(model="auto")))

        self.assertEqual(chunks, ["retry-ok"])
        self.assertEqual(seen_backends, [owner_backend, retry_backend])
        backend_factory.assert_called_once_with(access_token="token-2")
        self.assertTrue(owner_backend.closed)
        self.assertTrue(retry_backend.closed)

    def test_stream_text_deltas_closes_backend_when_accounting_raises(self) -> None:
        class FakeBackend:
            def __init__(self, access_token: str) -> None:
                self.access_token = access_token
                self.closed = False

            def close(self) -> None:
                self.closed = True

        owner_backend = FakeBackend("token-1")

        def fake_events(_backend, **_kwargs):
            yield {"type": "conversation.delta", "delta": "ok"}

        with mock.patch.object(
            conversation,
            "conversation_events",
            side_effect=fake_events,
        ), mock.patch.object(
            conversation.account_service,
            "mark_text_used",
        ), mock.patch.object(
            conversation.account_service,
            "record_account_traffic",
            side_effect=RuntimeError("accounting failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "accounting failed"):
                list(conversation.stream_text_deltas(owner_backend, ConversationRequest(model="auto")))

        self.assertTrue(owner_backend.closed)


class ImageCancellationAccountingTests(unittest.TestCase):
    def test_cancelled_image_stream_leaves_slot_accounting_to_task_service(self) -> None:
        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token
                self.cancel_event = None
                self.progress_callback = None
                self.closed = False

            def close(self) -> None:
                self.closed = True

        backend = FakeBackend("token-1")
        request = ConversationRequest(prompt="cat", model="gpt-image-2")

        with mock.patch.object(conversation.account_service, "get_available_access_token", return_value="token-1"), mock.patch.object(
            conversation.account_service,
            "get_account",
            return_value={"email": "masked@example.test"},
        ), mock.patch.object(conversation, "OpenAIBackendAPI", return_value=backend), mock.patch.object(
            conversation,
            "stream_image_outputs",
            side_effect=ImageStreamCancelledError("image SSE stream cancelled"),
        ), mock.patch.object(conversation.account_service, "mark_image_result") as mark_result:
            with self.assertRaises(ImageStreamCancelledError):
                conversation._generate_single_image(request, 1, 1)

        mark_result.assert_not_called()
        self.assertTrue(backend.closed)


if __name__ == "__main__":
    unittest.main()
