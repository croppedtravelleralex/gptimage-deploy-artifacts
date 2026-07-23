from __future__ import annotations

import unittest

from services.protocol.chatgpt_web_request import (
    build_chat_body,
    build_chat_headers,
    build_client_contextual_info,
    build_image_prepare_body,
    build_image_start_body,
    build_image_start_headers,
    oai_language_for_timezone,
    require_conduit_token,
    timezone_offset_min,
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
        self.assertEqual(headers["OpenAI-Sentinel-Chat-Requirements-Prepare-Token"], "req-token")
        self.assertEqual(headers["OpenAI-Sentinel-Proof-Token"], "proof")

    def test_chat_body_shape(self) -> None:
        body = build_chat_body([{"id": "1"}], "gpt-5", timezone="Asia/Shanghai")
        self.assertEqual(body["action"], "next")
        self.assertEqual(body["model"], "gpt-5")
        self.assertIn("client_contextual_info", body)
        self.assertEqual(body["system_hints"], [])
        self.assertTrue(body["history_and_training_disabled"])
        self.assertEqual(body["client_prepare_state"], "none")
        self.assertEqual(body["supported_encodings"], ["v1"])
        self.assertEqual(body["parent_message_id"], "client-created-root")
        self.assertNotIn("conversation_id", body)

    def test_chat_body_persist_and_continue(self) -> None:
        body = build_chat_body(
            [{"id": "1"}],
            "gpt-5",
            timezone="Asia/Singapore",
            history_and_training_disabled=False,
            conversation_id="conv-abc",
            parent_message_id="parent-xyz",
            contextual_jitter=False,
        )
        self.assertNotIn("history_and_training_disabled", body)
        self.assertEqual(body["conversation_id"], "conv-abc")
        self.assertEqual(body["parent_message_id"], "parent-xyz")
        self.assertEqual(body["timezone"], "Asia/Singapore")
        self.assertEqual(body["timezone_offset_min"], timezone_offset_min("Asia/Singapore"))
        self.assertEqual(body["force_parallel_switch"], "auto")

    def test_oai_language_and_contextual_jitter(self) -> None:
        self.assertEqual(oai_language_for_timezone("Asia/Shanghai"), "zh-CN")
        self.assertEqual(oai_language_for_timezone("Asia/Singapore"), "en-US")
        self.assertEqual(oai_language_for_timezone("Asia/Tokyo", "ja-JP,ja;q=0.9"), "ja-JP")
        a = build_client_contextual_info(seed="s1", jitter=True)
        b = build_client_contextual_info(seed="s1", jitter=True)
        self.assertEqual(a, b)
        c = build_client_contextual_info(seed="s2", jitter=True)
        self.assertNotEqual(a["time_since_loaded"], c["time_since_loaded"])

    def test_image_prepare_and_start(self) -> None:
        prepare = build_image_prepare_body(
            "a cat", "auto", timezone="Asia/Singapore", spa_tool_path=False
        )
        self.assertEqual(prepare["system_hints"], ["picture_v2"])
        self.assertEqual(prepare["timezone"], "Asia/Singapore")
        self.assertEqual(prepare["parent_message_id"], "client-created-root")
        self.assertEqual(prepare["client_prepare_state"], "sent")
        self.assertEqual(prepare["client_prepare_dispatch"], "immediate")
        self.assertEqual(prepare["client_prepare_source"], "context_change")
        self.assertEqual(prepare["partial_query"]["content"]["parts"], ["Create image"])
        self.assertNotIn("fork_from_shared_post", prepare)
        self.assertEqual(
            sorted(prepare["client_contextual_info"]),
            ["app_name", "has_web_push_capabilities", "web_push_notification_permission"],
        )
        start = build_image_start_body(
            "a cat", "auto", timezone="Asia/Singapore", spa_tool_path=False
        )
        self.assertEqual(start["system_hints"], ["picture_v2"])
        self.assertEqual(start["parent_message_id"], "client-created-root")
        self.assertEqual(start["client_prepare_state"], "none")
        self.assertEqual(start["messages"][0]["content"]["parts"], ["@Create image\u00a0a cat"])
        self.assertEqual(
            sorted(start["messages"][0]["metadata"]),
            ["serialization_metadata", "system_hints"],
        )
        self.assertEqual(
            start["messages"][0]["metadata"]["serialization_metadata"]["custom_symbol_offsets"],
            [
                {
                    "id": "picture_v2",
                    "symbol": "ecosystemMention",
                    "startIndex": 0,
                    "endIndex": 13,
                }
            ],
        )
        headers = build_image_start_headers(_Req(), "conduit-xyz", spa_tool_path=False)
        self.assertEqual(headers["X-Conduit-Token"], "conduit-xyz")

    def test_image_spa_tool_path_uses_proven_pure_http_envelope(self) -> None:
        legacy_context = {
            "app_name": "chatgpt.com",
            "is_web_push_capable": True,
            "is_web_push_enabled": False,
        }
        prepare = build_image_prepare_body(
            "a cat",
            "auto",
            timezone="Asia/Tokyo",
            spa_tool_path=True,
        )
        self.assertEqual(prepare["system_hints"], [])
        self.assertEqual(prepare["partial_query"]["content"]["parts"], ["a cat"])
        self.assertEqual(prepare["client_contextual_info"], legacy_context)

        start = build_image_start_body(
            "a cat",
            "auto",
            timezone="Asia/Tokyo",
            spa_tool_path=True,
        )
        self.assertEqual(start["system_hints"], [])
        self.assertEqual(start["messages"][0]["content"]["parts"], ["a cat"])
        self.assertNotIn("create_time", start["messages"][0])
        self.assertNotIn("metadata", start["messages"][0])
        self.assertEqual(start["client_contextual_info"], legacy_context)
        self.assertEqual(start["client_prepare_state"], "none")

        headers = build_image_start_headers(_Req(), "", spa_tool_path=True)
        self.assertNotIn("X-Conduit-Token", headers)
        self.assertNotIn("X-Oai-Turn-Trace-Id", headers)

    def test_text_prepare_body_spa_shape(self) -> None:
        from services.protocol.chatgpt_web_request import build_text_prepare_body

        body = build_text_prepare_body("hi", "auto", timezone="Asia/Tokyo")
        self.assertEqual(body["client_prepare_state"], "none")
        self.assertEqual(body["client_prepare_dispatch"], "debounced")
        self.assertEqual(body["supported_encodings"], ["v1"])
        self.assertEqual(body["system_hints"], [])
        self.assertEqual(body["partial_query"]["content"]["parts"], ["hi"])
        self.assertEqual(body["timezone"], "Asia/Tokyo")
        self.assertNotIn("history_and_training_disabled", body)

        with self.assertRaises(RuntimeError):
            require_conduit_token("")
        with self.assertRaises(RuntimeError):
            build_image_start_headers(_Req(), "", spa_tool_path=False)


if __name__ == "__main__":
    unittest.main()
