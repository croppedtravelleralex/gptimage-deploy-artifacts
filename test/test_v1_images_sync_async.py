from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "chatgpt2api")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.ai as ai_module

AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}


class ImageGenerationsSyncAsyncTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_run_generation_sync(identity, **kwargs):
            self.calls.append(kwargs)
            return {"created": 1, "data": [{"b64_json": "ZmFrZQ=="}], "usage": {"total_tokens": 1}}

        self.sync_patcher = mock.patch.object(ai_module, "run_generation_sync", fake_run_generation_sync)
        self.filter_patcher = mock.patch.object(ai_module, "filter_or_log", mock.AsyncMock())
        self.sync_patcher.start()
        self.filter_patcher.start()
        self.addCleanup(self.sync_patcher.stop)
        self.addCleanup(self.filter_patcher.stop)

        app = FastAPI()
        app.include_router(ai_module.create_router())
        self.client = TestClient(app)

    def test_non_stream_generation_uses_sync_over_async(self):
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2",
                "prompt": "南京海报",
                "n": 1,
                "response_format": "b64_json",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.calls[0]["prompt"], "南京海报")
        self.assertEqual(self.calls[0]["response_format"], "b64_json")
        self.assertEqual(response.json()["data"][0]["b64_json"], "ZmFrZQ==")

    def test_stream_generation_keeps_direct_handler(self):
        with mock.patch.object(ai_module.openai_v1_image_generations, "handle", return_value={"created": 1, "data": []}) as handle:
            response = self.client.post(
                "/v1/images/generations",
                headers=AUTH_HEADERS,
                json={
                    "model": "gpt-image-2",
                    "prompt": "流式",
                    "stream": True,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.calls), 0)
        handle.assert_called_once()


if __name__ == "__main__":
    unittest.main()
