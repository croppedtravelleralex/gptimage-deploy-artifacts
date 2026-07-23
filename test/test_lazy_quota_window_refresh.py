"""懒刷新：restore_at 已过且账面额度≤0 时可进入候选，取号时再拉上游；账号级错峰。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from services.account_service import AccountService


def _scheduler(*, jitter_hours: float = 0.0) -> MagicMock:
    mock_config = MagicMock()
    mock_config.get_scheduler_settings.return_value = {"lazy_refresh_jitter_hours": jitter_hours}
    mock_config.image_token_max_attempts = 5
    return mock_config


class LazyQuotaWindowRefreshTests(unittest.TestCase):
    def test_due_when_restore_passed_and_quota_zero(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        account = {"status": "正常", "quota": 0, "restore_at": past, "email": "a@example.com"}
        with patch("services.account_service.config", _scheduler(jitter_hours=0)):
            self.assertTrue(AccountService._quota_window_due_for_lazy_refresh(account))

    def test_not_due_before_restore(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        account = {"status": "正常", "quota": 0, "restore_at": future, "email": "a@example.com"}
        with patch("services.account_service.config", _scheduler(jitter_hours=0)):
            self.assertFalse(AccountService._quota_window_due_for_lazy_refresh(account))

    def test_not_due_when_quota_positive(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        account = {"status": "正常", "quota": 5, "restore_at": past}
        with patch("services.account_service.config", _scheduler(jitter_hours=0)):
            self.assertFalse(AccountService._quota_window_due_for_lazy_refresh(account))

    def test_not_due_abnormal_status(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        account = {"status": "异常", "quota": 0, "restore_at": past}
        with patch("services.account_service.config", _scheduler(jitter_hours=0)):
            self.assertFalse(AccountService._quota_window_due_for_lazy_refresh(account))

    def test_available_allows_stale_zero_past_restore(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        account = {"status": "正常", "quota": 0, "restore_at": past, "email": "a@example.com"}
        with patch("services.account_service.config", _scheduler(jitter_hours=0)):
            self.assertTrue(AccountService._is_image_account_available(account))

    def test_available_blocks_zero_before_restore(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        account = {"status": "正常", "quota": 0, "restore_at": future}
        with patch("services.account_service.config", _scheduler(jitter_hours=0)):
            self.assertFalse(AccountService._is_image_account_available(account))

    def test_jitter_spreads_eligibility_across_accounts(self) -> None:
        restore = datetime.now(timezone.utc) - timedelta(minutes=30)
        a = {"status": "正常", "quota": 0, "restore_at": restore.isoformat(), "email": "alpha@proton.me"}
        b = {"status": "正常", "quota": 0, "restore_at": restore.isoformat(), "email": "beta@proton.me"}
        with patch("services.account_service.config", _scheduler(jitter_hours=6)):
            ea = AccountService._lazy_refresh_eligible_at(a)
            eb = AccountService._lazy_refresh_eligible_at(b)
        self.assertIsNotNone(ea)
        self.assertIsNotNone(eb)
        assert ea is not None and eb is not None
        self.assertNotEqual(ea, eb)
        self.assertGreaterEqual((ea - restore).total_seconds(), 0)
        self.assertLessEqual((ea - restore).total_seconds(), 6 * 3600 + 1)
        self.assertGreaterEqual((eb - restore).total_seconds(), 0)
        self.assertLessEqual((eb - restore).total_seconds(), 6 * 3600 + 1)

    def test_jitter_blocks_until_eligible(self) -> None:
        restore = datetime.now(timezone.utc) - timedelta(minutes=5)
        account = {
            "status": "正常",
            "quota": 0,
            "restore_at": restore.isoformat(),
            "email": "latebird@example.com",
        }
        with patch("services.account_service.config", _scheduler(jitter_hours=6)):
            eligible = AccountService._lazy_refresh_eligible_at(account)
            assert eligible is not None
            if eligible > datetime.now(timezone.utc):
                self.assertFalse(AccountService._quota_window_due_for_lazy_refresh(account))
            # Far past restore forces due even with jitter
            account["restore_at"] = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
            self.assertTrue(AccountService._quota_window_due_for_lazy_refresh(account))

    def test_acquire_forces_remote_refresh_for_lazy_due(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        local = {
            "access_token": "tok-a",
            "status": "正常",
            "quota": 0,
            "restore_at": past,
            "email": "tok-a@example.com",
            "panda_receive_state": "verified_ready",
        }
        refreshed = {
            **local,
            "quota": 25,
            "restore_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "limits_progress": [
                {
                    "feature_name": "image_gen",
                    "remaining": 25,
                    "reset_after": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                }
            ],
        }
        svc = AccountService.__new__(AccountService)
        svc._acquire_next_candidate_token = MagicMock(return_value="tok-a")
        svc.get_account = MagicMock(return_value=dict(local))
        svc.release_image_slot = MagicMock()
        svc._can_skip_image_preflight = MagicMock(return_value=True)
        svc._account_matches_plan_type = MagicMock(return_value=True)
        svc._account_matches_any_plan_type = MagicMock(return_value=True)
        svc._account_matches_source_type = MagicMock(return_value=True)
        svc._list_ready_candidate_tokens = MagicMock(return_value=["tok-a"])
        svc.fetch_remote_info = MagicMock(return_value=refreshed)
        svc._is_image_account_schedulable = MagicMock(return_value=True)
        svc._clear_image_preflight_failure = MagicMock()
        svc._record_image_preflight_failure = MagicMock()

        with patch("services.account_service.config", _scheduler(jitter_hours=0)):
            token = AccountService.get_available_access_token(svc)
        self.assertEqual(token, "tok-a")
        svc.fetch_remote_info.assert_called()
        event = svc.fetch_remote_info.call_args.args[1]
        self.assertEqual(event, "lazy_quota_window_refresh")


if __name__ == "__main__":
    unittest.main()
