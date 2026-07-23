from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.account_service import AccountService
from services.storage.json_storage import JSONStorageBackend


class FakeProxySettings:
    def __init__(self):
        self.calls = []

    def build_session_kwargs(self, **kwargs):
        self.calls.append(dict(kwargs))
        return dict(kwargs, proxy="http://runtime.example:8118")


class FakeOAuthResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "id_token": "new-id-token",
        }


class FakeOAuthSession:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.posts = []
        FakeOAuthSession.instances.append(self)

    def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        return FakeOAuthResponse()

    def close(self):
        self.closed = True


class AccountServiceProxyRuntimeTests(unittest.TestCase):
    def test_account_traffic_totals_accumulate_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "accounts.json"
            service = AccountService(JSONStorageBackend(path))
            service.add_account_items(
                [{"access_token": "traffic-token", "status": "正常"}],
                include_items=False,
            )

            self.assertTrue(service.record_account_traffic("traffic-token", uploaded_bytes=120, downloaded_bytes=880))
            self.assertTrue(service.record_account_traffic("traffic-token", uploaded_bytes=30, downloaded_bytes=70))

            account = service.get_account("traffic-token") or {}
            self.assertEqual(account.get("traffic_uploaded_bytes"), 150)
            self.assertEqual(account.get("traffic_downloaded_bytes"), 950)
            self.assertEqual(account.get("traffic_total_bytes"), 1100)
            self.assertTrue(account.get("traffic_updated_at"))

            reloaded = AccountService(JSONStorageBackend(path)).get_account("traffic-token") or {}
            self.assertEqual(reloaded.get("traffic_total_bytes"), 1100)

    def test_refresh_token_request_uses_upstream_proxy_runtime(self) -> None:
        fake_proxy = FakeProxySettings()
        FakeOAuthSession.instances = []
        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "services.proxy_service.proxy_settings", fake_proxy
        ), patch("curl_cffi.requests.Session", FakeOAuthSession):
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            result = service._request_access_token_refresh(
                "refresh-token",
                {"access_token": "old-access-token", "fp": {"impersonate": "chrome"}},
            )

        self.assertEqual(result["access_token"], "new-access-token")
        self.assertTrue(fake_proxy.calls)
        self.assertTrue(fake_proxy.calls[0]["upstream"])
        self.assertEqual(fake_proxy.calls[0]["account"]["access_token"], "old-access-token")
        self.assertEqual(fake_proxy.calls[0]["verify"], True)
        self.assertEqual(FakeOAuthSession.instances[0].kwargs["proxy"], "http://runtime.example:8118")
        self.assertTrue(FakeOAuthSession.instances[0].closed)


    def test_automatic_password_relogin_uses_current_account_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "old-access-token",
                        "email": "masked@example.test",
                        "password": "password",
                        "proxy": "http://user:pass@proxy-one.example:8080",
                        "status": "正常",
                    }
                ],
                include_items=False,
            )
            with patch.object(
                service,
                "_login_with_password",
                return_value={"ok": False, "error": "temporary", "detail": {}},
            ) as login, patch.object(service, "remove_invalid_token"):
                service._password_re_login_thread(
                    "old-access-token",
                    "masked@example.test",
                    "password",
                    "unit-test",
                )

        self.assertEqual(login.call_count, 1)
        account = login.call_args.kwargs.get("account")
        self.assertIsInstance(account, dict)
        self.assertEqual(account.get("proxy"), "http://user:pass@proxy-one.example:8080")


if __name__ == "__main__":
    unittest.main()
