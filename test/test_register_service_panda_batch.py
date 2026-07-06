from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import time

from services.register_service import RegisterService


class RegisterServicePandaBatchTests(unittest.TestCase):
    def test_registered_accounts_are_staged_instead_of_uploaded_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = RegisterService(Path(tmp_dir) / "register.json", auto_start=False)
            staged: list[dict] = []

            class FakeStaging:
                def stage_account(self, account, source=""):
                    staged.append({**account, "source": source})
                    return account

            with patch.object(service, "_append_log"):
                with patch("services.panda_staging_service.panda_staging_service", FakeStaging()):
                    for i in range(3):
                        service._buffer_registered_account_for_panda(
                            {
                                "ok": True,
                                "result": {"access_token": f"token-{i}"},
                            }
                        )
                    service._flush_registered_panda_buffer(final=True)

            self.assertEqual([item["access_token"] for item in staged], ["token-0", "token-1", "token-2"])
            self.assertTrue(all(item["source"] == "register_service" for item in staged))

    def test_start_refuses_missing_loopback_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = RegisterService(Path(tmp_dir) / "register.json", auto_start=False)
            service.update({"proxy": "http://127.0.0.1:9", "enabled": True})

            result = service.start()

            self.assertFalse(result["enabled"])
            self.assertIn("本地代理不可用", result["logs"][-1]["text"])

    def test_start_does_not_reenable_stopping_runner(self) -> None:
        class AliveRunner:
            def is_alive(self) -> bool:
                return True

        with tempfile.TemporaryDirectory() as tmp_dir:
            service = RegisterService(Path(tmp_dir) / "register.json", auto_start=False)
            service.update({"enabled": False})
            service._runner = AliveRunner()  # type: ignore[assignment]

            result = service.start()

            self.assertFalse(result["enabled"])
            self.assertIn("上一轮注册任务仍在收尾", result["logs"][-1]["text"])

    def test_transient_result_counts_as_done_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = RegisterService(Path(tmp_dir) / "register.json", auto_start=False)
            service.update({"total": 1, "threads": 1, "mode": "total"})

            with patch("services.register_service._local_proxy_available", return_value=True), patch(
                "services.register_service.openai_register.worker",
                return_value={"ok": False, "transient": True, "index": 1, "error": "proxy closed"},
            ):
                service.start()
                deadline = time.time() + 10
                while time.time() < deadline:
                    result = service.get()
                    if not result["enabled"] and int(result["stats"].get("running") or 0) == 0:
                        break
                    time.sleep(0.05)

            stats = service.get()["stats"]
            self.assertEqual(stats["done"], 1)
            self.assertEqual(stats["fail"], 1)
            self.assertEqual(stats["transient"], 1)

    def test_start_if_enabled_refuses_missing_loopback_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = RegisterService(Path(tmp_dir) / "register.json", auto_start=False)
            service.update({"proxy": "http://127.0.0.1:9", "enabled": True})

            result = service.start_if_enabled()

            self.assertFalse(result["enabled"])
            self.assertIn("本地代理不可用", result["logs"][-1]["text"])

    def test_auto_start_refuses_missing_loopback_proxy_and_persists_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = Path(tmp_dir) / "register.json"
            service = RegisterService(store, auto_start=False)
            service.update({"proxy": "http://127.0.0.1:9", "enabled": True})

            result = RegisterService(store, auto_start=True).get()

            self.assertFalse(result["enabled"])
            self.assertIn("本地代理不可用", result["logs"][-1]["text"])
            self.assertFalse(RegisterService(store, auto_start=False).get()["enabled"])


if __name__ == "__main__":
    unittest.main()
