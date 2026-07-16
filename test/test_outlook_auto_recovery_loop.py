from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.accounts as accounts_module
from services.outlook_auto_recovery_loop_service import (
    OutlookAutoRecoveryLoopService,
    is_outlook_auto_recovery_candidate,
    select_outlook_auto_recovery_candidates,
)


class FakeAccountService:
    def __init__(self, accounts: list[dict]) -> None:
        self._accounts = list(accounts)

    def list_accounts(self):
        return [dict(item) for item in self._accounts]


class FakeRecoveryService:
    def __init__(self) -> None:
        self.busy = False
        self.started: list[str] = []
        self.progress: dict[str, dict] = {}
        self.prereq_ok = True
        self.prereq_reason = ""
        self.timeout_secs = 5.0

    def is_busy(self) -> bool:
        return self.busy

    def check_prerequisites(self):
        return self.prereq_ok, self.prereq_reason

    def start(self, access_token: str) -> str:
        if self.busy:
            raise RuntimeError("已有 Outlook 账号正在恢复，请等待当前任务完成")
        self.started.append(access_token)
        progress_id = f"p-{len(self.started)}"
        self.progress[progress_id] = {
            "progress_id": progress_id,
            "done": True,
            "ok": True,
            "stage": "done",
            "email": "us***r@outlook.com",
            "error": "",
            "result": {"quota": 25},
        }
        return progress_id

    def get_progress(self, progress_id: str):
        return dict(self.progress.get(progress_id) or {})


class OutlookAutoRecoveryLoopTests(unittest.TestCase):
    def test_candidate_matches_ui_refresh_rule(self) -> None:
        self.assertTrue(
            is_outlook_auto_recovery_candidate(
                {"email": "a@outlook.com", "status": "异常", "panda_receive_state": "verified_ready"}
            )
        )
        self.assertTrue(
            is_outlook_auto_recovery_candidate(
                {"email": "b@hotmail.com", "status": "正常", "panda_receive_state": "rejected"}
            )
        )
        # 确认窗内：不进自动恢复
        self.assertFalse(
            is_outlook_auto_recovery_candidate(
                {
                    "email": "c@outlook.com",
                    "status": "正常",
                    "panda_receive_state": "verified_ready",
                    "invalid_count": 1,
                    "last_refresh_error": "token invalidated",
                    "created_at": "2000-01-01T00:00:00+00:00",
                    "last_invalid_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        # 确认窗外：正常+invalid 进入串行恢复候选（ACC-008）
        self.assertTrue(
            is_outlook_auto_recovery_candidate(
                {
                    "email": "c2@outlook.com",
                    "status": "正常",
                    "panda_receive_state": "verified_ready",
                    "invalid_count": 1,
                    "last_refresh_error": "token invalidated (/backend-api/me)",
                    "created_at": "2000-01-01T00:00:00+00:00",
                    "last_invalid_at": "2000-01-02T00:00:00+00:00",
                }
            )
        )
        self.assertFalse(
            is_outlook_auto_recovery_candidate(
                {"email": "d@example.com", "status": "异常", "panda_receive_state": "rejected"}
            )
        )
        self.assertFalse(
            is_outlook_auto_recovery_candidate(
                {
                    "email": "terminal@outlook.com",
                    "status": "禁用",
                    "panda_receive_state": "rejected",
                    "outlook_recovery_state": "terminal",
                    "outlook_recovery_terminal_reason": "account_deactivated",
                }
            )
        )

    def test_select_candidates_respects_limit_and_skip(self) -> None:
        accounts = [
            {"email": "one@outlook.com", "status": "异常", "access_token": "t1"},
            {"email": "two@outlook.com", "status": "异常", "access_token": "t2"},
        ]
        selected = select_outlook_auto_recovery_candidates(accounts, limit=1, skip_emails={"one@outlook.com"})
        self.assertEqual([item["email"] for item in selected], ["two@outlook.com"])

    def test_disabled_loop_does_not_start_recovery(self) -> None:
        recovery = FakeRecoveryService()
        accounts = FakeAccountService(
            [{"email": "a@outlook.com", "status": "异常", "access_token": "tok", "panda_receive_state": "rejected"}]
        )
        service = OutlookAutoRecoveryLoopService(account_service=accounts, recovery_service=recovery)
        with (
            patch(
                "services.outlook_auto_recovery_loop_service.config.get_outlook_auto_recovery_settings",
                return_value={
                    "enabled": False,
                    "interval_sec": 1800,
                    "max_per_cycle": 1,
                    "startup_delay_sec": 0,
                    "progress_poll_sec": 0.1,
                },
            ),
        ):
            service._stop_event.set()
            service._run()
        self.assertEqual(recovery.started, [])
        status = service.get_status()
        self.assertEqual(status["state"], "off")
        self.assertFalse(status["enabled"])

    def test_cycle_recovers_one_candidate(self) -> None:
        recovery = FakeRecoveryService()
        accounts = FakeAccountService(
            [
                {"email": "ok@outlook.com", "status": "正常", "access_token": "ok", "panda_receive_state": "verified_ready"},
                {"email": "bad@outlook.com", "status": "异常", "access_token": "bad", "panda_receive_state": "rejected"},
            ]
        )
        service = OutlookAutoRecoveryLoopService(account_service=accounts, recovery_service=recovery)
        service._run_cycle({"enabled": True, "max_per_cycle": 1, "progress_poll_sec": 0.05})
        self.assertEqual(recovery.started, ["bad"])
        status = service.get_status()
        self.assertEqual(status["totals"]["attempted"], 1)
        self.assertEqual(status["totals"]["succeeded"], 1)
        self.assertTrue(status["last_result"]["ok"])

    def test_cycle_skips_when_manual_recovery_busy(self) -> None:
        recovery = FakeRecoveryService()
        recovery.busy = True
        accounts = FakeAccountService(
            [{"email": "bad@outlook.com", "status": "异常", "access_token": "bad", "panda_receive_state": "rejected"}]
        )
        service = OutlookAutoRecoveryLoopService(account_service=accounts, recovery_service=recovery)
        service._run_cycle({"enabled": True, "max_per_cycle": 1, "progress_poll_sec": 0.05})
        self.assertEqual(recovery.started, [])
        status = service.get_status()
        self.assertEqual(status["totals"]["skipped_busy"], 1)

    def test_cycle_pauses_when_prerequisites_missing(self) -> None:
        recovery = FakeRecoveryService()
        recovery.prereq_ok = False
        recovery.prereq_reason = "Outlook 恢复凭据未配置或文件不存在"
        accounts = FakeAccountService(
            [{"email": "bad@outlook.com", "status": "异常", "access_token": "bad", "panda_receive_state": "rejected"}]
        )
        service = OutlookAutoRecoveryLoopService(account_service=accounts, recovery_service=recovery)
        service._run_cycle({"enabled": True, "max_per_cycle": 1, "progress_poll_sec": 0.05})
        self.assertEqual(recovery.started, [])
        status = service.get_status()
        self.assertEqual(status["state"], "paused")
        self.assertIn("凭据", status["pause_reason"])

    def test_next_run_seconds_computed_from_next_run_at(self) -> None:
        service = OutlookAutoRecoveryLoopService(
            account_service=FakeAccountService([]),
            recovery_service=FakeRecoveryService(),
        )
        future = time.time() + 125
        from datetime import datetime, timezone

        service._set_status(next_run_at=datetime.fromtimestamp(future, timezone.utc).isoformat())
        status = service.get_status()
        self.assertIsInstance(status["seconds_until_next_run"], int)
        self.assertGreaterEqual(status["seconds_until_next_run"], 120)
        self.assertLessEqual(status["seconds_until_next_run"], 125)

    def test_api_status_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service = OutlookAutoRecoveryLoopService(
                account_service=FakeAccountService([]),
                recovery_service=FakeRecoveryService(),
            )
            app = FastAPI()
            with (
                patch.object(accounts_module, "outlook_auto_recovery_loop_service", service),
                patch.object(accounts_module, "require_admin", return_value=None),
                patch(
                    "services.outlook_auto_recovery_loop_service.config.get_outlook_auto_recovery_settings",
                    return_value={
                        "enabled": False,
                        "interval_sec": 1800,
                        "max_per_cycle": 1,
                        "startup_delay_sec": 0,
                        "progress_poll_sec": 2,
                    },
                ),
                patch(
                    "services.outlook_auto_recovery_loop_service.config.update",
                    side_effect=lambda data: {
                        "outlook_auto_recovery": {
                            "enabled": bool((data.get("outlook_auto_recovery") or {}).get("enabled")),
                            "interval_sec": int((data.get("outlook_auto_recovery") or {}).get("interval_sec") or 1800),
                            "max_per_cycle": 1,
                            "startup_delay_sec": 0,
                            "progress_poll_sec": 2,
                        }
                    },
                ),
            ):
                app.include_router(accounts_module.create_router())
                client = TestClient(app)
                status = client.get("/api/accounts/outlook-auto-recovery/status")
                self.assertEqual(status.status_code, 200)
                self.assertFalse(status.json()["enabled"])
                updated = client.post("/api/accounts/outlook-auto-recovery", json={"enabled": True, "interval_sec": 900})
                self.assertEqual(updated.status_code, 200)
                self.assertTrue(updated.json()["enabled"])


if __name__ == "__main__":
    unittest.main()
