from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "chatgpt2api")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.ai as ai_module
import api.image_tasks as image_tasks_module
from services.image_request_validation import (
    ImageRequestValidationError,
    prompt_requires_reference_image,
    validate_generation_request,
)
from services.image_task_service import ImageTaskService

AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}


class PromptReferenceDetectionTests(unittest.TestCase):
    def test_detects_reference_dependent_prompt(self) -> None:
        prompt = "生成街景，监控视角，女孩走在街道上，基于人物参考图做身份保持拼贴。"
        self.assertTrue(prompt_requires_reference_image(prompt))

    def test_allows_plain_generation_prompt(self) -> None:
        self.assertFalse(prompt_requires_reference_image("南京夜景海报，赛博朋克风格"))

    def test_validate_generation_rejects_missing_reference(self) -> None:
        with self.assertRaises(ImageRequestValidationError) as caught:
            validate_generation_request("请基于人物参考图生成监控风格拼贴。")
        self.assertEqual(caught.exception.code, "missing_reference_image")

    def test_validate_generation_allows_when_assets_present(self) -> None:
        validate_generation_request(
            "请基于人物参考图生成监控风格拼贴。",
            image_asset_ids=["asset-1"],
        )


class GenerationSubmitValidationTests(unittest.TestCase):
    def test_submit_generation_rejects_reference_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ImageTaskService(
                Path(tmp_dir) / "image_tasks.db",
                generation_handler=lambda _payload: {"data": [{"url": "http://example.test/image.png"}]},
                edit_handler=lambda _payload: {"data": [{"url": "http://example.test/edit.png"}]},
            )
            self.addCleanup(service.stop)
            with self.assertRaises(ImageRequestValidationError):
                service.submit_generation(
                    {"owner_id": "u1"},
                    client_task_id="task-ref-missing",
                    prompt="基于人物参考图生成监控风格街景。",
                    model="gpt-image-2",
                    size="1024x1024",
                )


class GenerationApiValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_image_generation_paused = ai_module.config.data.get("image_generation_paused")
        self._original_image_task_queue = ai_module.config.data.get("image_task_queue")
        ai_module.config.data["image_generation_paused"] = False
        ai_module.config.data["image_task_queue"] = {
            **(self._original_image_task_queue if isinstance(self._original_image_task_queue, dict) else {}),
            "enabled": True,
        }
        self.filter_patcher = mock.patch.object(ai_module, "filter_or_log", mock.AsyncMock())
        self.sync_patcher = mock.patch.object(
            ai_module,
            "run_generation_sync",
            mock.Mock(return_value={"created": 1, "data": [{"b64_json": "ZmFrZQ=="}]}),
        )
        self.filter_patcher.start()
        self.sync_patcher.start()
        app = FastAPI()
        app.include_router(ai_module.create_router())
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.filter_patcher.stop()
        self.sync_patcher.stop()
        if self._original_image_generation_paused is None:
            ai_module.config.data.pop("image_generation_paused", None)
        else:
            ai_module.config.data["image_generation_paused"] = self._original_image_generation_paused
        if self._original_image_task_queue is None:
            ai_module.config.data.pop("image_task_queue", None)
        else:
            ai_module.config.data["image_task_queue"] = self._original_image_task_queue

    def test_generations_endpoint_rejects_reference_prompt_before_upstream(self) -> None:
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2",
                "prompt": "基于人物参考图生成监控风格街景。",
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "missing_reference_image")
        ai_module.run_generation_sync.assert_not_called()

    def test_stream_generation_rejects_reference_prompt_before_handler(self) -> None:
        with mock.patch.object(ai_module.openai_v1_image_generations, "handle", return_value={"created": 1, "data": []}) as handle:
            response = self.client.post(
                "/v1/images/generations",
                headers=AUTH_HEADERS,
                json={
                    "model": "gpt-image-2",
                    "prompt": "基于人物参考图生成监控风格街景。",
                    "stream": True,
                },
            )
        self.assertEqual(response.status_code, 400, response.text)
        handle.assert_not_called()

    def test_image_tasks_generations_rejects_reference_prompt(self) -> None:
        app = FastAPI()
        app.include_router(image_tasks_module.create_router())
        client = TestClient(app)
        with mock.patch.object(image_tasks_module, "filter_or_log", mock.AsyncMock()):
            with mock.patch.object(image_tasks_module.image_task_service, "submit_generation") as submit:
                response = client.post(
                    "/api/image-tasks/generations",
                    headers=AUTH_HEADERS,
                    json={
                        "client_task_id": "task-1",
                        "prompt": "基于人物参考图生成监控风格街景。",
                        "model": "gpt-image-2",
                    },
                )
        self.assertEqual(response.status_code, 400, response.text)
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
