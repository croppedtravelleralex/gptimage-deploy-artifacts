from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from services.image_asset_service import (
    ImageAssetNotFoundError,
    ImageAssetService,
    ImageAssetUploadWindowFullError,
)


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}
OTHER_OWNER = {"id": "owner-2", "name": "Other", "role": "user"}


class ImageAssetServiceTests(unittest.TestCase):
    def test_create_and_read_asset(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ImageAssetService(
                db_path=Path(tmp_dir) / "assets.db",
                root_dir=Path(tmp_dir) / "assets",
            )

            items = service.create_assets(OWNER, [(b"image-bytes", "ref.png", "image/png")])

            self.assertEqual(len(items), 1)
            asset_id = items[0]["asset_id"]
            self.assertEqual(items[0]["status"], "ready")
            self.assertEqual(items[0]["bytes"], len(b"image-bytes"))
            self.assertEqual(service.read_assets(OWNER, [asset_id]), [(b"image-bytes", "ref.png", "image/png")])
            self.assertEqual(service.get_asset(OWNER, asset_id)["asset_id"], asset_id)

    def test_asset_is_owner_scoped(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ImageAssetService(
                db_path=Path(tmp_dir) / "assets.db",
                root_dir=Path(tmp_dir) / "assets",
            )
            asset_id = service.create_assets(OWNER, [(b"image-bytes", "ref.png", "image/png")])[0]["asset_id"]

            with self.assertRaises(ImageAssetNotFoundError):
                service.read_assets(OTHER_OWNER, [asset_id])

    def test_upload_window_rejects_when_global_concurrency_is_full(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ImageAssetService(
                db_path=Path(tmp_dir) / "assets.db",
                root_dir=Path(tmp_dir) / "assets",
                upload_global_concurrency_getter=lambda: 1,
                upload_per_user_concurrency_getter=lambda: 1,
                upload_max_bytes_inflight_getter=lambda: 1024,
            )

            with service.reserve_upload_window(OWNER, 4):
                with self.assertRaises(ImageAssetUploadWindowFullError):
                    service.create_assets(OTHER_OWNER, [(b"next", "next.png", "image/png")])

    def test_upload_window_rejects_when_bytes_inflight_is_full(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ImageAssetService(
                db_path=Path(tmp_dir) / "assets.db",
                root_dir=Path(tmp_dir) / "assets",
                upload_global_concurrency_getter=lambda: 10,
                upload_per_user_concurrency_getter=lambda: 10,
                upload_max_bytes_inflight_getter=lambda: 8,
            )

            with service.reserve_upload_window(OWNER, 6):
                with self.assertRaises(ImageAssetUploadWindowFullError):
                    service.create_assets(OTHER_OWNER, [(b"abcd", "next.png", "image/png")])

    def test_cleanup_expired_removes_database_row_and_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ImageAssetService(
                db_path=Path(tmp_dir) / "assets.db",
                root_dir=Path(tmp_dir) / "assets",
            )
            asset_id = service.create_assets(OWNER, [(b"old", "old.png", "image/png")])[0]["asset_id"]
            path = service._get_row("owner-1", asset_id)["storage_path"]
            self.assertTrue(Path(path).exists())
            with service._connect() as conn:
                conn.execute(
                    "UPDATE image_reference_assets SET expires_ts=? WHERE asset_id=?",
                    (time.time() - 1, asset_id),
                )
                conn.commit()

            removed = service.cleanup_expired()

            self.assertEqual(removed, 1)
            self.assertFalse(Path(path).exists())
            with self.assertRaises(ImageAssetNotFoundError):
                service.read_assets(OWNER, [asset_id])


if __name__ == "__main__":
    unittest.main()
