"""拟人化门禁回归：软熔断/429 不得永久卡死 status=限流。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.account_service import AccountService
from services.protocol.conversation import is_rate_limit_http_error


class SoftBandStatusTests(unittest.TestCase):
    def test_soft_cap_does_not_set_status_limited(self) -> None:
        svc = AccountService.__new__(AccountService)
        account = {
            "status": "正常",
            "maturity_stage": "mature",
            "limits_progress": [
                {"feature_name": "image_gen", "remaining": 5, "reset_after": "2026-07-19T00:00:00+00:00"}
            ],
            "image_gen_window_peak": 25,
            "image_gen_window_reset_at": "2026-07-19T00:00:00+00:00",
            "image_soft_band": 0.70,
        }
        with patch("services.config.config.get_scheduler_settings", return_value={
            "enabled": True,
            "daily_usage_ratio": 0.70,
            "new_account_usage_cap": 0.40,
        }):
            # remaining=5/peak=25 → used=0.8 ≥ 0.70 → soft_capped
            out = svc._apply_humanlike_quota_fields(dict(account))
        self.assertTrue(out.get("image_soft_capped"))
        self.assertEqual(out.get("status"), "正常")

    def test_soft_clear_heals_legacy_limited_status(self) -> None:
        svc = AccountService.__new__(AccountService)
        account = {
            "status": "限流",
            "image_soft_capped": True,
            "maturity_stage": "mature",
            "limits_progress": [
                {"feature_name": "image_gen", "remaining": 25, "reset_after": "2026-07-20T00:00:00+00:00"}
            ],
            "image_gen_window_peak": 5,
            "image_gen_window_reset_at": "2026-07-19T00:00:00+00:00",
            "image_soft_band": 0.70,
        }
        with patch("services.config.config.get_scheduler_settings", return_value={
            "enabled": True,
            "daily_usage_ratio": 0.70,
            "new_account_usage_cap": 0.40,
        }):
            out = svc._apply_humanlike_quota_fields(dict(account))
        self.assertFalse(out.get("image_soft_capped"))
        self.assertEqual(out.get("status"), "正常")

    def test_available_respects_soft_flag_not_false_limited(self) -> None:
        self.assertFalse(
            AccountService._is_image_account_available(
                {
                    "status": "正常",
                    "quota": 10,
                    "image_soft_capped": True,
                    "restore_at": "2099-01-01T00:00:00+00:00",
                }
            )
        )
        self.assertTrue(
            AccountService._is_image_account_available(
                {
                    "status": "正常",
                    "quota": 10,
                    "image_soft_capped": True,
                    "restore_at": "2020-01-01T00:00:00+00:00",
                }
            )
        )


class RateLimitMatchTests(unittest.TestCase):
    def test_status_429(self) -> None:
        self.assertTrue(is_rate_limit_http_error("oops", 429))

    def test_http_429_text(self) -> None:
        self.assertTrue(is_rate_limit_http_error("HTTP 429 Too Many Requests", 502))

    def test_bare_rate_limit_not_matched(self) -> None:
        self.assertFalse(is_rate_limit_http_error("hit account rate limit policy elsewhere", 502))
        self.assertFalse(is_rate_limit_http_error("upstream rate_limit soft hint", 400))

    def test_rate_limit_exceeded_matched(self) -> None:
        self.assertTrue(is_rate_limit_http_error("code=rate_limit_exceeded", 400))


if __name__ == "__main__":
    unittest.main()
