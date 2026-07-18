"""懒刷新：restore_at 已过且账面额度≤0 时可进入候选，取号时再拉上游。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from services.account_service import AccountService


class LazyQuotaWindowRefreshTests(unittest.TestCase):
    def test_due_when_restore_passed_and_quota_zero(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        account = {"status": "正常", "quota": 0, "restore_at": past}
        self.assertTrue(AccountService._quota_window_due_for_lazy_refresh(account))

    def test_not_due_before_restore(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        account = {"status": "正常", "quota": 0, "restore_at": future}
        self.assertFalse(AccountService._quota_window_due_for_lazy_refresh(account))

    def test_not_due_when_quota_positive(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        account = {"status": "正常", "quota": 5, "restore_at": past}
        self.assertFalse(AccountService._quota_window_due_for_lazy_refresh(account))

    def test_not_due_abnormal_status(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        account = {"status": "异常", "quota": 0, "restore_at": past}
        self.assertFalse(AccountService._quota_window_due_for_lazy_refresh(account))

    def test_available_allows_stale_zero_past_restore(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        account = {"status": "正常", "quota": 0, "restore_at": past}
        self.assertTrue(AccountService._is_image_account_available(account))

    def test_available_blocks_zero_before_restore(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        account = {"status": "正常", "quota": 0, "restore_at": future}
        self.assertFalse(AccountService._is_image_account_available(account))

    def test_acquire_forces_remote_refresh_for_lazy_due(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        local = {
            "access_token": "tok-a",
            "status": "正常",
            "quota": 0,
            "restore_at": past,
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

        mock_config = MagicMock()
        mock_config.image_token_max_attempts = 5
        with patch("services.account_service.config", mock_config):
            token = AccountService.get_available_access_token(svc)
        self.assertEqual(token, "tok-a")
        svc.fetch_remote_info.assert_called()
        event = svc.fetch_remote_info.call_args.args[1]
        self.assertEqual(event, "lazy_quota_window_refresh")


if __name__ == "__main__":
    unittest.main()
