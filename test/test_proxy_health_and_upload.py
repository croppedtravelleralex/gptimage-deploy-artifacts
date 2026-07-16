from __future__ import annotations

import unittest
from unittest.mock import patch

from services.account_refresh_all_service import AccountRefreshAllService
from services.register import proxy_health


class ProxyHealthTests(unittest.TestCase):
    def test_sticky_mismatch_fails(self) -> None:
        class Sess:
            def get(self, *a, **k):
                return type("R", (), {"status_code": 200, "text": "{}"})()

            def close(self):
                return None

        class Req:
            @staticmethod
            def Session(**kwargs):
                return Sess()

        with patch.dict("sys.modules", {"curl_cffi": type("M", (), {"requests": Req})()}), patch.object(
            proxy_health,
            "measure_proxy_egress_ip",
            side_effect=[
                {"ok": True, "ip": "1.2.3.4", "loc": "US", "egress_hash": "aaaaaaaaaaaa"},
                {"ok": True, "ip": "5.6.7.8", "loc": "US", "egress_hash": "bbbbbbbbbbbb"},
            ],
        ):
            result = proxy_health.validate_http_proxy(
                "http://user:pass@203.0.113.10:80",
                timeout=5,
                require_sticky=True,
                sticky_gap_sec=0,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("error"), "egress_not_sticky")


class PandaUploadProxyPreserveTests(unittest.TestCase):
    def test_sticky_webshare_proxy_not_cleared(self) -> None:
        from services.account_identity import proxy_binding_hash

        proxy = "http://user:pass@203.0.113.10:8000"
        account = {
            "access_token": "tok",
            "proxy": proxy,
            "proxy_provider": "webshare",
            "proxy_scope": "account_sticky",
            "proxy_binding_hash": proxy_binding_hash(proxy),
            "proxy_egress_hash": "egress-hash",
            "registration_proxy_hash": proxy_binding_hash(proxy),
            "lifecycle_ip_mode": "sticky_one_ip_full",
            "fp": {
                "user-agent": "ua",
                "impersonate": "chrome120",
                "oai-device-id": "device",
                "oai-session-id": "session",
                "sec-ch-ua": "ch",
                "sec-ch-ua-arch": '"x86"',
                "sec-ch-ua-bitness": '"64"',
                "sec-ch-ua-full-version": '"120.0.0.0"',
                "sec-ch-ua-full-version-list": "list",
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-ch-ua-platform-version": '"10.0.0"',
            },
        }
        prepared = AccountRefreshAllService._prepare_account_for_panda_upload(account)
        self.assertEqual(prepared["proxy"], account["proxy"])
        self.assertEqual(prepared["proxy_scope"], "account_sticky")

    def test_loopback_proxy_still_cleared(self) -> None:
        account = {"access_token": "tok", "proxy": "http://127.0.0.1:41003"}
        prepared = AccountRefreshAllService._prepare_account_for_panda_upload(account)
        self.assertEqual(prepared["proxy"], "")
        self.assertEqual(prepared["proxy_scope"], "panda_runtime_default")


if __name__ == "__main__":
    unittest.main()
