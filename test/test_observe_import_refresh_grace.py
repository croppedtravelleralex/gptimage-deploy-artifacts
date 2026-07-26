"""观察导入：grace 期内跳过 token/配额远程刷新。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from services.account_service import AccountService


class ObserveImportRefreshGraceTests(unittest.TestCase):
    def test_grace_active_from_explicit_refresh_after(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        account = {"panda_observe_refresh_after": future}
        self.assertTrue(AccountService._observe_import_refresh_grace_active(account))

    def test_grace_inactive_after_refresh_after(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        account = {"panda_observe_refresh_after": past}
        self.assertFalse(AccountService._observe_import_refresh_grace_active(account))

    def test_grace_derived_from_identity_isolated_import(self) -> None:
        imported = datetime.now(timezone.utc).isoformat()
        account = {
            "panda_receive_state": "identity_isolated",
            "panda_imported_at": imported,
        }
        self.assertTrue(AccountService._observe_import_refresh_grace_active(account))

    def test_fetch_remote_info_skips_during_grace(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(minutes=8)).isoformat()
        account = {
            "access_token": "tok-grace",
            "email": "observe@example.com",
            "quota": 22,
            "panda_observe_refresh_after": future,
        }
        service = AccountService.__new__(AccountService)
        service._accounts = {"tok-grace": account}
        service._token_refresh_lock = MagicMock()
        service._get_account_for_token = MagicMock(return_value=("tok-grace", account))
        with patch.object(service, "refresh_access_token") as refresh_mock:
            result = service.fetch_remote_info("tok-grace", "test_grace")
        refresh_mock.assert_not_called()
        self.assertEqual(result["email"], "observe@example.com")
        self.assertEqual(result["quota"], 22)

    def test_refresh_access_token_skips_during_grace(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(minutes=8)).isoformat()
        account = {
            "access_token": "tok-grace",
            "refresh_token": "rt",
            "expires_at": 0,
            "panda_observe_refresh_after": future,
        }
        service = AccountService.__new__(AccountService)
        service._token_refresh_lock = MagicMock()
        service._get_account_for_token = MagicMock(return_value=("tok-grace", account))
        with patch.object(service, "_request_access_token_refresh") as refresh_mock:
            token = service.refresh_access_token("tok-grace")
        refresh_mock.assert_not_called()
        self.assertEqual(token, "tok-grace")


if __name__ == "__main__":
    unittest.main()
