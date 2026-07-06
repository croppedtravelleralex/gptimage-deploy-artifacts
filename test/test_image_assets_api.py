from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.image_assets as image_assets_module
from services.image_asset_service import ImageAssetService, ImageAssetUploadWindowFullError


AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}
PNG_BYTES = b"\x89PNG\r\n\x1a\n"
DATA_IMAGE_URL = f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode('ascii')}"


class ImageAssetsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.asset_service = ImageAssetService(
            db_path=Path(self.tmp.name) / "assets.db",
            root_dir=Path(self.tmp.name) / "assets",
        )
        self.service_patcher = mock.patch.object(image_assets_module, "image_asset_service", self.asset_service)
        self.service_patcher.start()
        self.addCleanup(self.service_patcher.stop)
        self.identity_patcher = mock.patch.object(
            image_assets_module,
            "require_identity",
            return_value={"id": "test-key", "name": "test", "role": "admin"},
        )
        self.identity_patcher.start()
        self.addCleanup(self.identity_patcher.stop)
        app = FastAPI()
        app.include_router(image_assets_module.create_router())
        self.client = TestClient(app)

    def test_create_reference_asset_from_json_data_url(self):
        response = self.client.post(
            "/api/image-assets/references",
            headers=AUTH_HEADERS,
            json={"images": [{"image_url": DATA_IMAGE_URL}]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["bytes"], len(PNG_BYTES))
        self.assertEqual(self.asset_service.read_assets({"id": "test-key"}, [item["asset_id"]])[0][0], PNG_BYTES)

    def test_create_reference_asset_from_multipart(self):
        response = self.client.post(
            "/api/image-assets/references",
            headers=AUTH_HEADERS,
            files={"image": ("one.png", b"one", "image/png")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        self.assertEqual(item["filename"], "one.png")
        self.assertEqual(self.asset_service.read_assets({"id": "test-key"}, [item["asset_id"]])[0][0], b"one")

    def test_create_reference_asset_returns_429_when_upload_window_is_full(self):
        with mock.patch.object(
            self.asset_service,
            "create_assets",
            side_effect=ImageAssetUploadWindowFullError("image reference upload window is full", retry_after_secs=7),
        ):
            response = self.client.post(
                "/api/image-assets/references",
                headers=AUTH_HEADERS,
                files={"image": ("one.png", b"one", "image/png")},
            )

        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.headers.get("retry-after"), "7")
        self.assertIn("upload window", response.text)


if __name__ == "__main__":
    unittest.main()
