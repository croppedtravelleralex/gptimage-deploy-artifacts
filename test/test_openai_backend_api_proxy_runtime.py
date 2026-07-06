from __future__ import annotations

import unittest
from unittest.mock import patch

from services import openai_backend_api
from services.openai_backend_api import OpenAIBackendAPI


class FakeProxySettings:
    def __init__(self):
        self.session_calls = []
        self.header_calls = []

    def build_session_kwargs(self, **kwargs):
        self.session_calls.append(dict(kwargs))
        return dict(kwargs, proxy="http://runtime.example:8118")

    def build_headers(self, headers=None, target_url="", account=None, upstream=True, **kwargs):
        self.header_calls.append({
            "headers": dict(headers or {}),
            "target_url": target_url,
            "account": dict(account or {}),
            "upstream": upstream,
            **kwargs,
        })
        merged = dict(headers or {})
        merged["Cookie"] = "cf_clearance=ok"
        return merged


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers = {}


class FakeAccountService:
    def get_account(self, token):
        return {
            "access_token": token,
            "fp": {
                "user-agent": "Registered UA",
                "impersonate": "chrome",
                "oai-device-id": "registered-device",
                "oai-session-id": "registered-session",
                "sec-ch-ua": '"Google Chrome";v="145"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-ch-ua-full-version-list": '"Google Chrome";v="145.0.0.0"',
                "sec-ch-ua-platform-version": '"10.0.0"',
            },
        }


class OpenAIBackendAPIProxyRuntimeTests(unittest.TestCase):
    def test_backend_api_uses_upstream_proxy_and_clearance_headers(self) -> None:
        fake_proxy = FakeProxySettings()
        with patch.object(openai_backend_api, "proxy_settings", fake_proxy), patch.object(
            openai_backend_api, "account_service", FakeAccountService()
        ), patch.object(openai_backend_api.requests, "Session", FakeSession):
            api = OpenAIBackendAPI("access-token")
            headers = api._headers("/backend-api/me")

        self.assertTrue(fake_proxy.session_calls)
        self.assertTrue(fake_proxy.session_calls[0]["upstream"])
        self.assertEqual(fake_proxy.session_calls[0]["account"]["access_token"], "access-token")
        self.assertEqual(api.session.kwargs["proxy"], "http://runtime.example:8118")
        self.assertEqual(api.user_agent, "Registered UA")
        self.assertEqual(api.device_id, "registered-device")
        self.assertEqual(api.session.headers["Sec-Ch-Ua-Full-Version-List"], '"Google Chrome";v="145.0.0.0"')
        self.assertEqual(headers["Cookie"], "cf_clearance=ok")
        self.assertTrue(fake_proxy.header_calls[0]["upstream"])
        self.assertEqual(fake_proxy.header_calls[0]["target_url"], "https://chatgpt.com/backend-api/me")


if __name__ == "__main__":
    unittest.main()
