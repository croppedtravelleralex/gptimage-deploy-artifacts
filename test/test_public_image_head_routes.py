from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from api.app import create_app
from services.config import config


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?"
    b"\x00\x05\xfe\x02\xfeA\xde\xfc\xd6\x00\x00\x00\x00IEND\xaeB`\x82"
)


class PublicImageHeadRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.relative_path = "test-head-routes/sample.png"
        self.image_path = config.images_dir / self.relative_path
        self.thumbnail_path = config.image_thumbnails_dir / f"{self.relative_path}.png"
        self.image_path.parent.mkdir(parents=True, exist_ok=True)
        self.image_path.write_bytes(PNG_BYTES)
        if self.thumbnail_path.exists():
            self.thumbnail_path.unlink()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        for path in (self.image_path, self.thumbnail_path):
            if path.exists():
                path.unlink()

    def test_public_image_head_returns_image_headers_not_spa_html(self) -> None:
        response = self.client.head(f"/images/{self.relative_path}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("content-type"), "image/png")
        self.assertEqual(response.headers.get("access-control-allow-origin"), "*")
        self.assertEqual(response.content, b"")

    def test_public_thumbnail_head_returns_image_headers_not_spa_html(self) -> None:
        response = self.client.head(f"/image-thumbnails/{self.relative_path}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("content-type"), "image/png")
        self.assertEqual(response.headers.get("access-control-allow-origin"), "*")
        self.assertEqual(response.content, b"")


if __name__ == "__main__":
    unittest.main()
