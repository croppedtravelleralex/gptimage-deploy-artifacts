from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import scripts.outlook_camoufox_stable_register as stable


class _FakeCookies:
    def __init__(self, jar: list[SimpleNamespace]) -> None:
        self.jar = jar
        self.set_calls: list[tuple[str, str, str]] = []

    def set(self, name: str, value: str, *, domain: str) -> None:
        self.set_calls.append((name, value, domain))


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, str]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = "json"

    def json(self) -> dict[str, str]:
        return self._payload


class _FakeNextAuthSession:
    def __init__(self, *, include_state: bool = True) -> None:
        jar = [
            SimpleNamespace(
                name="__Secure-next-auth.state" if include_state else "unrelated",
                value="state-value",
                domain=".chatgpt.com",
                path="/",
                secure=True,
            ),
            SimpleNamespace(
                name="oai-did",
                value="device-value",
                domain=".chatgpt.com",
                path="/",
                secure=True,
            ),
        ]
        self.cookies = _FakeCookies(jar)
        self.closed = False
        self.post_data: dict[str, str] = {}

    def get(self, *_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(200, {"csrfToken": "csrf-value"})

    def post(self, *_args: object, **kwargs: object) -> _FakeResponse:
        self.post_data = dict(kwargs.get("data") or {})
        return _FakeResponse(
            200,
            {"url": "https://auth.openai.com/api/accounts/authorize?state=state-value"},
        )

    def close(self) -> None:
        self.closed = True


class OutlookCamoufoxStableRegisterTests(unittest.TestCase):
    def test_prepare_nextauth_requires_and_returns_matching_state_cookie(self) -> None:
        session = _FakeNextAuthSession()
        with mock.patch("curl_cffi.requests.Session", return_value=session) as factory:
            authorize_url, cookies = stable.prepare_chatgpt_nextauth(
                "user@outlook.com",
                "http://proxy-user:proxy-password@127.0.0.1:54100",
            )

        self.assertTrue(authorize_url.startswith("https://auth.openai.com/api/accounts/authorize"))
        self.assertTrue(any(cookie["name"] == "__Secure-next-auth.state" for cookie in cookies))
        self.assertEqual(session.post_data["csrfToken"], "csrf-value")
        self.assertTrue(session.closed)
        self.assertEqual(
            factory.call_args.kwargs["proxies"],
            {
                "http": "http://proxy-user:proxy-password@127.0.0.1:54100",
                "https": "http://proxy-user:proxy-password@127.0.0.1:54100",
            },
        )

    def test_prepare_nextauth_rejects_missing_state_cookie(self) -> None:
        session = _FakeNextAuthSession(include_state=False)
        with mock.patch("curl_cffi.requests.Session", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "nextauth_state_cookie_missing"):
                stable.prepare_chatgpt_nextauth("user@outlook.com", "http://127.0.0.1:54100")
        self.assertTrue(session.closed)

    def test_collect_chatgpt_session_accepts_home_session_access_token(self) -> None:
        page = mock.Mock()
        page.evaluate.return_value = {
            "status": 200,
            "body": {
                "accessToken": "new-access-token",
                "sessionToken": "new-session-token",
                "expires": "2026-07-23T00:00:00Z",
            },
        }

        tokens = stable.collect_chatgpt_session(page, "http://127.0.0.1:54100")

        self.assertEqual(tokens["access_token"], "new-access-token")
        self.assertEqual(tokens["chatgpt_session_token"], "new-session-token")
        self.assertEqual(tokens["refresh_token"], "")
        page.context.cookies.assert_not_called()

    def test_main_keeps_sticky_proxy_separate_from_browser_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            credentials = root / "outlook.secret.txt"
            browser_proxy = root / "chain.secret.txt"
            out_dir = root / "out"
            credentials.write_text(
                "user@outlook.com----mail-password----client-id----mail-refresh-token\n",
                encoding="utf-8",
            )
            browser_proxy.write_text("http://chain-user:chain-password@127.0.0.1:54100\n", encoding="utf-8")
            argv = [
                "outlook_camoufox_stable_register.py",
                "--mode",
                "relogin",
                "--accounts-file",
                str(credentials),
                "--proxy",
                "92.113.236.79:6664:sticky-user:sticky-password",
                "--browser-proxy-file",
                str(browser_proxy),
                "--out-dir",
                str(out_dir),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(stable, "relogin_outlook", return_value={"ok": True}) as relogin,
            ):
                rc = stable.main()

        self.assertEqual(rc, 0)
        self.assertEqual(
            relogin.call_args.kwargs["sticky_proxy"],
            "http://sticky-user:sticky-password@92.113.236.79:6664",
        )
        self.assertEqual(
            relogin.call_args.kwargs["browser_proxy"],
            "http://chain-user:chain-password@127.0.0.1:54100",
        )

    def test_proxy_secrets_are_redacted_from_errors(self) -> None:
        proxy = "http://proxy-user:proxy-password@127.0.0.1:54100"
        error = stable.redact_proxy_secret(f"failed through {proxy} proxy-password", proxy)

        self.assertNotIn("proxy-user", error)
        self.assertNotIn("proxy-password", error)
        self.assertIn("127.0.0.1:54100", error)


if __name__ == "__main__":
    unittest.main()
