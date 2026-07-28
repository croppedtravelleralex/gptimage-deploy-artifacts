"""Rust quota_prime 准入规则测试。"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from services.image_pipeline.binding_calendar import (
    _py_evaluate_prime,
    evaluate_prime_eligibility,
    engine_info,
    prime_account_input,
    prime_settings_input,
)


class QuotaPrimeRustTests(unittest.TestCase):
    def _account(self, **overrides: object) -> dict:
        base = {
            "access_token": "tok-1",
            "type": "Plus",
            "quota": 25,
            "success": 0,
            "status": "正常",
            "image_schedulable": True,
            "image_quota_unknown": False,
            "panda_sync_state": "synced",
            "panda_receive_state": "",
            "maturity_stage": "",
            "created_at": "2020-01-01T00:00:00+00:00",
        }
        base.update(overrides)
        return base

    def test_eligible_via_rust_or_python(self) -> None:
        account = self._account()
        now_unix = int(datetime(2026, 7, 28, tzinfo=timezone.utc).timestamp())
        payload = {
            "mode": "auto",
            "now_unix": now_unix,
            "settings": prime_settings_input(
                {
                    "enabled": True,
                    "full_quota": 25,
                    "min_account_age_days": 7,
                    "skip_panda_sync_states": ["staging", "ready"],
                }
            ),
            "account": prime_account_input(account),
        }
        result = evaluate_prime_eligibility(payload)
        self.assertTrue(result.get("eligible"))
        py = _py_evaluate_prime(payload)
        self.assertEqual(bool(result.get("eligible")), bool(py.get("eligible")))

    def test_rejects_staging(self) -> None:
        account = self._account(panda_sync_state="staging")
        payload = {
            "mode": "auto",
            "now_unix": int(datetime(2026, 7, 28, tzinfo=timezone.utc).timestamp()),
            "settings": prime_settings_input({"enabled": True}),
            "account": prime_account_input(account),
        }
        result = evaluate_prime_eligibility(payload)
        self.assertFalse(result.get("eligible"))
        self.assertEqual(result.get("reason"), "panda_sync")

    def test_force_bypasses_new_account(self) -> None:
        account = self._account(created_at="2026-07-27T00:00:00+00:00")
        now_unix = int(datetime(2026, 7, 28, tzinfo=timezone.utc).timestamp())
        payload = {
            "mode": "force",
            "now_unix": now_unix,
            "settings": prime_settings_input({"enabled": True}),
            "account": prime_account_input(account),
        }
        result = evaluate_prime_eligibility(payload)
        self.assertTrue(result.get("eligible"))
        if engine_info().get("engine") == "rust":
            self.assertEqual(result.get("reason"), "force")


if __name__ == "__main__":
    unittest.main()
