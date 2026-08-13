from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from services.account_cf_refresh_service import AccountCfRefreshService
from services.proxy_cf_eligibility import (
    account_cf_cache_expired,
    account_needs_cf_stamp_refresh,
    cf_scan_index_stale,
)


class AccountCfRefreshServiceTest(unittest.TestCase):
    def test_cf_scan_index_stale_when_empty(self) -> None:
        with patch("services.proxy_cf_eligibility.load_scan_index", return_value={}):
            self.assertTrue(cf_scan_index_stale())

    def test_account_cf_cache_expired(self) -> None:
        account = {
            "proxy_cf_ok": True,
            "proxy_cf_ok_at": time.time() - 90000,
            "proxy_cf_probe_endpoint": "1.2.3.4:8080",
        }
        with patch("services.proxy_cf_eligibility.scan_stale_sec", return_value=86400.0):
            self.assertTrue(account_cf_cache_expired(account, proxy_url="http://u:p@1.2.3.4:8080"))

    def test_account_needs_refresh_when_stamp_stale(self) -> None:
        account = {
            "status": "正常",
            "quota": 10,
            "proxy": "http://u:p@1.2.3.4:8080",
            "proxy_cf_ok": True,
            "proxy_cf_ok_at": time.time() - 90000,
            "proxy_cf_probe_endpoint": "1.2.3.4:8080",
        }
        with patch("services.proxy_cf_eligibility.scan_stale_sec", return_value=86400.0):
            with patch("services.proxy_cf_eligibility.scan_verdict", return_value=None):
                with patch("services.proxy_cf_eligibility.is_gpt_unavailable_proxy", return_value=False):
                    with patch("services.account_service.account_service._is_image_account_available", return_value=True):
                        with patch("services.account_service.account_service._has_image_account_failure_evidence", return_value=False):
                            with patch("services.account_service.account_service._requires_panda_receive_verification", return_value=False):
                                with patch("services.account_service.account_service._active_proxy_binding_duplicate", return_value=False):
                                    with patch("services.account_service.account_service._active_proxy_egress_duplicate", return_value=False):
                                        with patch(
                                            "services.account_service.account_service._is_image_account_schedulable",
                                            return_value=False,
                                        ):
                                            self.assertTrue(account_needs_cf_stamp_refresh(account))

    def test_tick_restamps_candidate(self) -> None:
        service = AccountCfRefreshService()
        account = {
            "email": "a@example.com",
            "access_token": "tok-a",
            "status": "正常",
            "quota": 10,
            "proxy": "http://u:p@1.2.3.4:8080",
        }
        with patch("services.account_cf_refresh_service._settings", return_value={"enabled": True, "max_probes_per_tick": 2, "probe_timeout_sec": 45.0, "min_retry_sec": 3600.0, "trigger_batch_scan_when_stale": False}):
            with patch.object(service, "_maybe_trigger_batch_scan", return_value=None):
                with patch.object(service, "_list_candidates", return_value=[{"email": "a@example.com", "token": "tok-a", "proxy": account["proxy"], "endpoint": "1.2.3.4:8080"}]):
                    with patch("services.account_cf_refresh_service.probe_proxy_cf_with_retries", return_value={"ok": True, "cf_classification": "none", "elapsed_ms": 10, "probe_attempts": 2, "probe_retries": 1}):
                        with patch("services.account_cf_refresh_service.clear_gpt_unavailable"):
                            with patch("services.account_cf_refresh_service.account_service.update_account_identity") as update:
                                tick = service.run_once(force=True)
        self.assertEqual(tick["restamped"], 1)
        update.assert_called_once()

    def test_quarantined_proxy_still_needs_refresh(self) -> None:
        """Stale quarantine must not veto candidacy, or the account deadlocks forever.

        Real case: cf403 was recorded against a *former* account, the proxy was later
        reassigned to a healthy one, and the only code that clears the entry runs after
        candidate selection.
        """
        account = {
            "status": "正常",
            "quota": 23,
            "proxy": "http://u:p@1.2.3.4:8080",
            "proxy_cf_ok": True,
            "proxy_cf_ok_at": time.time() - 90000,
            "proxy_cf_probe_endpoint": "1.2.3.4:8080",
        }
        with patch("services.proxy_cf_eligibility.scan_stale_sec", return_value=86400.0), \
             patch("services.proxy_cf_eligibility.scan_verdict", return_value=None), \
             patch("services.proxy_cf_eligibility.is_gpt_unavailable_proxy", return_value=True), \
             patch("services.account_service.account_service._is_image_account_available", return_value=True), \
             patch("services.account_service.account_service._has_image_account_failure_evidence", return_value=False), \
             patch("services.account_service.account_service._requires_panda_receive_verification", return_value=False), \
             patch("services.account_service.account_service._active_proxy_binding_duplicate", return_value=False), \
             patch("services.account_service.account_service._active_proxy_egress_duplicate", return_value=False), \
             patch("services.account_service.account_service._is_image_account_schedulable", return_value=False):
            self.assertTrue(account_needs_cf_stamp_refresh(account))

    def test_tick_requarantines_when_probe_fails(self) -> None:
        """Clearing quarantine to probe must be undone when the probe fails."""
        service = AccountCfRefreshService()
        proxy = "http://u:p@1.2.3.4:8080"
        settings = {
            "enabled": True,
            "max_probes_per_tick": 2,
            "probe_timeout_sec": 45.0,
            "min_retry_sec": 3600.0,
            "trigger_batch_scan_when_stale": False,
        }
        with patch("services.account_cf_refresh_service._settings", return_value=settings), \
             patch.object(service, "_maybe_trigger_batch_scan", return_value=None), \
             patch.object(service, "_list_candidates", return_value=[
                 {"email": "a@example.com", "token": "tok-a", "proxy": proxy, "endpoint": "1.2.3.4:8080"}]), \
             patch("services.account_cf_refresh_service.probe_proxy_cf_with_retries",
                   return_value={"ok": False, "cf_classification": "cf403", "elapsed_ms": 10}), \
             patch("services.account_cf_refresh_service.is_gpt_unavailable_proxy", return_value=True), \
             patch("services.account_cf_refresh_service.clear_gpt_unavailable") as clear, \
             patch("services.account_cf_refresh_service.mark_gpt_unavailable") as mark, \
             patch("services.account_cf_refresh_service.account_service.update_account_identity") as update:
            tick = service.run_once(force=True)

        self.assertEqual(tick["restamped"], 0)
        clear.assert_called_once()
        mark.assert_called_once()
        self.assertEqual(mark.call_args.args[0], proxy)
        update.assert_not_called()
        self.assertTrue(tick["rows"][0]["re_quarantined"])


if __name__ == "__main__":
    unittest.main()
