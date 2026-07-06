from __future__ import annotations

import unittest
from unittest.mock import patch

from services.register import mail_provider


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response

    def close(self):
        pass


class TempMailLolProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_next = mail_provider.tempmail_lol_next_create_at
        self._old_backoff = mail_provider.tempmail_lol_backoff_until
        mail_provider.tempmail_lol_next_create_at = 0.0
        mail_provider.tempmail_lol_backoff_until = 0.0

    def tearDown(self) -> None:
        mail_provider.tempmail_lol_next_create_at = self._old_next
        mail_provider.tempmail_lol_backoff_until = self._old_backoff

    def test_tempmail_lol_create_does_not_use_provider_rate_slot(self) -> None:
        fake = FakeSession(FakeResponse(201, {"address": "a@example.com", "token": "mail-token"}))
        entry = {"type": "tempmail_lol", "create_min_interval_sec": 60}
        conf = {"user_agent": "UA", "request_timeout": 3, "proxy": ""}

        with patch.object(mail_provider, "_create_session", return_value=fake), patch.object(
            mail_provider,
            "_reserve_tempmail_lol_create_slot",
        ) as reserve:
            provider = mail_provider.TempMailLolProvider(entry, conf)
            mailbox = provider.create_mailbox()

        reserve.assert_not_called()
        self.assertEqual(mailbox["address"], "a@example.com")
        self.assertEqual(fake.calls[0]["method"], "POST")
        self.assertTrue(fake.calls[0]["url"].endswith("/v2/inbox/create"))

    def test_tempmail_lol_429_does_not_update_global_backoff(self) -> None:
        fake = FakeSession(FakeResponse(429, text='{"error":"Rate limited (free)"}', headers={"retry-after": "7"}))
        entry = {"type": "tempmail_lol", "create_min_interval_sec": 0, "rate_limit_backoff_sec": 60}
        conf = {"user_agent": "UA", "request_timeout": 3, "proxy": ""}

        with patch.object(mail_provider, "_create_session", return_value=fake):
            provider = mail_provider.TempMailLolProvider(entry, conf)
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                provider.create_mailbox()

        self.assertEqual(mail_provider.tempmail_lol_backoff_until, 0.0)

    def test_loopback_connection_refused_is_transient(self) -> None:
        self.assertTrue(mail_provider._is_transient_mail_error("Connection refused by proxy"))
        self.assertTrue(mail_provider._is_transient_mail_error("No connection could be made because the target machine actively refused it"))


if __name__ == "__main__":
    unittest.main()
