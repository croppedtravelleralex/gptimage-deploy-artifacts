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


if __name__ == "__main__":
    unittest.main()
