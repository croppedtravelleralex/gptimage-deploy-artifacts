from __future__ import annotations

import unittest

from utils.helper import is_image_chat_request, looks_like_image_prompt


class ImageChatIntentTests(unittest.TestCase):
    def test_looks_like_image_prompt_zh(self) -> None:
        self.assertTrue(looks_like_image_prompt("生成图片，帅哥"))
        self.assertTrue(looks_like_image_prompt("帮我画一张猫"))
        self.assertFalse(looks_like_image_prompt("那么呢"))
        self.assertFalse(looks_like_image_prompt("解释一下量子力学"))

    def test_is_image_chat_request_by_intent(self) -> None:
        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": "生成图片，帅哥"}],
        }
        self.assertTrue(is_image_chat_request(body))

    def test_is_image_chat_request_text_only(self) -> None:
        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": "写一段自我介绍"}],
        }
        self.assertFalse(is_image_chat_request(body))


if __name__ == "__main__":
    unittest.main()
