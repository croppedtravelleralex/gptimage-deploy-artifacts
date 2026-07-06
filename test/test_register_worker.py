from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from services.register import openai_register


class FakeRegistrar:
    def __init__(self, proxy: str):
        self.proxy = proxy
        self.closed = False

    def register(self, index: int) -> dict:
        return {
            "email": "new@example.com",
            "password": "secret",
            "access_token": "registered-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "source_type": "web",
        }

    def close(self) -> None:
        self.closed = True


class FakeAccountService:
    def __init__(self):
        self.added: list[dict] = []
        self.deleted: list[str] = []

    def add_account_items(self, items: list[dict], include_items: bool = True) -> dict:
        self.added.extend(items)
        return {"added": len(items), "skipped": 0}

    def refresh_accounts(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        defer_invalid_removal: bool = True,
        include_items: bool = True,
    ) -> dict:
        return {
            "refreshed": 0,
            "errors": [{"token": "regi...oken", "error": "token invalidated (/backend-api/me)"}],
            "relogined": 0,
        }

    def delete_accounts(self, tokens: list[str], include_items: bool = True) -> dict:
        self.deleted.extend(tokens)
        return {"removed": len(tokens)}


class RegisterWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_stats = dict(openai_register.stats)
        openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 1.0})

    def tearDown(self) -> None:
        openai_register.stats.clear()
        openai_register.stats.update(self.original_stats)

    def test_worker_deletes_and_fails_when_post_register_verification_is_invalid(self) -> None:
        fake_service = FakeAccountService()

        with tempfile.TemporaryDirectory() as tmp_dir:
            diagnostics_path = Path(tmp_dir) / "diagnostics.jsonl"
            with patch.object(openai_register, "PlatformRegistrar", FakeRegistrar), patch.object(
                openai_register,
                "account_service",
                fake_service,
            ), patch.object(openai_register, "register_post_verify_diagnostics_file", diagnostics_path):
                result = openai_register.worker(1)
            diagnostic = diagnostics_path.read_text(encoding="utf-8")

        self.assertFalse(result["ok"])
        self.assertEqual(result.get("verification_failed"), "invalid")
        self.assertEqual(fake_service.deleted, ["registered-token"])
        self.assertEqual(openai_register.stats["success"], 0)
        self.assertEqual(openai_register.stats["fail"], 1)
        self.assertIn('"verification_failed":"invalid"', diagnostic)
        self.assertIn('"has_refresh_token":true', diagnostic)
        self.assertIn('"access_token":"token:', diagnostic)
        self.assertNotIn('"access_token":"registered-token"', diagnostic)

    def test_curl_7_proxy_connect_failure_is_transient(self) -> None:
        error = (
            "Failed to perform, curl: (7) Failed to connect to 127.0.0.1 "
            "port 40080 after 2019 ms: Could not connect to server."
        )

        self.assertTrue(openai_register.is_transient_register_error(error))

    def test_loopback_connection_refused_is_transient(self) -> None:
        self.assertTrue(openai_register.is_transient_register_error("Connection refused by proxy"))
        self.assertTrue(openai_register.is_transient_register_error("No connection could be made because the target machine actively refused it"))


if __name__ == "__main__":
    unittest.main()
