from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.accounts as accounts_module
from services.outlook_account_recovery_service import OutlookAccountRecoveryService


class FakeAccountService:
    def __init__(self, account: dict) -> None:
        self.account = dict(account)
        self.reload_count = 0

    def get_account(self, access_token: str):
        if access_token != self.account.get("access_token"):
            return None
        return dict(self.account)

    def reload_from_storage(self):
        self.reload_count += 1
        return {"total": 1}

    def update_account(self, access_token: str, updates: dict, quiet: bool = False):
        if access_token != self.account.get("access_token"):
            return None
        self.account.update(dict(updates or {}))
        return dict(self.account)


class OutlookAccountRecoveryServiceTests(unittest.TestCase):
    def test_start_queues_only_abnormal_outlook_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_dir = root / "data"
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "recover_panda_outlook_accounts.py").write_text("# test", encoding="utf-8")
            credentials = data_dir / "runlogs" / "credentials.secret.txt"
            credentials.parent.mkdir(parents=True)
            credentials.write_text("secret", encoding="utf-8")
            launched: list[object] = []
            account_service = FakeAccountService(
                {
                    "access_token": "old-token",
                    "email": "user@outlook.com",
                    "password": "Pw!test123456",
                    "status": "异常",
                    "panda_receive_state": "rejected",
                    "invalid_count": 2,
                }
            )
            service = OutlookAccountRecoveryService(
                account_service=account_service,
                base_dir=root,
                data_dir=data_dir,
                credentials_file=credentials,
                worker_launcher=lambda worker: launched.append(worker),
            )

            progress_id = service.start("old-token")
            progress = service.get_progress(progress_id)

            self.assertEqual(len(launched), 1)
            self.assertFalse(progress["done"])
            self.assertEqual(progress["stage"], "queued")
            self.assertEqual(progress["email"], "us***r@outlook.com")

    def test_worker_runs_recovery_engine_and_reloads_account_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_dir = root / "data"
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            script = scripts_dir / "recover_panda_outlook_accounts.py"
            script.write_text(
                """import argparse, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--root")
p.add_argument("--credentials-file")
p.add_argument("--target-email")
p.add_argument("--limit")
p.add_argument("--report-dir")
p.add_argument("--backup-dir")
p.add_argument("--proxy-file", default="")
a, _ = p.parse_known_args()
Path(a.report_dir).mkdir(parents=True, exist_ok=True)
Path(a.backup_dir).mkdir(parents=True, exist_ok=True)
print(json.dumps({"event": "recovery_progress", "stage": "commit"}), flush=True)
Path(a.report_dir, "summary.json").write_text(json.dumps({"restored": 1, "failed": 0}), encoding="utf-8")
Path(a.report_dir, "rows.json").write_text(json.dumps([{"ok": True, "quota": 20, "status": "正常", "schedulable": True, "old_removed": True, "old_fp_inherited": False, "login_via_chatgpt_email_otp": True}]), encoding="utf-8")
""",
                encoding="utf-8",
            )
            credentials = data_dir / "runlogs" / "credentials.secret.txt"
            credentials.parent.mkdir(parents=True)
            credentials.write_text("secret", encoding="utf-8")
            account_service = FakeAccountService(
                {
                    "access_token": "old-token",
                    "email": "user@outlook.com",
                    "password": "Pw!test123456",
                    "status": "异常",
                    "panda_receive_state": "rejected",
                    "invalid_count": 2,
                }
            )
            service = OutlookAccountRecoveryService(
                account_service=account_service,
                base_dir=root,
                data_dir=data_dir,
                credentials_file=credentials,
            )

            progress_id = service.start("old-token")
            deadline = time.time() + 5
            progress = service.get_progress(progress_id)
            while progress and not progress["done"] and time.time() < deadline:
                time.sleep(0.02)
                progress = service.get_progress(progress_id)

            self.assertIsNotNone(progress)
            self.assertTrue(progress["done"])
            self.assertTrue(progress["ok"])
            self.assertEqual(progress["result"]["quota"], 20)
            self.assertEqual(account_service.reload_count, 1)

    def test_worker_marks_account_deactivated_as_terminal_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_dir = root / "data"
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            script = scripts_dir / "recover_panda_outlook_accounts.py"
            script.write_text(
                """import argparse, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--root')
p.add_argument('--credentials-file')
p.add_argument('--target-email')
p.add_argument('--limit')
p.add_argument('--report-dir')
p.add_argument('--backup-dir')
p.add_argument('--proxy-file', default='')
a, _ = p.parse_known_args()
Path(a.report_dir).mkdir(parents=True, exist_ok=True)
Path(a.backup_dir).mkdir(parents=True, exist_ok=True)
Path(a.report_dir, 'summary.json').write_text(json.dumps({'restored': 0, 'failed': 1}), encoding='utf-8')
Path(a.report_dir, 'rows.json').write_text(json.dumps([{
    'ok': False,
    'stage': 'terminal',
    'terminal_reason': 'account_deactivated',
    'error': 'OpenAI 账号已删除或停用，无法自动恢复',
}]), encoding='utf-8')
""",
                encoding="utf-8",
            )
            credentials = data_dir / "runlogs" / "credentials.secret.txt"
            credentials.parent.mkdir(parents=True)
            credentials.write_text("secret", encoding="utf-8")
            account_service = FakeAccountService(
                {
                    "access_token": "old-token",
                    "email": "user@outlook.com",
                    "password": "Pw!test123456",
                    "status": "异常",
                    "panda_receive_state": "rejected",
                    "invalid_count": 2,
                }
            )
            service = OutlookAccountRecoveryService(
                account_service=account_service,
                base_dir=root,
                data_dir=data_dir,
                credentials_file=credentials,
            )

            progress_id = service.start("old-token")
            deadline = time.time() + 5
            progress = service.get_progress(progress_id)
            while progress and not progress["done"] and time.time() < deadline:
                time.sleep(0.02)
                progress = service.get_progress(progress_id)

            self.assertIsNotNone(progress)
            self.assertTrue(progress["done"])
            self.assertFalse(progress["ok"])
            self.assertIn("已删除或停用", progress["error"])
            account = account_service.get_account("old-token")
            self.assertEqual(account["status"], "禁用")
            self.assertEqual(account["outlook_recovery_state"], "terminal")
            self.assertEqual(account["outlook_recovery_terminal_reason"], "account_deactivated")
            self.assertEqual(account["panda_receive_state"], "rejected")

    def test_start_rejects_verified_healthy_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_dir = root / "data"
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "recover_panda_outlook_accounts.py").write_text("# test", encoding="utf-8")
            credentials = data_dir / "runlogs" / "credentials.secret.txt"
            credentials.parent.mkdir(parents=True)
            credentials.write_text("secret", encoding="utf-8")
            service = OutlookAccountRecoveryService(
                account_service=FakeAccountService(
                    {
                        "access_token": "healthy-token",
                        "email": "healthy@outlook.com",
                        "status": "正常",
                        "panda_receive_state": "verified_ready",
                        "invalid_count": 0,
                    }
                ),
                base_dir=root,
                data_dir=data_dir,
                credentials_file=credentials,
                worker_launcher=lambda worker: None,
            )

            with self.assertRaisesRegex(ValueError, "仅允许恢复异常"):
                service.start("healthy-token")

    def test_terminal_deactivated_account_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_dir = root / "data"
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "recover_panda_outlook_accounts.py").write_text("# test", encoding="utf-8")
            credentials = data_dir / "runlogs" / "credentials.secret.txt"
            credentials.parent.mkdir(parents=True)
            credentials.write_text("secret", encoding="utf-8")
            account_service = FakeAccountService(
                {
                    "access_token": "terminal-token",
                    "email": "terminal@outlook.com",
                    "status": "禁用",
                    "panda_receive_state": "rejected",
                    "outlook_recovery_state": "terminal",
                    "outlook_recovery_terminal_reason": "account_deactivated",
                }
            )
            service = OutlookAccountRecoveryService(
                account_service=account_service,
                base_dir=root,
                data_dir=data_dir,
                credentials_file=credentials,
                worker_launcher=lambda worker: None,
            )

            with self.assertRaisesRegex(ValueError, "停用"):
                service.start("terminal-token")



    def test_start_allows_password_without_credentials_when_yumail_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_dir = root / "data"
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "recover_panda_outlook_accounts.py").write_text("# test", encoding="utf-8")
            missing_credentials = data_dir / "runlogs" / "missing.credentials.secret.txt"
            missing_credentials.parent.mkdir(parents=True, exist_ok=True)
            launched: list[object] = []
            account_service = FakeAccountService(
                {
                    "access_token": "old-token",
                    "email": "user@outlook.com",
                    "password": "Pw!test123456",
                    "status": "异常",
                    "panda_receive_state": "rejected",
                    "invalid_count": 2,
                }
            )
            service = OutlookAccountRecoveryService(
                account_service=account_service,
                base_dir=root,
                data_dir=data_dir,
                credentials_file=missing_credentials,
                worker_launcher=lambda worker: launched.append(worker),
            )
            with mock.patch("services.outlook_account_recovery_service.yumail_otp.is_configured", return_value=True), mock.patch(
                "services.outlook_account_recovery_service.yumail_otp.probe_reachable",
                return_value={"ok": True},
            ):
                progress_id = service.start("old-token")
            self.assertEqual(len(launched), 1)
            self.assertFalse(service.get_progress(progress_id)["done"])

    def test_start_rejects_missing_password_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_dir = root / "data"
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "recover_panda_outlook_accounts.py").write_text("# test", encoding="utf-8")
            missing_credentials = data_dir / "runlogs" / "missing.credentials.secret.txt"
            missing_credentials.parent.mkdir(parents=True, exist_ok=True)
            service = OutlookAccountRecoveryService(
                account_service=FakeAccountService(
                    {
                        "access_token": "old-token",
                        "email": "user@outlook.com",
                        "status": "异常",
                        "panda_receive_state": "rejected",
                        "invalid_count": 2,
                    }
                ),
                base_dir=root,
                data_dir=data_dir,
                credentials_file=missing_credentials,
                worker_launcher=lambda worker: None,
            )
            with self.assertRaisesRegex(ValueError, "need_openai_password"):
                service.start("old-token")

class OutlookAccountRecoveryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.admin_patch = mock.patch.object(accounts_module, "require_admin", lambda _authorization: {"role": "admin"})
        self.admin_patch.start()
        app = FastAPI()
        app.include_router(accounts_module.create_router())
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.admin_patch.stop()

    def test_start_and_poll_outlook_recovery(self) -> None:
        with (
            mock.patch.object(accounts_module.outlook_account_recovery_service, "start", return_value="progress-1") as start,
            mock.patch.object(
                accounts_module.outlook_account_recovery_service,
                "get_progress",
                return_value={"progress_id": "progress-1", "done": False, "stage": "login"},
            ),
        ):
            response = self.client.post(
                "/api/accounts/recover-outlook",
                json={"access_token": "old-token"},
            )
            progress = self.client.get("/api/accounts/recover-outlook/progress/progress-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"progress_id": "progress-1"})
        start.assert_called_once_with("old-token")
        self.assertEqual(progress.status_code, 200)
        self.assertEqual(progress.json()["stage"], "login")

    def test_missing_progress_returns_404(self) -> None:
        with mock.patch.object(accounts_module.outlook_account_recovery_service, "get_progress", return_value=None):
            response = self.client.get("/api/accounts/recover-outlook/progress/missing")

        self.assertEqual(response.status_code, 404)

    def test_reload_accounts_from_storage_refreshes_live_account_service(self) -> None:
        with (
            mock.patch.object(accounts_module.account_service, "reload_from_storage", return_value={"total": 20}) as reload_accounts,
            mock.patch.object(accounts_module.account_service, "get_stats", return_value={"total": 20, "schedulable": 17}),
        ):
            response = self.client.post("/api/accounts/reload-from-storage")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"ok": True, "total": 20, "stats": {"total": 20, "schedulable": 17}},
        )
        reload_accounts.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
