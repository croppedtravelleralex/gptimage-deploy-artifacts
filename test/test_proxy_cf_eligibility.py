from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.proxy_cf_eligibility import (
    account_cf_cache_ok,
    is_proxy_cf_ok_for_image,
    require_cf_ok_for_image,
    scan_verdict,
)


class ProxyCfEligibilityTest(unittest.TestCase):
    def test_require_cf_default_on(self) -> None:
        with patch("services.proxy_cf_eligibility._cf_policy", return_value={}):
            self.assertTrue(require_cf_ok_for_image())

    def test_quarantined_proxy_blocked(self) -> None:
        with patch("services.proxy_cf_eligibility.is_gpt_unavailable_proxy", return_value=True):
            self.assertFalse(is_proxy_cf_ok_for_image("http://u:p@1.2.3.4:8080"))

    def test_account_cf_cache(self) -> None:
        account = {
            "proxy_cf_ok": True,
            "proxy_cf_ok_at": time.time(),
            "proxy_cf_probe_endpoint": "1.2.3.4:8080",
        }
        self.assertTrue(account_cf_cache_ok(account, proxy_url="http://u:p@1.2.3.4:8080"))

    def test_scan_verdict_from_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = {
                "generated_at": "2026-07-25T08:00:00+00:00",
                "nodes": [
                    {"proxy_endpoint": "1.2.3.4:8080", "ok": True},
                    {"proxy_endpoint": "5.6.7.8:8080", "ok": False},
                ],
            }
            path = Path(tmp) / "webshare_cf_scan_latest.json"
            path.write_text(__import__("json").dumps(report), encoding="utf-8")
            with patch("services.proxy_cf_eligibility._scan_report_path", return_value=path):
                self.assertTrue(scan_verdict("1.2.3.4:8080"))
                self.assertFalse(scan_verdict("5.6.7.8:8080"))
                self.assertIsNone(scan_verdict("9.9.9.9:8080"))

    def test_unscanned_blocked_when_strict(self) -> None:
        with patch("services.proxy_cf_eligibility._cf_policy", return_value={"require_cf_ok_for_image": True, "block_unscanned_for_schedule": True}):
            with patch("services.proxy_cf_eligibility.is_gpt_unavailable_proxy", return_value=False):
                with patch("services.proxy_cf_eligibility.load_scan_index", return_value={}):
                    self.assertFalse(is_proxy_cf_ok_for_image("http://u:p@1.2.3.4:8080"))


if __name__ == "__main__":
    unittest.main()
