"""Unit tests for multimodal search message building."""

from __future__ import annotations

import unittest

from services.openai_backend_api import OpenAIBackendAPI


class SearchMultimodalMessageTests(unittest.TestCase):
    def test_text_only_message(self) -> None:
        msg = OpenAIBackendAPI._build_search_user_message("hello search", [])
        self.assertEqual(msg["content"]["content_type"], "text")
        self.assertEqual(msg["content"]["parts"], ["hello search"])
        self.assertIn("search", msg["metadata"]["system_hints"])

    def test_multimodal_message_with_uploaded_images(self) -> None:
        uploaded = [
            {
                "file_id": "file-abc",
                "file_name": "search_1.png",
                "file_size": 12,
                "mime_type": "image/png",
                "width": 10,
                "height": 8,
            }
        ]
        msg = OpenAIBackendAPI._build_search_user_message("看图搜一下", uploaded)
        self.assertEqual(msg["content"]["content_type"], "multimodal_text")
        parts = msg["content"]["parts"]
        self.assertEqual(parts[0]["content_type"], "image_asset_pointer")
        self.assertEqual(parts[0]["asset_pointer"], "file-service://file-abc")
        self.assertEqual(parts[-1], "看图搜一下")
        self.assertEqual(msg["metadata"]["attachments"][0]["id"], "file-abc")

    def test_extract_search_prefers_text_over_code_stub(self) -> None:
        api = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        conversation = {
            "mapping": {
                "1": {
                    "message": {
                        "id": "code-1",
                        "author": {"role": "assistant"},
                        "create_time": 200.0,
                        "content": {"content_type": "code", "text": 'search("hello")'},
                        "metadata": {},
                    }
                },
                "2": {
                    "message": {
                        "id": "text-1",
                        "author": {"role": "assistant"},
                        "create_time": 100.0,
                        "content": {"content_type": "text", "parts": ["主色是浅灰。"]},
                        "metadata": {"finish_details": {"type": "stop"}},
                    }
                },
            }
        }
        result = api._extract_search_result("cid", conversation)
        self.assertEqual(result["answer"], "主色是浅灰。")
        self.assertEqual(result["assistant_message_id"], "text-1")
        self.assertEqual(result["status"], "stop")

    def test_vision_local_prompt_heuristic(self) -> None:
        self.assertTrue(OpenAIBackendAPI._is_vision_local_search_prompt("这张图主色大概是什么？"))
        self.assertTrue(OpenAIBackendAPI._is_vision_local_search_prompt("描述一下图片里有什么"))
        self.assertFalse(OpenAIBackendAPI._is_vision_local_search_prompt("这张图是什么牌子，帮我搜索官网"))
        self.assertFalse(OpenAIBackendAPI._is_vision_local_search_prompt("OpenAI 是什么公司？"))

    def test_search_completion_chunk_includes_sources_on_finish(self) -> None:
        chunk = OpenAIBackendAPI._search_completion_chunk(
            "gpt-5-5",
            completion_id="chatcmpl-x",
            created=1,
            content="你好",
            role="assistant",
            finish_reason="stop",
            sources=[{"title": "a", "url": "https://example.com"}],
        )
        self.assertEqual(chunk["choices"][0]["delta"]["content"], "你好")
        self.assertEqual(chunk["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chunk["sources"][0]["url"], "https://example.com")


if __name__ == "__main__":
    unittest.main()
