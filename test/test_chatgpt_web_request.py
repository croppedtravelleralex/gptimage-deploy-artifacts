from __future__ import annotations

import unittest

from services.protocol.chatgpt_web_request import (
    build_chat_body,
    build_chat_headers,
    build_image_prepare_body,
    build_image_start_body,
    build_image_start_headers,
    require_conduit_token,
)


class _Req:
    token = "req-token"
    proof_token = "proof"
    turnstile_token = ""
    so_token = "so"


class ChatgptWebRequestTests(unittest.TestCase):
    def test_chat_headers_include_sentinel(self) -> None:
        headers = build_chat_headers(_Req())
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertEqual(headers["OpenAI-Sentinel-Chat-Requirements-Token"], "req-token")
        self.assertEqual(headers["OpenAI-Sentinel-Proof-Token"], "proof")

    def test_chat_body_shape(self) -> None:
        body = build_chat_body([{"id": "1"}], "gpt-5", timezone="Asia/Shanghai")
        self.assertEqual(body["action"], "next")
        self.assertEqual(body["model"], "gpt-5")
        self.assertIn("client_contextual_info", body)
        self.assertEqual(body["system_hints"], [])

    def test_image_prepare_and_start(self) -> None:
        prepare = build_image_prepare_body("a cat", "auto")
        self.assertEqual(prepare["system_hints"], ["picture_v2"])
        start = build_image_start_body("a cat", "auto")
        self.assertEqual(start["system_hints"], ["picture_v2"])
        headers = build_image_start_headers(_Req(), "conduit-xyz")
        self.assertEqual(headers["X-Conduit-Token"], "conduit-xyz")

    def test_conduit_fail_fast(self) -> None:
        with self.assertRaises(RuntimeError):
            require_conduit_token("")
        with self.assertRaises(RuntimeError):
            build_image_start_headers(_Req(), "")


if __name__ == "__main__":
    unittest.main()
