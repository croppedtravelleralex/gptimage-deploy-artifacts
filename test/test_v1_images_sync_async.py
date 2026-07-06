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

class ImageGenerationsAsyncTunnelTests(unittest.TestCase):
    def setUp(self):
        self.sync_calls = []

        def fake_run_generation_sync(identity, **kwargs):
            self.sync_calls.append(kwargs)
            return {"created": 1, "data": [{"b64_json": "ZmFrZQ=="}]}

        class FakeTaskService:
            def __init__(self):
                self.generation_calls = []
                self.edit_calls = []

            def submit_generation(self, identity, **kwargs):
                self.generation_calls.append(kwargs)
                return {
                    "id": kwargs["client_task_id"],
                    "status": "queued",
                    "mode": "generate",
                    "created_at": "2026-01-01 00:00:00",
                    "updated_at": "2026-01-01 00:00:00",
                }

            def submit_edit(self, identity, **kwargs):
                self.edit_calls.append(kwargs)
                return {
                    "id": kwargs["client_task_id"],
                    "status": "queued",
                    "mode": "edit",
                    "created_at": "2026-01-01 00:00:00",
                    "updated_at": "2026-01-01 00:00:00",
                }

            def list_tasks(self, identity, ids):
                return {
                    "items": [
                        {
                            "id": ids[0],
                            "status": "success",
                            "mode": "generate",
                            "created_at": "2026-01-01 00:00:00",
                            "updated_at": "2026-01-01 00:00:30",
                            "data": [{"b64_json": "b3V0"}],
                        }
                    ],
                    "missing_ids": [],
                }

        self.fake_service = FakeTaskService()
        self.sync_patcher = mock.patch.object(ai_module, "run_generation_sync", fake_run_generation_sync)
        self.task_patcher = mock.patch.object(ai_module, "image_task_service", self.fake_service)
        self.filter_patcher = mock.patch.object(ai_module, "filter_or_log", mock.AsyncMock())
        self.sync_patcher.start()
        self.task_patcher.start()
        self.filter_patcher.start()
        self.addCleanup(self.sync_patcher.stop)
        self.addCleanup(self.task_patcher.stop)
        self.addCleanup(self.filter_patcher.stop)
        app = FastAPI()
        app.include_router(ai_module.create_router())
        self.client = TestClient(app)

    def test_generation_panda_async_returns_task_without_waiting(self):
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2",
                "prompt": "异步猫",
                "client_task_id": "async-gen-1",
                "panda_async": True,
                "response_format": "b64_json",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["object"], "image.task")
        self.assertEqual(payload["task_id"], "async-gen-1")
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(len(self.fake_service.generation_calls), 1)
        self.assertEqual(self.sync_calls, [])
        self.assertEqual(self.fake_service.generation_calls[0]["response_format"], "b64_json")

    def test_generation_panda_task_id_returns_final_openai_response(self):
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2",
                "prompt": "poll",
                "panda_task_id": "async-gen-1",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["data"][0]["b64_json"], "b3V0")
        self.assertEqual(payload["task_id"], "async-gen-1")
        self.assertEqual(payload["panda_task"]["status"], "success")
        self.assertEqual(self.sync_calls, [])


    def test_generation_prompt_tunnel_async_survives_newapi_field_filtering(self):
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2",
                "prompt": "panda-async: 异步隧道猫",
                "client_task_id": "async-gen-prompt-1",
                "response_format": "url",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["object"], "image.task")
        self.assertEqual(payload["task_id"], "async-gen-prompt-1")
        self.assertEqual(self.fake_service.generation_calls[0]["prompt"], "异步隧道猫")
        self.assertEqual(self.sync_calls, [])

    def test_generation_prompt_tunnel_task_poll_survives_newapi_field_filtering(self):
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2",
                "prompt": "panda status async-gen-1",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["data"][0]["b64_json"], "b3V0")
        self.assertEqual(payload["task_id"], "async-gen-1")
        self.assertEqual(self.sync_calls, [])

    def test_generation_prompt_tunnel_task_error_does_not_return_top_level_error(self):
        def list_error_task(_identity, ids):
            return {
                "items": [
                    {
                        "id": ids[0],
                        "status": "error",
                        "mode": "generate",
                        "progress": "failed",
                        "created_at": "2026-01-01 00:00:00",
                        "updated_at": "2026-01-01 00:08:30",
                        "data": [],
                        "error": "image task hard timeout before upstream completion (510.0s); no conversation_id captured",
                    }
                ],
                "missing_ids": [],
            }

        self.fake_service.list_tasks = list_error_task

        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2",
                "prompt": "panda status async-error-1",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["object"], "image.task")
        self.assertEqual(payload["status"], "error")
        self.assertIn("panda_error", payload)
        self.assertNotIn("error", payload)

    def test_edit_panda_async_returns_task_without_sync_wait(self):
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            data={
                "model": "gpt-image-2",
                "prompt": "异步编辑",
                "client_task_id": "async-edit-1",
                "panda_async": "true",
                "response_format": "url",
            },
            files={"image": ("asset.txt", b"panda-asset://asset-a", "text/plain")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["object"], "image.task")
        self.assertEqual(payload["task_id"], "async-edit-1")
        self.assertEqual(len(self.fake_service.edit_calls), 1)
        self.assertEqual(self.fake_service.edit_calls[0]["image_asset_ids"], ["asset-a"])
