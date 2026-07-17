from __future__ import annotations

import unittest
from unittest.mock import patch

from services import openai_backend_api
from services.openai_backend_api import OpenAIBackendAPI


class FakeProxySettings:
    def __init__(self):
        self.session_calls = []
        self.header_calls = []
        self.invalidate_calls = []

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

    def invalidate_clearance(self, **kwargs):
        self.invalidate_calls.append(dict(kwargs))


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers = {}
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class FakeAccountService:
    def __init__(self):
        self.update_calls = []

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

    def update_account(self, token, updates, quiet=False):
        self.update_calls.append((token, updates, quiet))
        return updates


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
        self.assertNotIn("Authorization", api.session.headers)
        self.assertEqual(headers["Sec-Ch-Ua-Full-Version-List"], '"Google Chrome";v="145.0.0.0"')
        self.assertEqual(headers["Authorization"], "Bearer access-token")
        self.assertEqual(headers["Cookie"], "cf_clearance=ok")
        self.assertTrue(fake_proxy.header_calls[0]["upstream"])
        self.assertEqual(fake_proxy.header_calls[0]["target_url"], "https://chatgpt.com/backend-api/me")

    def test_backend_api_close_is_idempotent(self) -> None:
        fake_proxy = FakeProxySettings()
        with patch.object(openai_backend_api, "proxy_settings", fake_proxy), patch.object(
            openai_backend_api, "account_service", FakeAccountService()
        ), patch.object(openai_backend_api.requests, "Session", FakeSession):
            api = OpenAIBackendAPI("access-token")
            api.close()
            api.close()

        self.assertEqual(api.session.close_calls, 1)

    def test_bootstrap_headers_go_through_clearance_merge(self) -> None:
        fake_proxy = FakeProxySettings()
        with patch.object(openai_backend_api, "proxy_settings", fake_proxy), patch.object(
            openai_backend_api, "account_service", FakeAccountService()
        ), patch.object(openai_backend_api.requests, "Session", FakeSession):
            api = OpenAIBackendAPI("access-token")
            headers = api._bootstrap_headers()

        self.assertEqual(headers["Cookie"], "cf_clearance=ok")
        self.assertTrue(fake_proxy.header_calls)
        self.assertEqual(fake_proxy.header_calls[-1]["target_url"], "https://chatgpt.com/")
        self.assertTrue(fake_proxy.header_calls[-1]["upstream"])

    def test_bootstrap_soft_fails_on_cf_html_403(self) -> None:
        from utils.helper import UpstreamHTTPError

        class BoomSession(FakeSession):
            def get(self, *args, **kwargs):
                raise UpstreamHTTPError(
                    "bootstrap",
                    403,
                    "<html><head><meta name=\"viewport\" content=\"width=device-width\" />"
                    "<style>.scale-appear{}</style></head><body>blocked</body></html>",
                )

        fake_proxy = FakeProxySettings()
        with patch.object(openai_backend_api, "proxy_settings", fake_proxy), patch.object(
            openai_backend_api, "account_service", FakeAccountService()
        ), patch.object(openai_backend_api.requests, "Session", BoomSession):
            api = OpenAIBackendAPI("access-token")
            api._bootstrap()

        self.assertEqual(api.pow_script_sources, [openai_backend_api.DEFAULT_POW_SCRIPT])
        self.assertTrue(fake_proxy.invalidate_calls)

    def test_bootstrap_still_raises_on_non_soft_status(self) -> None:
        from utils.helper import UpstreamHTTPError

        class BoomSession(FakeSession):
            def get(self, *args, **kwargs):
                raise UpstreamHTTPError("bootstrap", 401, {"error": "unauthorized"})

        fake_proxy = FakeProxySettings()
        with patch.object(openai_backend_api, "proxy_settings", fake_proxy), patch.object(
            openai_backend_api, "account_service", FakeAccountService()
        ), patch.object(openai_backend_api.requests, "Session", BoomSession):
            api = OpenAIBackendAPI("access-token")
            with self.assertRaises(UpstreamHTTPError):
                api._bootstrap()

    def test_cf_edge_block_detects_empty_403(self) -> None:
        from utils.helper import UpstreamHTTPError

        self.assertTrue(
            OpenAIBackendAPI._is_cf_edge_block(UpstreamHTTPError("/backend-api/f/conversation", 403, ""))
        )
        self.assertFalse(
            OpenAIBackendAPI._is_cf_edge_block(UpstreamHTTPError("/backend-api/f/conversation", 401, {"e": 1}))
        )


if __name__ == "__main__":
    unittest.main()
