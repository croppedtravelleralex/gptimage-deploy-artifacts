from __future__ import annotations

import queue
import threading
import time
import unittest
from unittest import mock
from concurrent.futures import Future

from curl_cffi.requests.models import STREAM_END

from services.config import config
from services.openai_backend_api import (
    ImageStreamCancelledError,
    OpenAIBackendAPI,
    iter_sse_payloads_until_first_payload,
)
from services.protocol.conversation import is_pre_conversation_transient_error


class FakeSseResponse:
    def __init__(self, lines: list[bytes], delay_secs: float = 0.0) -> None:
        self._lines = lines
        self._delay_secs = delay_secs

    def iter_lines(self):
        for line in self._lines:
            if self._delay_secs > 0:
                time.sleep(self._delay_secs)
            yield line


class FakeCurlStreamResponse:
    """模拟 curl_cffi 0.15 的 queue-backed stream response。"""

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.queue: queue.Queue[object] = queue.Queue()
        for chunk in chunks or []:
            self.queue.put(chunk)
        self.quit_now = threading.Event()
        self.stream_task: Future[None] = Future()
        self.stream_task.set_result(None)
        self._stream_closed = False

    def iter_lines(self):
        raise AssertionError("queue-backed curl stream must not call blocking iter_lines()")

    def close(self) -> None:
        self.quit_now.set()
        self._stream_closed = True


class ImagePreConversationTimeoutTests(unittest.TestCase):
    def test_first_payload_deadline_rejects_blank_heartbeat_stream(self) -> None:
        response = FakeSseResponse([b"", b"", b""], delay_secs=0.02)

        with self.assertRaisesRegex(TimeoutError, "first payload timeout"):
            list(iter_sse_payloads_until_first_payload(response, 0.03))

    def test_first_payload_deadline_allows_valid_data_payload(self) -> None:
        response = FakeSseResponse([b"", b'data: {"conversation_id":"abc"}'], delay_secs=0.0)

        payloads = list(iter_sse_payloads_until_first_payload(response, 1.0))

        self.assertEqual(payloads, ['{"conversation_id":"abc"}'])

    def test_queue_backed_stream_deadline_does_not_block_on_response_close(self) -> None:
        response = FakeCurlStreamResponse()
        started = time.monotonic()

        with self.assertRaisesRegex(TimeoutError, "first payload timeout"):
            list(iter_sse_payloads_until_first_payload(response, 0.03))

        self.assertLess(time.monotonic() - started, 0.3)
        self.assertTrue(response.quit_now.is_set())
        self.assertTrue(response._stream_closed)

    def test_queue_backed_stream_parses_payload_split_across_chunks(self) -> None:
        response = FakeCurlStreamResponse([b"da", b'ta: {"conversation_id":"abc"}\r\n', STREAM_END])

        payloads = list(iter_sse_payloads_until_first_payload(response, 1.0))

        self.assertEqual(payloads, ['{"conversation_id":"abc"}'])

    def test_control_data_does_not_satisfy_conversation_ready_deadline(self) -> None:
        response = FakeCurlStreamResponse([b'data: {"type":"ping"}\n'])

        with self.assertRaisesRegex(TimeoutError, "conversation metadata timeout"):
            list(
                iter_sse_payloads_until_first_payload(
                    response,
                    0.03,
                    ready_predicate=lambda payload: "conversation_id" in payload,
                )
            )

        self.assertTrue(response.quit_now.is_set())

    def test_post_conversation_deadline_soft_handoffs_stalled_stream_for_polling(self) -> None:
        response = FakeCurlStreamResponse([b'data: {"conversation_id":"conv-1"}\n'])
        started = time.monotonic()

        payloads = list(
            iter_sse_payloads_until_first_payload(
                response,
                1.0,
                ready_predicate=lambda payload: "conversation_id" in payload,
                post_ready_timeout_secs=0.03,
            )
        )

        self.assertEqual(payloads, ['{"conversation_id":"conv-1"}'])
        self.assertLess(time.monotonic() - started, 0.3)
        # conversation_id 已拿到后只能软交接给 poll；quit_now 会中止仍在
        # 服务端执行的 image tool，导致永远轮询不到附件。
        self.assertFalse(response.quit_now.is_set())

    def test_post_conversation_deadline_is_not_extended_by_control_heartbeats(self) -> None:
        response = FakeCurlStreamResponse([b'data: {"conversation_id":"conv-1"}\n'])
        stop = threading.Event()

        def feed_control_heartbeats() -> None:
            while not stop.wait(0.005):
                response.queue.put(b'data: {"type":"ping"}\n')

        feeder = threading.Thread(target=feed_control_heartbeats, daemon=True)
        feeder.start()
        started = time.monotonic()
        try:
            payloads = list(
                iter_sse_payloads_until_first_payload(
                    response,
                    1.0,
                    ready_predicate=lambda payload: "conversation_id" in payload,
                    post_ready_timeout_secs=0.03,
                )
            )
        finally:
            stop.set()
            feeder.join(timeout=0.2)

        self.assertGreaterEqual(len(payloads), 1)
        self.assertEqual(payloads[0], '{"conversation_id":"conv-1"}')
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertFalse(response.quit_now.is_set())

    def test_cancel_event_aborts_queue_backed_stream(self) -> None:
        response = FakeCurlStreamResponse()
        cancel_event = threading.Event()
        timer = threading.Timer(0.02, cancel_event.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(ImageStreamCancelledError, "cancelled"):
                list(
                    iter_sse_payloads_until_first_payload(
                        response,
                        1.0,
                        cancel_event=cancel_event,
                    )
                )
        finally:
            timer.cancel()

        self.assertLess(time.monotonic() - started, 0.3)
        self.assertTrue(response.quit_now.is_set())

    def test_picture_stream_reports_conversation_id_with_own_access_token(self) -> None:
        response = FakeCurlStreamResponse([b'data: {"conversation_id":"conv-1"}\n', STREAM_END])
        progress: list[object] = []
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "token-1"
        backend.account = {}
        backend.cancel_event = None
        backend.progress_callback = progress.append
        backend._report_progress = lambda _step: None
        backend._open_image_sse_with_cf_retry = lambda prompt, model, references, **kwargs: response

        payloads = list(backend._stream_picture_conversation("cat", "gpt-image-2", []))

        self.assertEqual(payloads, ['{"conversation_id":"conv-1"}'])
        self.assertIn(
            {
                "step": "conversation_id_captured",
                "conversation_id": "conv-1",
                "access_token": "token-1",
            },
            progress,
        )

    def test_cancel_event_interrupts_image_poll_sleep(self) -> None:
        backend = object.__new__(OpenAIBackendAPI)
        backend.cancel_event = threading.Event()
        timer = threading.Timer(0.02, backend.cancel_event.set)
        timer.start()
        started = time.monotonic()
        try:
            with mock.patch.dict(
                config.data,
                {"image_poll_initial_wait_secs": 0.2, "image_poll_interval_secs": 0.2},
            ):
                with self.assertRaisesRegex(ImageStreamCancelledError, "cancelled"):
                    backend._poll_image_results("conv-1", timeout_secs=1.0)
        finally:
            timer.cancel()

        self.assertLess(time.monotonic() - started, 0.15)

    def test_first_payload_deadline_is_retryable_pre_conversation_failure(self) -> None:
        self.assertTrue(
            is_pre_conversation_transient_error(
                "image pre-conversation SSE first payload timeout after 45s"
            )
        )


if __name__ == "__main__":
    unittest.main()
