"""Tests for sentinel validation preflight + concurrent account picking."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from scripts.sentinel_ticket_validation_suite import pick_concurrent_accounts, preflight_image_account


class PickConcurrentAccountsTests(unittest.TestCase):
    def test_unique_egress_skips_shared_ip(self) -> None:
        accounts = [
            {"email": "a@x.com", "proxy": "http://u:p@1.2.3.4:1", "proxy_egress_ip": "1.2.3.4", "_quota": 10},
            {"email": "b@x.com", "proxy": "http://u:p@1.2.3.4:2", "proxy_egress_ip": "1.2.3.4", "_quota": 9},
            {"email": "c@x.com", "proxy": "http://u:p@5.6.7.8:1", "proxy_egress_ip": "5.6.7.8", "_quota": 8},
        ]
        picked = pick_concurrent_accounts(accounts, 2, unique_egress=True)
        self.assertEqual(len(picked), 2)
        egress = {str(a.get("proxy_egress_ip")) for a in picked}
        self.assertEqual(len(egress), 2)

    def test_without_unique_takes_top_quota(self) -> None:
        accounts = [
            {"email": "a@x.com", "proxy": "http://a", "_quota": 5},
            {"email": "b@x.com", "proxy": "http://b", "_quota": 20},
        ]
        picked = pick_concurrent_accounts(accounts, 1, unique_egress=False)
        self.assertEqual(picked[0]["email"], "b@x.com")


class PreflightImageAccountTests(unittest.TestCase):
    @patch("services.account_service.account_service")
    def test_rejects_stale_quota(self, svc: MagicMock) -> None:
        svc.fetch_remote_info.return_value = {
            "email": "u@x.com",
            "quota": 3,
            "access_token": "tok",
        }
        svc.image_quota_state.return_value = "stale"
        svc.available_image_quota_for_account.return_value = 0
        from pathlib import Path

        with self.assertRaises(RuntimeError) as ctx:
            preflight_image_account("tok", label="t", log_path=Path("/tmp/x.jsonl"))
        self.assertIn("quota_state=stale", str(ctx.exception))

    @patch("services.account_service.account_service")
    def test_accepts_ready(self, svc: MagicMock) -> None:
        svc.fetch_remote_info.return_value = {
            "email": "u@x.com",
            "quota": 3,
            "last_quota_refresh_at": "2026-07-24T00:00:00+00:00",
        }
        svc.image_quota_state.return_value = "ready"
        svc.available_image_quota_for_account.return_value = 3
        from pathlib import Path

        info = preflight_image_account("tok", label="t", log_path=Path("/tmp/x.jsonl"))
        self.assertEqual(info["quota_state"], "ready")


if __name__ == "__main__":
    unittest.main()
