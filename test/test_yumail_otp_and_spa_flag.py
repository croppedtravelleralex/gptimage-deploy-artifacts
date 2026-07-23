from __future__ import annotations

import json
import unittest
from unittest import mock

from services import yumail_otp


class YumailOtpClientTests(unittest.TestCase):
    def test_resolve_api_base_defaults_to_loopback_8782(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            # clear YUMAIL_API_BASE if set
            with mock.patch("services.yumail_otp.os.getenv", side_effect=lambda k, d=None: None if k == "YUMAIL_API_BASE" else __import__("os").getenv(k, d)):
                base = yumail_otp.resolve_api_base(None)
        self.assertTrue(base.startswith("http://127.0.0.1:8782"))
        self.assertTrue(base.endswith("/api/v1") or "/api/v1" in base)

    def test_resolve_api_base_appends_v1(self) -> None:
        self.assertEqual(yumail_otp.resolve_api_base("http://127.0.0.1:8782/api"), "http://127.0.0.1:8782/api/v1")

    def test_extract_openai_chinese_otp(self) -> None:
        code = yumail_otp._extract_otp_from_text("ChatGPT 验证码", "您的验证码是 654321")
        self.assertEqual(code, "654321")

    def test_wait_for_code_by_email_routes_outlook(self) -> None:
        with mock.patch.object(yumail_otp, "wait_for_outlook_otp", return_value="111222") as outlook:
            with mock.patch.object(yumail_otp, "wait_for_pool_otp") as pool:
                code = yumail_otp.wait_for_code_by_email("user@outlook.com", timeout_sec=30)
        self.assertEqual(code, "111222")
        outlook.assert_called_once()
        pool.assert_not_called()


class ChatgptSpaImageFlagTests(unittest.TestCase):
    def test_spa_flag_empties_system_hints_and_skips_conduit_header(self) -> None:
        from services.protocol.chatgpt_web_request import (
            build_image_prepare_body,
            build_image_start_body,
            build_image_start_headers,
        )

        class _Req:
            token = "t"
            proof_token = "p"
            turnstile_token = ""
            so_token = ""

        prepare = build_image_prepare_body("cat", "auto", spa_tool_path=True)
        start = build_image_start_body("cat", "auto", spa_tool_path=True)
        headers = build_image_start_headers(_Req(), "", spa_tool_path=True)
        self.assertEqual(prepare["system_hints"], [])
        self.assertEqual(start["system_hints"], [])
        self.assertNotIn("X-Conduit-Token", headers)
        self.assertIn("X-Oai-Turn-Trace-Id", headers)

        classic = build_image_prepare_body("cat", "auto", spa_tool_path=False)
        self.assertEqual(classic["system_hints"], ["picture_v2"])

    def test_backend_image_headers_spa_uses_build_start_headers(self) -> None:
        from services.openai_backend_api import OpenAIBackendAPI

        class _Req:
            token = "t"
            proof_token = "p"
            turnstile_token = ""
            so_token = ""

        api = object.__new__(OpenAIBackendAPI)
        api.access_token = "tok"
        api._headers = lambda path, headers: dict(headers)  # type: ignore[method-assign]
        headers = api._image_headers(
            "/backend-api/f/conversation",
            _Req(),
            "",
            "text/event-stream",
            spa_tool_path=True,
        )
        self.assertNotIn("X-Conduit-Token", headers)
        self.assertIn("X-Oai-Turn-Trace-Id", headers)
        self.assertEqual(headers.get("Accept"), "text/event-stream")


if __name__ == "__main__":
    unittest.main()
