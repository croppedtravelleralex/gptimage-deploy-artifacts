from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.account_service import AccountService
from services.config import config
from services.image_pipeline.aci_ranker import sort_tokens_by_aci
from services.storage.json_storage import JSONStorageBackend


class ImageQuotaSchedulingTests(unittest.TestCase):
    def test_aci_sort_skips_unconfirmed_quota(self) -> None:
        accounts = {
            "good": {
                "access_token": "good",
                "status": "正常",
                "quota": 8,
                "image_quota_unknown": False,
            },
            "unknown": {
                "access_token": "unknown",
                "status": "正常",
                "quota": 8,
                "image_quota_unknown": True,
            },
        }
        ranked = sort_tokens_by_aci(lambda token: accounts.get(token) or {}, ["unknown", "good"])
        self.assertEqual(ranked, ["good"])

    def test_zero_quota_not_schedulable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "zero",
                        "status": "正常",
                        "quota": 0,
                        "image_quota_unknown": False,
                        "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                    }
                ]
            )
            with patch.object(config, "get_image_pipeline_settings", return_value={"enabled": False}):
                self.assertFalse(service._is_image_account_schedulable(service.get_account("zero") or {}))
                self.assertEqual(service._list_ready_candidate_tokens(), [])

    def test_available_image_quota_excludes_unknown_and_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "ready",
                        "status": "正常",
                        "quota": 8,
                        "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                    },
                    {
                        "access_token": "unknown",
                        "status": "正常",
                        "quota": 8,
                        "image_quota_unknown": True,
                    },
                    {
                        "access_token": "stale",
                        "status": "正常",
                        "quota": 5,
                        "last_quota_refresh_at": "2000-01-01T00:00:00+00:00",
                    },
                ]
            )
            with patch.object(
                config,
                "get_image_pipeline_settings",
                return_value={"enabled": True, "require_quota_freshness": True},
            ):
                self.assertEqual(service.available_image_quota_for_account(service.get_account("ready") or {}), 8)
                self.assertEqual(service.available_image_quota_for_account(service.get_account("unknown") or {}), 0)
                self.assertEqual(service.available_image_quota_for_account(service.get_account("stale") or {}), 0)
                stats = service.get_stats()
                self.assertEqual(stats["available_image_quota"], 8)


if __name__ == "__main__":
    unittest.main()
