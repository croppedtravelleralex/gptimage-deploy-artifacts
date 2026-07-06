from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from services.register import openai_register
from test.test_register_worker import FakeRegistrar


class FakeTransientAccountService:
    def __init__(self):
        self.added = []
        self.deleted = []

    def add_account_items(self, items, include_items=True):
        self.added.extend(items)
        return {"added": len(items), "skipped": 0}

    def refresh_accounts(self, access_tokens, progress_id=None, defer_invalid_removal=True, include_items=True):
        return {
            "refreshed": 0,
            "errors": [],
            "relogined": 0,
            "items": [{
                "access_token": "secret-access-token",
                "refresh_token": "secret-refresh-token",
                "id_token": "secret-id-token",
            }],
        }

    def delete_accounts(self, tokens, include_items=True):
        self.deleted.extend(tokens)
        return {"removed": len(tokens)}


class RegisterWorkerTransientRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_stats = dict(openai_register.stats)
        openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 1.0})

    def tearDown(self) -> None:
        openai_register.stats.clear()
        openai_register.stats.update(self.original_stats)

    def test_transient_post_verify_message_does_not_expose_refresh_result_items(self) -> None:
        fake_service = FakeTransientAccountService()
        captured_logs: list[str] = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            diagnostics_path = Path(tmp_dir) / "diagnostics.jsonl"
            with patch.object(openai_register, "PlatformRegistrar", FakeRegistrar), patch.object(
                openai_register, "account_service", fake_service
            ), patch.object(openai_register, "register_post_verify_diagnostics_file", diagnostics_path), patch.object(
                openai_register, "log", lambda text, color="": captured_logs.append(str(text))
            ):
                result = openai_register.worker(2)

        combined = "\n".join(captured_logs + [str(result)])
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("transient"))
        self.assertIn("post_register_verification_no_refresh_success", result["error"])
        self.assertNotIn("secret-access-token", combined)
        self.assertNotIn("secret-refresh-token", combined)
        self.assertNotIn("secret-id-token", combined)
        self.assertEqual(fake_service.deleted, ["registered-token"])
        self.assertEqual(openai_register.stats["done"], 1)
        self.assertEqual(openai_register.stats["fail"], 1)


if __name__ == "__main__":
    unittest.main()
