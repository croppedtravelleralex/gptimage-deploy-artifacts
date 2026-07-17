from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.account_refresh_all_service import AccountRefreshAllOptions, AccountRefreshAllService
from services.account_service import AccountService
from services.storage.json_storage import JSONStorageBackend


class AccountRefreshAllServiceTests(unittest.TestCase):
    def test_request_can_raise_refresh_all_concurrency_above_configured_default(self) -> None:
        with patch(
            "services.account_refresh_all_service.config.get_account_refresh_all_settings",
            return_value={
                "concurrency": 2,
                "max_concurrency": 4,
                "batch_size": 10,
                "delay_between_accounts_sec": 0.5,
                "delay_between_batches_sec": 1.0,
                "stale_after_hours": 0,
                "include_recent": True,
                "min_available_memory_mb": 0,
                "max_load_1m": 0,
                "resource_pause_enabled": False,
                "resource_check_interval_sec": 5.0,
                "delete_invalid": True,
                "delete_after_failures": 1,
                "expired_grace_hours": 1,
            },
        ):
            options = AccountRefreshAllOptions.from_mapping({
                "concurrency": 100,
                "max_concurrency": 100,
                "batch_size": 100,
            })

        self.assertEqual(options.concurrency, 100)

    def test_refreshed_quota_enters_image_scheduler_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {"access_token": "quota-token", "status": "限流", "quota": 0, "type": "free"}
            ])

            def fake_fetch_remote_info(access_token: str, event: str = "fetch_remote_info", defer_invalid_removal: bool = True):
                return account_service.update_account(
                    access_token,
                    {
                        "status": "正常",
                        "quota": 2,
                        "image_quota_unknown": False,
                    },
                    quiet=True,
                )

            account_service.fetch_remote_info = fake_fetch_remote_info  # type: ignore[method-assign]
            refresh_all = AccountRefreshAllService(account_service)

            status = refresh_all.start(
                AccountRefreshAllOptions(
                    limit=1,
                    stale_after_hours=0,
                    delay_between_accounts_sec=0,
                    delay_between_batches_sec=0,
                    min_available_memory_mb=0,
                    max_load_1m=0,
                )
            )
            self.assertEqual(status["total"], 1)

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status = refresh_all.get_status()
                if status["state"] == "completed":
                    break
                time.sleep(0.02)

            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["refreshed"], 1)
            self.assertEqual(status["available"], 1)
            self.assertEqual(status["became_available"], 1)

            account = account_service.get_account("quota-token")
            self.assertIsNotNone(account)
            self.assertEqual(account["status"], "正常")
            self.assertEqual(account["quota"], 2)
            self.assertTrue(account.get("last_quota_refresh_at"))
            self.assertEqual(account.get("quota_refresh_fail_count"), 0)

            selected = account_service.get_available_access_token()
            try:
                self.assertEqual(selected, "quota-token")
            finally:
                account_service.release_image_slot(selected)

    def test_refresh_preserves_identity_isolated_receive_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items(
                [
                    {
                        "access_token": "iso-token",
                        "status": "正常",
                        "quota": 1,
                        "type": "plus",
                        "panda_receive_state": "identity_isolated",
                    }
                ]
            )

            def fake_fetch_remote_info(
                access_token: str,
                event: str = "fetch_remote_info",
                defer_invalid_removal: bool = True,
            ):
                return account_service.update_account(
                    access_token,
                    {"status": "正常", "quota": 3, "image_quota_unknown": False},
                    quiet=True,
                )

            account_service.fetch_remote_info = fake_fetch_remote_info  # type: ignore[method-assign]
            refresh_all = AccountRefreshAllService(account_service)
            status = refresh_all.start(
                AccountRefreshAllOptions(
                    limit=1,
                    stale_after_hours=0,
                    delay_between_accounts_sec=0,
                    delay_between_batches_sec=0,
                    min_available_memory_mb=0,
                    max_load_1m=0,
                )
            )
            self.assertEqual(status["total"], 1)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status = refresh_all.get_status()
                if status["state"] == "completed":
                    break
                time.sleep(0.02)
            self.assertEqual(status["state"], "completed")
            account = account_service.get_account("iso-token")
            self.assertIsNotNone(account)
            self.assertEqual(account.get("panda_receive_state"), "identity_isolated")
            self.assertEqual(int(account.get("quota") or 0), 3)

    def test_start_skips_recent_accounts_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "recent-token",
                    "status": "正常",
                    "quota": 1,
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                }
            ])
            refresh_all = AccountRefreshAllService(account_service)

            status = refresh_all.start(
                AccountRefreshAllOptions(
                    stale_after_hours=6,
                    include_recent=False,
                    min_available_memory_mb=0,
                    max_load_1m=0,
                )
            )

            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["total"], 0)
            self.assertEqual(status["skipped"], 1)

    def test_recent_problematic_accounts_are_still_selected_for_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "recent-normal-token",
                    "status": "正常",
                    "quota": 1,
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                },
                {
                    "access_token": "recent-limited-token",
                    "status": "限流",
                    "quota": 0,
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                },
                {
                    "access_token": "recent-abnormal-token",
                    "status": "异常",
                    "quota": 0,
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                },
            ])
            refresh_all = AccountRefreshAllService(account_service)

            tokens, skipped = refresh_all._build_token_queue(
                AccountRefreshAllOptions(
                    stale_after_hours=6,
                    include_recent=False,
                    min_available_memory_mb=0,
                    max_load_1m=0,
                )
            )

            self.assertEqual(tokens, ["recent-limited-token", "recent-abnormal-token"])
            self.assertEqual(skipped, 1)

    def test_terminal_outlook_account_is_skipped_even_with_token_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items(
                [
                    {
                        "access_token": "terminal-token",
                        "status": "异常",
                        "quota": 0,
                        "outlook_recovery_state": "terminal",
                        "outlook_recovery_terminal_reason": "account_deactivated",
                    },
                    {
                        "access_token": "healthy-token",
                        "status": "正常",
                        "quota": 1,
                    },
                ]
            )
            refresh_all = AccountRefreshAllService(account_service)

            tokens, skipped = refresh_all._build_token_queue(
                AccountRefreshAllOptions(
                    token_overrides=["terminal-token", "healthy-token"],
                    include_recent=True,
                    min_available_memory_mb=0,
                    max_load_1m=0,
                )
            )

            self.assertEqual(tokens, ["healthy-token"])
            self.assertEqual(skipped, 1)

    def test_resource_pause_disabled_continues_cleanup_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {"access_token": "cleanup-token", "status": "限流", "quota": 0, "type": "free"}
            ])

            def fake_fetch_remote_info(access_token: str, event: str = "fetch_remote_info", defer_invalid_removal: bool = True):
                return account_service.update_account(
                    access_token,
                    {"status": "正常", "quota": 1, "image_quota_unknown": False},
                    quiet=True,
                )

            account_service.fetch_remote_info = fake_fetch_remote_info  # type: ignore[method-assign]
            refresh_all = AccountRefreshAllService(account_service)

            with patch.object(
                refresh_all,
                "_resource_ok",
                return_value=(False, "available memory 100MB < 512MB", {"available_memory_mb": 100}),
            ):
                status = refresh_all.start(
                    AccountRefreshAllOptions(
                        limit=1,
                        stale_after_hours=0,
                        include_recent=True,
                        resource_pause_enabled=False,
                        delay_between_accounts_sec=0,
                        delay_between_batches_sec=0,
                    )
                )
                self.assertEqual(status["state"], "running")

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    status = refresh_all.get_status()
                    if status["state"] == "completed":
                        break
                    time.sleep(0.02)

            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["processed"], 1)
            self.assertEqual(status["refreshed"], 1)

    def test_limit_zero_does_not_refresh_local_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {"access_token": "do-not-refresh", "status": "正常", "quota": 1}
            ])
            refresh_all = AccountRefreshAllService(account_service)

            status = refresh_all.start(
                AccountRefreshAllOptions(
                    limit=0,
                    stale_after_hours=0,
                    include_recent=True,
                    min_available_memory_mb=0,
                    max_load_1m=0,
                )
            )

            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["total"], 0)
            self.assertEqual(status["skipped"], 1)

    def test_invalid_error_is_not_downgraded_by_recent_proxy_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "unstable-token",
                    "status": "正常",
                    "quota": 1,
                    "last_token_refresh_error": "Failed to perform, curl: (56) CONNECT tunnel failed, response 503",
                    "last_token_refresh_error_at": "2999-01-01T00:00:00+00:00",
                }
            ])
            refresh_all = AccountRefreshAllService(account_service)

            removed = refresh_all._record_failure(
                "unstable-token",
                "token invalidated (/backend-api/me)",
                AccountRefreshAllOptions(delete_invalid=True, delete_after_failures=1),
            )

            self.assertTrue(removed)
            self.assertIsNone(account_service.get_account("unstable-token"))

    def test_transient_refresh_failure_does_not_refresh_quota_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "unstable-token",
                    "status": "正常",
                    "quota": 1,
                    "last_quota_refresh_at": "2026-01-01T00:00:00+00:00",
                }
            ])
            refresh_all = AccountRefreshAllService(account_service)

            removed = refresh_all._record_failure(
                "unstable-token",
                "Failed to perform, curl: (56) CONNECT tunnel failed, response 503",
                AccountRefreshAllOptions(delete_invalid=True, delete_after_failures=1),
            )

            self.assertFalse(removed)
            account = account_service.get_account("unstable-token")
            self.assertIsNotNone(account)
            self.assertEqual(account.get("last_quota_refresh_at"), "2026-01-01T00:00:00+00:00")
            self.assertEqual(account.get("quota_refresh_failure_kind"), "transient")
            self.assertFalse(account_service._is_image_account_schedulable(account or {}))

    def test_confirmed_invalid_refresh_failure_deletes_local_account_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {"access_token": "dead-token", "status": "正常", "quota": 1, "type": "free"}
            ])
            refresh_all = AccountRefreshAllService(account_service)

            removed = refresh_all._record_failure(
                "dead-token",
                "token invalidated (/backend-api/me)",
                AccountRefreshAllOptions(delete_invalid=True, delete_after_failures=1),
            )

            self.assertTrue(removed)
            self.assertIsNone(account_service.get_account("dead-token"))

    def test_available_account_is_not_queued_when_panda_auth_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "quota-token",
                    "status": "正常",
                    "quota": 3,
                    "type": "free",
                    "panda_sync_state": "ready",
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                }
            ])
            refresh_all = AccountRefreshAllService(account_service)
            pending_path = Path(tmp_dir) / "panda_sync_pending.json"
            refresh_all._pending_file = pending_path

            synced, failed, queued = refresh_all._queue_or_sync_accounts_to_panda(
                [account_service.get_account("quota-token") or {}],
                AccountRefreshAllOptions(
                    panda_sync_requested=True,
                    panda_sync_enabled=False,
                    panda_sync_base_url="https://gptimage.relai.asia",
                ),
            )

            self.assertEqual((synced, failed, queued), (0, 1, 0))
            self.assertFalse(pending_path.exists())
            self.assertIsNotNone(account_service.get_account("quota-token"))

    def test_panda_acceptance_removes_local_only_after_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "accepted-token",
                    "status": "正常",
                    "quota": 3,
                    "type": "free",
                    "panda_sync_state": "ready",
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                }
            ])
            refresh_all = AccountRefreshAllService(account_service)

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return b'{"added": 1, "skipped": 0, "updated": 0}'

            with patch("services.account_refresh_all_service.urllib.request.urlopen", return_value=FakeResponse()):
                synced, failed, queued = refresh_all._queue_or_sync_accounts_to_panda(
                    [account_service.get_account("accepted-token") or {}],
                    AccountRefreshAllOptions(
                        panda_sync_requested=True,
                        panda_sync_enabled=True,
                        panda_sync_base_url="https://gptimage.relai.asia",
                        panda_sync_auth_key="secret",
                        panda_sync_remove_local_on_success=True,
                    ),
                )

            self.assertEqual((synced, failed, queued), (1, 0, 0))
            self.assertIsNone(account_service.get_account("accepted-token"))

    def test_panda_acceptance_marks_local_synced_when_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "accepted-token",
                    "status": "正常",
                    "quota": 3,
                    "type": "free",
                    "panda_sync_state": "ready",
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                }
            ])
            refresh_all = AccountRefreshAllService(account_service)

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return b'{"added": 1, "skipped": 0, "updated": 0}'

            with patch("services.account_refresh_all_service.urllib.request.urlopen", return_value=FakeResponse()):
                synced, failed, queued = refresh_all._queue_or_sync_accounts_to_panda(
                    [account_service.get_account("accepted-token") or {}],
                    AccountRefreshAllOptions(
                        panda_sync_requested=True,
                        panda_sync_enabled=True,
                        panda_sync_base_url="https://gptimage.relai.asia",
                        panda_sync_auth_key="secret",
                        panda_sync_remove_local_on_success=False,
                    ),
                )

            account = account_service.get_account("accepted-token") or {}
            self.assertEqual((synced, failed, queued), (1, 0, 0))
            self.assertEqual(account.get("panda_sync_state"), "synced")
            self.assertTrue(account.get("panda_synced_at"))

    def test_queue_available_accounts_reports_sync_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "ready-token",
                    "status": "正常",
                    "quota": 3,
                    "panda_sync_state": "ready",
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                },
                {
                    "access_token": "missing-remote-token",
                    "status": "正常",
                    "quota": 3,
                    "panda_sync_state": "synced",
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                },
                {
                    "access_token": "already-remote-token",
                    "status": "正常",
                    "quota": 3,
                    "panda_sync_state": "synced",
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                },
                {
                    "access_token": "tainted-token",
                    "status": "正常",
                    "quota": 3,
                    "panda_sync_state": "ready",
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                    "quota_refresh_failure_kind": "transient",
                },
            ])
            refresh_all = AccountRefreshAllService(account_service)

            with patch("services.account_refresh_all_service.config.get_panda_sync_settings", return_value={
                "enabled": True,
                "base_url": "https://gptimage.relai.asia",
                "auth_key": "secret",
                "batch_size": 20,
                "timeout_seconds": 60,
                "remove_local_on_success": True,
                "queue_on_failure": False,
                "cooldown_seconds": 0,
                "upload_max_batch": 20,
                "watermark_enabled": True,
                "high_watermark": 1500,
                "low_watermark": 500,
            }), patch.object(refresh_all, "_fetch_panda_account_tokens", return_value={"already-remote-token"}), patch.object(refresh_all, "_panda_upload_capacity", return_value=0):
                result = refresh_all.queue_available_accounts_for_panda()

            details = result["details"]
            self.assertEqual(result["synced"], 0)
            self.assertEqual(details["eligible"], 2)
            self.assertEqual(details["remote_missing_reupload"], 1)
            self.assertEqual(details["already_remote"], 1)
            self.assertEqual(details["blocked_by_failure_evidence"], 1)
            self.assertEqual(details["blocked_by_watermark"], 2)

    def test_refresh_preserves_synced_state_when_token_does_not_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "synced-token",
                    "status": "正常",
                    "quota": 1,
                    "type": "free",
                    "panda_sync_state": "synced",
                    "panda_synced_at": "2999-01-01T00:00:00+00:00",
                }
            ])

            def fake_fetch_remote_info(access_token: str, event: str = "fetch_remote_info", defer_invalid_removal: bool = True):
                return account_service.update_account(
                    access_token,
                    {"status": "正常", "quota": 2, "image_quota_unknown": False},
                    quiet=True,
                )

            account_service.fetch_remote_info = fake_fetch_remote_info  # type: ignore[method-assign]
            refresh_all = AccountRefreshAllService(account_service)
            refresh_all._pending_file = Path(tmp_dir) / "panda_sync_pending.json"

            with patch("services.account_refresh_all_service.urllib.request.urlopen") as urlopen_mock:
                status = refresh_all.start(
                    AccountRefreshAllOptions(
                        limit=1,
                        stale_after_hours=0,
                        delay_between_accounts_sec=0,
                        delay_between_batches_sec=0,
                        min_available_memory_mb=0,
                        max_load_1m=0,
                        panda_sync_requested=True,
                        panda_sync_enabled=True,
                        panda_sync_base_url="https://gptimage.relai.asia",
                        panda_sync_auth_key="secret",
                    )
                )
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    status = refresh_all.get_status()
                    if status["state"] == "completed":
                        break
                    time.sleep(0.02)

            account = account_service.get_account("synced-token") or {}
            self.assertEqual(status["state"], "completed")
            self.assertEqual(account.get("panda_sync_state"), "synced")
            self.assertEqual(status["synced_to_panda"], 0)
            urlopen_mock.assert_not_called()

    def test_refresh_preserves_local_proxy_only_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "local-only-token",
                    "status": "正常",
                    "quota": 1,
                    "type": "free",
                    "proxy": "http://127.0.0.1:41007",
                    "panda_sync_state": "local_proxy_only",
                }
            ])

            def fake_fetch_remote_info(access_token: str, event: str = "fetch_remote_info", defer_invalid_removal: bool = True):
                return account_service.update_account(
                    access_token,
                    {"status": "正常", "quota": 2, "image_quota_unknown": False},
                    quiet=True,
                )

            account_service.fetch_remote_info = fake_fetch_remote_info  # type: ignore[method-assign]
            refresh_all = AccountRefreshAllService(account_service)

            refresh_all._process_token(
                0,
                "local-only-token",
                AccountRefreshAllOptions(
                    delay_between_accounts_sec=0,
                    min_available_memory_mb=0,
                    max_load_1m=0,
                ),
            )

            account = account_service.get_account("local-only-token") or {}
            self.assertEqual(account.get("panda_sync_state"), "local_proxy_only")

    def test_panda_sync_strips_local_loopback_proxy_from_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "loopback-token",
                    "status": "正常",
                    "quota": 3,
                    "type": "free",
                    "proxy": "http://127.0.0.1:41007",
                    "proxy_scope": "local_dedicated",
                    "proxy_egress_hash": "local-hash",
                    "panda_sync_state": "ready",
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                }
            ])
            refresh_all = AccountRefreshAllService(account_service)
            uploaded: dict = {}

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return b'{"added": 1, "skipped": 0, "updated": 0}'

            def fake_urlopen(request, timeout):
                uploaded.update(json.loads(request.data.decode("utf-8"))["accounts"][0])
                return FakeResponse()

            with patch("services.account_refresh_all_service.urllib.request.urlopen", side_effect=fake_urlopen):
                synced, failed, queued = refresh_all._queue_or_sync_accounts_to_panda(
                    [account_service.get_account("loopback-token") or {}],
                    AccountRefreshAllOptions(
                        panda_sync_requested=True,
                        panda_sync_enabled=True,
                        panda_sync_base_url="https://gptimage.relai.asia",
                        panda_sync_auth_key="secret",
                        panda_sync_remove_local_on_success=False,
                    ),
                )

            self.assertEqual((synced, failed, queued), (1, 0, 0))
            self.assertEqual(uploaded.get("proxy"), "")
            self.assertEqual(uploaded.get("proxy_scope"), "panda_runtime_default")
            self.assertIsNone(uploaded.get("proxy_egress_hash"))
            self.assertEqual(uploaded.get("panda_receive_state"), "incoming")

    def test_panda_sync_skips_tainted_ready_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {
                    "access_token": "clean-token",
                    "status": "正常",
                    "quota": 3,
                    "type": "free",
                    "panda_sync_state": "ready",
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                },
                {
                    "access_token": "tainted-token",
                    "status": "正常",
                    "quota": 3,
                    "type": "free",
                    "panda_sync_state": "ready",
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                    "last_refresh_error": "token invalidated (/backend-api/me)",
                    "invalid_count": 1,
                },
            ])
            refresh_all = AccountRefreshAllService(account_service)
            refresh_all._pending_file = Path(tmp_dir) / "panda_sync_pending.json"

            synced, failed, queued = refresh_all._queue_or_sync_accounts_to_panda(
                [account_service.get_account("clean-token") or {}, account_service.get_account("tainted-token") or {}],
                AccountRefreshAllOptions(
                    panda_sync_requested=True,
                    panda_sync_enabled=False,
                    panda_sync_base_url="https://gptimage.relai.asia",
                ),
            )

            self.assertEqual((synced, failed, queued), (0, 1, 0))
            self.assertFalse(refresh_all._pending_file.exists())
            self.assertIsNotNone(account_service.get_account("clean-token"))
            self.assertIsNotNone(account_service.get_account("tainted-token"))

    def test_panda_sync_batches_ok_accounts_even_when_remove_local_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {"access_token": f"batch-token-{i}", "status": "限流", "quota": 0, "type": "free"}
                for i in range(3)
            ])

            def fake_fetch_remote_info(access_token: str, event: str = "fetch_remote_info", defer_invalid_removal: bool = True):
                return account_service.update_account(
                    access_token,
                    {
                        "status": "正常",
                        "quota": 2,
                        "image_quota_unknown": False,
                    },
                    quiet=True,
                )

            account_service.fetch_remote_info = fake_fetch_remote_info  # type: ignore[method-assign]
            refresh_all = AccountRefreshAllService(account_service)
            refresh_all._pending_file = Path(tmp_dir) / "panda_sync_pending.json"
            request_account_counts: list[int] = []

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return b'{"added": 3, "skipped": 0, "updated": 0}'

            def fake_urlopen(request, timeout=0):
                payload = request.data.decode("utf-8")
                request_account_counts.append(len(__import__("json").loads(payload)["accounts"]))
                return FakeResponse()

            with patch("services.account_refresh_all_service.urllib.request.urlopen", side_effect=fake_urlopen):
                status = refresh_all.start(
                    AccountRefreshAllOptions(
                        concurrency=1,
                        limit=3,
                        stale_after_hours=0,
                        delay_between_accounts_sec=0,
                        delay_between_batches_sec=0,
                        min_available_memory_mb=0,
                        max_load_1m=0,
                        panda_sync_requested=True,
                        panda_sync_enabled=True,
                        panda_sync_base_url="https://gptimage.relai.asia",
                        panda_sync_auth_key="secret",
                        panda_sync_batch_size=20,
                        panda_sync_remove_local_on_success=True,
                    )
                )
                self.assertEqual(status["total"], 3)

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    status = refresh_all.get_status()
                    if status["state"] == "completed":
                        break
                    time.sleep(0.02)

            self.assertEqual(status["state"], "completed")
            self.assertEqual(request_account_counts, [3])
            self.assertEqual(status["synced_to_panda"], 3)
            for i in range(3):
                self.assertIsNone(account_service.get_account(f"batch-token-{i}"))

    def test_sync_last_refreshed_accounts_to_panda_uses_recent_refresh_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items([
                {"access_token": "recent-a", "status": "正常", "quota": 2, "type": "free"},
                {"access_token": "recent-b", "status": "正常", "quota": 1, "type": "free"},
            ])
            account_service.refresh_accounts(["recent-a", "recent-b"], include_items=False)

            refresh_all = AccountRefreshAllService(account_service)
            seen_batches: list[list[str]] = []

            def fake_queue(tokens=None):
                batch = list(tokens or [])
                seen_batches.append(batch)
                return {"synced": len(batch), "failed": 0, "queued": 0}

            with patch.object(refresh_all, "queue_refreshed_tokens_for_panda", side_effect=fake_queue):
                result = refresh_all.sync_last_refreshed_accounts_to_panda()

            self.assertEqual(result, {"synced": 2, "failed": 0, "queued": 0})
            self.assertEqual(seen_batches, [["recent-a", "recent-b"]])

    def test_refresh_all_splits_true_unlimited_and_unknown_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            account_service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account_service.add_account_items(
                [
                    {"access_token": "plus-unknown", "status": "限流", "quota": 0, "type": "Plus"},
                    {"access_token": "pro-unlimited", "status": "限流", "quota": 0, "type": "Pro"},
                ]
            )

            def fake_fetch_remote_info(access_token: str, event: str = "fetch_remote_info", defer_invalid_removal: bool = True):
                if access_token == "pro-unlimited":
                    return account_service.update_account(
                        access_token,
                        {
                            "status": "正常",
                            "quota": 0,
                            "image_quota_unknown": True,
                        },
                        quiet=True,
                    )
                return account_service.update_account(
                    access_token,
                    {
                        "status": "正常",
                        "quota": 0,
                        "image_quota_unknown": True,
                    },
                    quiet=True,
                )

            account_service.fetch_remote_info = fake_fetch_remote_info  # type: ignore[method-assign]
            refresh_all = AccountRefreshAllService(account_service)

            status = refresh_all.start(
                AccountRefreshAllOptions(
                    limit=2,
                    stale_after_hours=0,
                    delay_between_accounts_sec=0,
                    delay_between_batches_sec=0,
                    min_available_memory_mb=0,
                    max_load_1m=0,
                )
            )
            self.assertEqual(status["total"], 2)

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status = refresh_all.get_status()
                if status["state"] == "completed":
                    break
                time.sleep(0.02)

            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["unlimited_quota"], 1)
            self.assertEqual(status["unknown_quota"], 1)
            self.assertEqual(status["quota_total"], 0)


if __name__ == "__main__":
    unittest.main()
