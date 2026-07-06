from __future__ import annotations

import time
import unittest

from services.openai_backend_api import iter_sse_payloads_until_first_payload


class FakeSseResponse:
    def __init__(self, lines: list[bytes], delay_secs: float = 0.0) -> None:
        self._lines = lines
        self._delay_secs = delay_secs

    def iter_lines(self):
        for line in self._lines:
            if self._delay_secs > 0:
                time.sleep(self._delay_secs)
            yield line


class ImagePreConversationTimeoutTests(unittest.TestCase):
    def test_first_payload_deadline_rejects_blank_heartbeat_stream(self) -> None:
        response = FakeSseResponse([b"", b"", b""], delay_secs=0.02)

        with self.assertRaisesRegex(TimeoutError, "first payload timeout"):
            list(iter_sse_payloads_until_first_payload(response, 0.03))

    def test_first_payload_deadline_allows_valid_data_payload(self) -> None:
        response = FakeSseResponse([b"", b'data: {"conversation_id":"abc"}'], delay_secs=0.0)

        payloads = list(iter_sse_payloads_until_first_payload(response, 1.0))

        self.assertEqual(payloads, ['{"conversation_id":"abc"}'])


if __name__ == "__main__":
    unittest.main()
