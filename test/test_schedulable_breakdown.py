from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.account_service import AccountService


def _acct(**kwargs):
    base = {
        "access_token": "tok",
        "email": "a@example.com",
        "status": "正常",
        "quota": 10,
        "panda_receive_state": "verified_ready",
        "last_quota_refresh_at": "2026-07-19T00:00:00+00:00",
    }
    base.update(kwargs)
    return base


class SchedulableBreakdownTests(unittest.TestCase):
    def test_breakdown_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService.__new__(AccountService)
            service._lock = __import__("threading").RLock()
            service._image_slot_condition = __import__("threading").Condition(service._lock)
            service._accounts = {
                "t1": _acct(access_token="t1", email="ok@ex.com"),
                "t2": _acct(access_token="t2", email="bad@ex.com", status="异常"),
                "t3": _acct(
                    access_token="t3",
                    email="iso@ex.com",
                    panda_receive_state="identity_isolated",
                ),
                "t4": _acct(
                    access_token="t4",
                    email="taint@ex.com",
                    invalid_count=1,
                ),
            }
            service._image_inflight = {}
            service._image_preflight_failed_until = {}
            with patch("services.account_service.config") as cfg:
                cfg.image_require_recent_quota_refresh = False
                cfg.image_account_concurrency = 1
                cfg.image_global_concurrency = 0
                cfg.image_global_queue_timeout_secs = 0
                cfg.get_scheduler_settings.return_value = {"enabled": False}
                breakdown = service.get_schedulable_breakdown()
            buckets = breakdown["buckets"]
            self.assertEqual(breakdown["total"], 4)
            self.assertGreaterEqual(buckets["schedulable"], 1)
            self.assertGreaterEqual(buckets["excluded_by_status"], 1)
            self.assertGreaterEqual(buckets["excluded_by_receive_state"], 1)
            self.assertGreaterEqual(buckets["excluded_by_failure_evidence"], 1)
            self.assertIn("primary_reason_counts", breakdown)
            self.assertIn("runtime", breakdown)


if __name__ == "__main__":
    unittest.main()
