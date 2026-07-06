from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services.panda_staging_service import PandaStagingService


BASE_SETTINGS = {
    "enabled": True,
    "base_url": "https://panda.example",
    "auth_key": "secret",
    "batch_size": 20,
    "timeout_seconds": 60,
    "cooldown_seconds": 0,
    "staging_enabled": True,
    "probe_before_upload": True,
    "probe_schedule_minutes": [30, 120, 360],
    "low_probe_schedule_minutes": [10, 30, 90],
    "emergency_probe_schedule_minutes": [5, 15, 45],
    "probe_batch_limit": 100,
    "low_probe_batch_limit": 150,
    "emergency_probe_batch_limit": 200,
    "probe_concurrency": 4,
    "low_probe_concurrency": 6,
    "emergency_probe_concurrency": 8,
    "probe_cooldown_sec": 120,
    "low_probe_cooldown_sec": 60,
    "emergency_probe_cooldown_sec": 30,
    "watermark_enabled": True,
    "high_watermark": 1500,
    "low_watermark": 500,
    "emergency_watermark": 200,
    "upload_max_batch": 20,
    "low_upload_max_batch": 20,
    "emergency_upload_max_batch": 20,
    "sync_interval_minutes": 30,
    "low_sync_interval_sec": 60,
    "emergency_sync_interval_sec": 30,
    "public_import_min_interval_sec": 30,
    "public_import_max_batch_size": 20,
}


class FakeAccountService:
    def __init__(self, accounts):
        self.accounts = list(accounts)
        self.updated = []

    def list_accounts(self):
        return list(self.accounts)

    def update_account(self, token, updates, quiet=True):
        self.updated.append((token, dict(updates)))
        for account in self.accounts:
            if account.get("access_token") == token:
                account.update(updates)
                return account
        return None


class PandaStagingServiceTests(unittest.TestCase):
    def test_schedule_switches_by_remote_watermark(self) -> None:
        service = PandaStagingService()
        settings = dict(BASE_SETTINGS)

        self.assertEqual(service._supply_mode(settings, {"schedulable": 700}), "normal")
        self.assertEqual(service._probe_schedule_minutes(settings, "normal"), [30, 120, 360])
        self.assertEqual(service._supply_mode(settings, {"schedulable": 300}), "low")
        self.assertEqual(service._probe_schedule_minutes(settings, "low"), [10, 30, 90])
        self.assertEqual(service._supply_mode(settings, {"schedulable": 50}), "emergency")
        self.assertEqual(service._probe_schedule_minutes(settings, "emergency"), [5, 15, 45])

    def test_due_probe_uses_accelerated_schedule_before_stored_future_time(self) -> None:
        service = PandaStagingService()
        now = datetime.now(timezone.utc)
        account = {
            "access_token": "tok-due",
            "created_at": (now - timedelta(minutes=20)).isoformat(),
            "panda_sync_state": "staging",
            "panda_probe_count": 0,
            "panda_probe_next_at": (now + timedelta(hours=1)).isoformat(),
            "status": "正常",
            "quota": 1,
        }
        fake = FakeAccountService([account])
        settings = dict(BASE_SETTINGS)

        with patch("services.panda_staging_service.account_service", fake), \
             patch.object(service, "_settings", return_value=settings), \
             patch.object(service, "_remote_stats", return_value={"schedulable": 100}):
            self.assertEqual(service._due_probe_tokens(), ["tok-due"])

    def test_transient_backoff_future_time_is_not_accelerated(self) -> None:
        service = PandaStagingService()
        now = datetime.now(timezone.utc)
        account = {
            "access_token": "tok-backoff",
            "created_at": (now - timedelta(minutes=20)).isoformat(),
            "panda_sync_state": "staging",
            "panda_probe_count": 0,
            "panda_probe_next_at": (now + timedelta(minutes=30)).isoformat(),
            "panda_probe_last_error": "temporary network error",
            "status": "正常",
            "quota": 1,
        }
        fake = FakeAccountService([account])
        settings = dict(BASE_SETTINGS)

        with patch("services.panda_staging_service.account_service", fake), \
             patch.object(service, "_settings", return_value=settings), \
             patch.object(service, "_remote_stats", return_value={"schedulable": 100}):
            self.assertEqual(service._due_probe_tokens(), [])

    def test_low_watermark_upload_uses_fast_interval_and_public_batch_cap(self) -> None:
        service = PandaStagingService()
        now = datetime.now(timezone.utc)
        accounts = [
            {
                "access_token": f"tok-{i}",
                "created_at": now.isoformat(),
                "panda_sync_state": "ready",
                "panda_ready_at": now.isoformat(),
                "status": "正常",
                "quota": 1,
                "last_quota_refresh_at": now.isoformat(),
            }
            for i in range(50)
        ]
        fake = FakeAccountService(accounts)
        settings = dict(BASE_SETTINGS)
        settings["low_upload_max_batch"] = 80
        settings["public_import_max_batch_size"] = 20
        captured = {}

        def fake_sync(batch, options):
            captured["batch_len"] = len(batch)
            captured["option_batch_size"] = options.panda_sync_batch_size
            return len(batch), 0, 0

        with patch("services.panda_staging_service.account_service", fake), \
             patch.object(service, "_settings", return_value=settings), \
             patch.object(service, "_remote_stats", return_value={"schedulable": 300}), \
             patch("services.panda_staging_service.account_refresh_all_service._queue_or_sync_accounts_to_panda", side_effect=fake_sync):
            result = service._upload_ready_accounts()

        self.assertEqual(result, {"synced": 20, "failed": 0, "queued": 0})
        self.assertEqual(captured["batch_len"], 20)
        self.assertEqual(captured["option_batch_size"], 20)
        self.assertEqual(service.get_status()["last_upload"]["mode"], "low")


    def test_ready_backlog_prioritizes_upload_over_probe(self) -> None:
        service = PandaStagingService()
        settings = dict(BASE_SETTINGS)
        accounts = [
            {"access_token": f"tok-ready-{i}", "panda_sync_state": "ready", "status": "正常", "quota": 1}
            for i in range(25)
        ]
        fake = FakeAccountService(accounts)
        with patch("services.panda_staging_service.account_service", fake):
            self.assertTrue(service._should_prioritize_ready_upload(settings, {"schedulable": 50}))

    def test_probe_status_anonymizes_token(self) -> None:
        service = PandaStagingService()
        with patch.object(service, "_due_probe_tokens", return_value=["sk-1234567890abcdef"]), \
             patch.object(service, "_probe_one", return_value={"token": "sk-1234567890abcdef", "status": "ok", "deleted": 0}), \
             patch.object(service, "_settings", return_value=dict(BASE_SETTINGS)), \
             patch.object(service, "_remote_stats", return_value={"schedulable": 100}):
            service._run_due_probes()

        last_probe = service.get_status()["last_probe"]
        self.assertNotEqual(last_probe["token"], "sk-1234567890abcdef")
        self.assertTrue(last_probe["token"].startswith("token:"))


if __name__ == "__main__":
    unittest.main()

