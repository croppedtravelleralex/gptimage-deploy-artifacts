from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import AccountService
from services.auth_service import AuthService
from services.config import config
from services.openai_backend_api import InvalidAccessTokenError
from services.storage.json_storage import JSONStorageBackend
from utils.helper import anonymize_token, split_image_model


class AccountCapabilityTests(unittest.TestCase):
    def _set_config(self, key: str, value):
        previous = config.data.get(key)
        config.data[key] = value
        return previous

    def _restore_config(self, key: str, previous) -> None:
        if previous is None:
            config.data.pop(key, None)
        else:
            config.data[key] = previous

    def test_unknown_quota_accounts_are_available_only_when_not_throttled(self) -> None:
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "限流", "image_quota_unknown": True, "quota": 0}
            )
        )
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "正常", "image_quota_unknown": True, "quota": 0}
            )
        )

    def test_prolite_variants_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertEqual(service._normalize_account_type("prolite"), "ProLite")
            self.assertEqual(service._normalize_account_type("pro_lite"), "ProLite")

    def test_search_account_type_ignores_unrelated_scalar_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertIsNone(
                service._search_account_type(
                    {
                        "amr": ["pwd", "otp", "mfa"],
                        "chatgpt_compute_residency": "no_constraint",
                        "chatgpt_data_residency": "no_constraint",
                        "user_id": "user-I52GFfLGFM0dokFk2dBiKEBn",
                    }
                )
            )

    def test_mark_image_result_does_not_consume_unknown_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])
            service.update_account(
                "token-1",
                {
                    "status": "正常",
                    "quota": 0,
                    "image_quota_unknown": True,
                },
            )

            updated = service.mark_image_result("token-1", success=True)

            self.assertIsNotNone(updated)
            self.assertEqual(updated["quota"], 0)
            self.assertEqual(updated["status"], "正常")
            self.assertTrue(updated["image_quota_unknown"])

    def test_limits_progress_image_gen_overrides_stale_unknown_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "token-1",
                        "status": "正常",
                        "quota": 0,
                        "image_quota_unknown": True,
                        "limits_progress": [
                            {"feature_name": "image_gen", "remaining": 25, "reset_after": "2026-06-30T13:57:04.555288+00:00"}
                        ],
                    }
                ]
            )

            account = service.get_account("token-1")

            self.assertIsNotNone(account)
            self.assertEqual(account["quota"], 25)
            self.assertFalse(account["image_quota_unknown"])
            self.assertEqual(account["restore_at"], "2026-06-30T13:57:04.555288+00:00")

    def test_tainted_accounts_are_not_image_schedulable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "clean-token",
                        "status": "正常",
                        "quota": 5,
                        "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                    },
                    {
                        "access_token": "invalid-token",
                        "status": "正常",
                        "quota": 5,
                        "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                        "invalid_count": 1,
                        "last_refresh_error": "token invalidated (/backend-api/me)",
                    },
                    {
                        "access_token": "transient-token",
                        "status": "正常",
                        "quota": 5,
                        "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                        "quota_refresh_fail_count": 1,
                        "quota_refresh_failure_kind": "transient",
                    },
                ]
            )

            self.assertEqual(service._list_ready_candidate_tokens(), ["clean-token"])
            stats = service.get_stats()
            self.assertEqual(stats["schedulable"], 1)
            self.assertEqual(stats["tainted_count"], 2)

    def test_stats_expose_runtime_image_candidate_counts(self) -> None:
        prev_backoff = self._set_config("image_preflight_failure_backoff_sec", 60)
        prev_required = self._set_config("image_require_recent_quota_refresh", False)
        prev_account_concurrency = self._set_config("image_account_concurrency", 1)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items(
                    [
                        {"access_token": "token-a", "status": "正常", "quota": 5},
                        {"access_token": "token-b", "status": "正常", "quota": 5},
                    ]
                )

                initial = service.get_stats()
                self.assertEqual(initial["schedulable"], 2)
                self.assertEqual(initial["preflight_backoff_count"], 0)
                self.assertEqual(initial["ready_candidate_count"], 2)
                self.assertEqual(initial["available_candidate_count"], 2)
                self.assertEqual(initial["image_inflight_count"], 0)

                service._record_image_preflight_failure("token-a", RuntimeError("bad token"))

                backed_off = service.get_stats()
                self.assertEqual(backed_off["schedulable"], 2)
                self.assertEqual(backed_off["preflight_backoff_count"], 1)
                self.assertEqual(backed_off["ready_candidate_count"], 1)
                self.assertEqual(backed_off["available_candidate_count"], 1)

                with service._image_slot_condition:
                    service._image_inflight["token-b"] = 1

                saturated = service.get_stats()
                self.assertEqual(saturated["ready_candidate_count"], 1)
                self.assertEqual(saturated["available_candidate_count"], 0)
                self.assertEqual(saturated["image_inflight_count"], 1)
        finally:
            self._restore_config("image_preflight_failure_backoff_sec", prev_backoff)
            self._restore_config("image_require_recent_quota_refresh", prev_required)
            self._restore_config("image_account_concurrency", prev_account_concurrency)

    def test_imported_accounts_enter_panda_incoming_until_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))

            service.import_account_items([
                {
                    "access_token": "incoming-token",
                    "status": "正常",
                    "quota": 5,
                    "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                }
            ])
            incoming = service.get_account("incoming-token") or {}

            self.assertEqual(incoming.get("panda_receive_state"), "incoming")
            self.assertFalse(service._is_image_account_schedulable(incoming))
            self.assertEqual(service.get_stats()["schedulable"], 0)

            service.update_account(
                "incoming-token",
                {
                    "panda_receive_state": "verified_ready",
                    "panda_verified_at": "2999-01-01T00:00:00+00:00",
                    "panda_sync_state": "ready",
                    "last_quota_refresh_error": None,
                    "quota_refresh_fail_count": 0,
                    "quota_refresh_failure_kind": None,
                },
                quiet=True,
            )

            self.assertEqual(service.get_stats()["schedulable"], 1)

    def test_true_unlimited_accounts_do_not_consume_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "token-pro",
                        "status": "正常",
                        "type": "Pro",
                        "quota": 0,
                        "image_quota_unknown": True,
                        "limits_progress": [
                            {"feature_name": "image_gen", "remaining": 25, "reset_after": "2026-06-30T13:57:04.555288+00:00"}
                        ],
                    }
                ]
            )

            updated = service.mark_image_result("token-pro", success=True)

            self.assertIsNotNone(updated)
            self.assertEqual(updated["quota"], 0)
            self.assertTrue(updated["image_quota_unknown"])
            self.assertEqual(updated["status"], "正常")

    def test_stats_split_true_unlimited_and_unknown_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "token-free", "status": "正常", "quota": 25},
                    {
                        "access_token": "token-unknown",
                        "status": "正常",
                        "quota": 0,
                        "image_quota_unknown": True,
                    },
                    {
                        "access_token": "token-pro",
                        "status": "正常",
                        "type": "Pro",
                        "quota": 0,
                        "image_quota_unknown": True,
                    },
                ]
            )

            stats = service.get_stats()

            self.assertEqual(stats["unlimited_quota_count"], 1)
            self.assertEqual(stats["unknown_quota_count"], 1)

    def test_split_image_model_supports_plan_type_prefix(self) -> None:
        self.assertEqual(split_image_model("gpt-image-2"), (None, "gpt-image-2"))
        self.assertEqual(split_image_model("plus-codex-gpt-image-2"), ("plus", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("team-codex-gpt-image-2"), ("team", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("pro-codex-gpt-image-2"), ("pro", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("plus-gpt-image-2"), (None, None))
        self.assertEqual(split_image_model("unknown-image-model"), (None, None))

    def test_get_available_access_token_filters_by_plan_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "token-plus", "type": "Plus", "status": "正常", "quota": 3},
                    {"access_token": "token-pro", "type": "Pro", "status": "正常", "quota": 3},
                ]
            )

            service.fetch_remote_info = lambda access_token, event="fetch_remote_info": service.get_account(access_token)

            plus_token = service.get_available_access_token(plan_type="plus")
            pro_token = service.get_available_access_token(plan_type="pro")
            service.release_image_slot(plus_token)
            service.release_image_slot(pro_token)

            self.assertEqual(plus_token, "token-plus")
            self.assertEqual(pro_token, "token-pro")

    def test_recent_quota_requirement_filters_unverified_accounts(self) -> None:
        prev_required = self._set_config("image_require_recent_quota_refresh", True)
        prev_hours = self._set_config("image_quota_freshness_hours", 6)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items(
                    [
                        {"access_token": "never-token", "status": "正常", "quota": 25},
                        {
                            "access_token": "stale-token",
                            "status": "正常",
                            "quota": 25,
                            "last_quota_refresh_at": "2000-01-01T00:00:00+00:00",
                        },
                        {
                            "access_token": "recent-token",
                            "status": "正常",
                            "quota": 25,
                            "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                        },
                    ]
                )

                self.assertEqual(service._list_ready_candidate_tokens(), ["recent-token"])
        finally:
            self._restore_config("image_require_recent_quota_refresh", prev_required)
            self._restore_config("image_quota_freshness_hours", prev_hours)

    def test_recently_verified_accounts_are_prioritized(self) -> None:
        prev_required = self._set_config("image_require_recent_quota_refresh", False)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items(
                    [
                        {"access_token": "never-token", "status": "正常", "quota": 25},
                        {
                            "access_token": "recent-token",
                            "status": "正常",
                            "quota": 25,
                            "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                        },
                    ]
                )

                self.assertEqual(service._list_ready_candidate_tokens()[0], "recent-token")
        finally:
            self._restore_config("image_require_recent_quota_refresh", prev_required)

    def test_recent_quota_requirement_falls_back_when_no_recent_accounts_exist(self) -> None:
        prev_required = self._set_config("image_require_recent_quota_refresh", True)
        prev_hours = self._set_config("image_quota_freshness_hours", 6)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items(
                    [
                        {
                            "access_token": "stale-token",
                            "status": "正常",
                            "quota": 25,
                            "last_quota_refresh_at": "2000-01-01T00:00:00+00:00",
                        },
                        {"access_token": "never-token", "status": "正常", "quota": 25},
                    ]
                )

                self.assertEqual(service._list_ready_candidate_tokens(), ["stale-token", "never-token"])
        finally:
            self._restore_config("image_require_recent_quota_refresh", prev_required)
            self._restore_config("image_quota_freshness_hours", prev_hours)

    def test_configured_image_token_attempts_can_skip_more_than_twenty_bad_candidates(self) -> None:
        prev_attempts = self._set_config("image_token_max_attempts", 120)
        prev_required = self._set_config("image_require_recent_quota_refresh", False)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items(
                    [
                        *[
                            {
                                "access_token": f"bad-token-{index}",
                                "status": "正常",
                                "quota": 25,
                                "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                            }
                            for index in range(80)
                        ],
                        {
                            "access_token": "good-token",
                            "status": "正常",
                            "quota": 25,
                            "last_quota_refresh_at": "2999-01-01T00:00:00+00:00",
                        },
                    ]
                )

                def fake_fetch(access_token: str, event: str = "fetch_remote_info", defer_invalid_removal: bool = True):
                    if access_token.startswith("bad-token-"):
                        raise RuntimeError("transient preflight failure")
                    return service.get_account(access_token)

                service.fetch_remote_info = fake_fetch  # type: ignore[method-assign]

                selected = service.get_available_access_token()
                try:
                    self.assertEqual(selected, "good-token")
                finally:
                    service.release_image_slot(selected)
        finally:
            self._restore_config("image_token_max_attempts", prev_attempts)
            self._restore_config("image_require_recent_quota_refresh", prev_required)

    def test_refresh_accounts_can_remove_invalid_token_without_confirmation_delay(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"], defer_invalid_removal=False)

                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual(result["items"], [])
                self.assertIsNone(service.get_account("invalid-token"))
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_refresh_accounts_defers_invalid_token_removal_by_default(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"])

                account = service.get_account("invalid-token")
                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertIsNotNone(account)
                self.assertEqual(account["invalid_count"], 1)
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value


class TokenLogTests(unittest.TestCase):
    def test_anonymize_token_hides_raw_value(self) -> None:
        token = "super-secret-token"
        token_ref = anonymize_token(token)

        self.assertTrue(token_ref.startswith("token:"))
        self.assertNotIn(token, token_ref)


class AuthServiceTests(unittest.TestCase):
    def test_create_authenticate_disable_and_delete_user_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))

            item, raw_key = service.create_key(role="user", name="Alice")

            self.assertEqual(item["role"], "user")
            self.assertEqual(item["name"], "Alice")
            self.assertTrue(item["enabled"])
            self.assertTrue(raw_key.startswith("sk-"))

            authed = service.authenticate(raw_key)
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertEqual(authed["role"], "user")
            self.assertIsNotNone(authed["last_used_at"])

            updated = service.update_key(item["id"], {"enabled": False}, role="user")
            self.assertIsNotNone(updated)
            self.assertFalse(updated["enabled"])
            self.assertIsNone(service.authenticate(raw_key))

            self.assertTrue(service.delete_key(item["id"], role="user"))
            self.assertFalse(service.delete_key(item["id"], role="user"))
            self.assertEqual(service.list_keys(role="user"), [])

    def test_authenticate_ignores_last_used_save_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            def fail_save() -> None:
                raise OSError("disk unavailable")

            service._save = fail_save

            authed = service.authenticate(raw_key)

            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertIsNotNone(authed["last_used_at"])

    def test_update_user_key_replaces_raw_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            updated = service.update_key(item["id"], {"key": "sk-user-custom-key"}, role="user")

            self.assertIsNotNone(updated)
            self.assertIsNone(service.authenticate(raw_key))

            authed = service.authenticate("sk-user-custom-key")
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])

    def test_user_key_name_must_be_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            first, _ = service.create_key(role="user", name="Alice")
            second, _ = service.create_key(role="user", name="Bob")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.create_key(role="user", name="Alice")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.update_key(second["id"], {"name": "Alice"}, role="user")

            updated = service.update_key(first["id"], {"name": "Alice"}, role="user")
            self.assertIsNotNone(updated)
            self.assertEqual(updated["name"], "Alice")


if __name__ == "__main__":
    unittest.main()
