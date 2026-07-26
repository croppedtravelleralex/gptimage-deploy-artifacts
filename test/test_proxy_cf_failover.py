from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.proxy_cf_failover import (
    bump_cf_streak,
    load_proxy_pool,
    pick_clean_proxy,
    pick_swap_proxy,
    reset_cf_streak,
    swap_account_proxy_on_cf,
)
from services.proxy_quarantine import is_gpt_unavailable_proxy, mark_gpt_unavailable


class ProxyCfFailoverTest(unittest.TestCase):
    def test_parse_webshare_pool_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool.txt"
            pool.write_text("92.113.246.215:5800:user:pass\n", encoding="utf-8")
            urls = load_proxy_pool(pool)
            self.assertEqual(len(urls), 1)
            self.assertIn("92.113.246.215:5800", urls[0])

    def test_quarantined_proxy_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool.txt"
            pool.write_text(
                "1.2.3.4:8080:u:p\n5.6.7.8:8080:u:p\n",
                encoding="utf-8",
            )
            with patch("services.proxy_cf_eligibility.require_cf_ok_for_image", return_value=False):
                with patch(
                    "services.proxy_cf_failover.is_gpt_unavailable_proxy",
                    side_effect=lambda url: "1.2.3.4" in str(url),
                ):
                    picked = pick_clean_proxy(pool_path=pool)
            self.assertIn("5.6.7.8", picked)
            self.assertNotIn("1.2.3.4", picked)

    def test_pick_swap_proxy_falls_back_when_pool_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool.txt"
            pool.write_text(
                "1.2.3.4:8080:u:p\n5.6.7.8:8080:u:p\n",
                encoding="utf-8",
            )
            with patch("services.proxy_cf_eligibility.require_cf_ok_for_image", return_value=False):
                with patch(
                    "services.proxy_cf_failover.is_gpt_unavailable_proxy",
                    return_value=True,
                ):
                    self.assertEqual(pick_clean_proxy(pool_path=pool), "")
                    picked = pick_swap_proxy(pool_path=pool, exclude={"1.2.3.4:8080"})
            self.assertIn("5.6.7.8", picked)

    def test_swap_requires_threshold(self) -> None:
        token = "tok-a"
        reset_cf_streak(token)
        with patch("services.proxy_cf_failover.account_service") as svc:
            svc.get_account.return_value = {"proxy": "http://u:p@1.2.3.4:8080", "email": "a@x.com"}
            first = swap_account_proxy_on_cf(token, threshold=2)
            self.assertTrue(first.get("skipped"))
            self.assertEqual(bump_cf_streak(token), 2)

    def test_reset_observability_lights(self) -> None:
        import threading

        from services.account_service import AccountService

        svc = AccountService.__new__(AccountService)
        svc._lock = threading.RLock()
        svc._accounts = {}
        svc._persist_upsert_accounts = lambda accounts: None
        svc._resolve_access_token_locked = lambda t: t
        svc._normalize_account = lambda item: dict(item)
        token = "tok-b"
        svc._accounts[token] = {
            "access_token": token,
            "email": "b@x.com",
            "cf_daily": [{"date": "2026-07-22", "ok": 0, "cf": 3, "image_fail": 1}],
            "egress_daily": [{"date": "2026-07-22", "ip": "1.2.3.4", "status": "warn"}],
        }
        self.assertTrue(svc.reset_observability_lights(token))
        self.assertEqual(svc._accounts[token]["cf_daily"], [])
        self.assertEqual(svc._accounts[token]["egress_daily"], [])


if __name__ == "__main__":
    unittest.main()
