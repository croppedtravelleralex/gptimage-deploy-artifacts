from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from scripts import recover_panda_outlook_accounts as recovery


class FakeAccountService:
    _OAUTH_USER_AGENT = "test-agent"

    def __init__(self, *, refresh_ok: bool) -> None:
        self.refresh_ok = refresh_ok
        self.events: list[str] = []
        self.accounts = {
            "old-token": {
                "email": "user@outlook.com",
                "access_token": "old-token",
                "password": "openai-password",
                "status": "异常",
                "quota": 0,
                "panda_receive_state": "rejected",
                "invalid_count": 2,
            }
        }

    def _login_with_password(self, email, password, otp_resolver=None):
        self.events.append("login")
        self.asserted_otp = otp_resolver() if otp_resolver else None
        return {
            "ok": True,
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "id_token": "new-id",
        }

    def get_account(self, token):
        value = self.accounts.get(token)
        return dict(value) if value else None

    def add_account_items(self, items, include_items=False):
        item = dict(items[0])
        self.events.append(f"add:{item['access_token']}")
        self.accounts[item["access_token"]] = item
        return {"added": 1, "skipped": 0}

    def refresh_accounts(self, tokens, defer_invalid_removal=True, include_items=False):
        token = tokens[0]
        self.events.append(f"refresh:{token}")
        if not self.refresh_ok:
            return {"refreshed": 0, "errors": [{"error": "HTTP 403"}]}
        self.accounts[token].update(
            {
                "email": "user@outlook.com",
                "status": "正常",
                "quota": 25,
                "invalid_count": 0,
                "last_refresh_error": None,
                "last_token_refresh_error": None,
                "last_quota_refresh_error": None,
                "quota_refresh_failure_kind": None,
                "quota_refresh_fail_count": 0,
            }
        )
        return {"refreshed": 1, "errors": []}

    def resolve_access_token(self, token):
        return token

    def update_account(self, token, updates, quiet=False):
        self.events.append(f"update:{token}")
        self.accounts[token].update(updates)
        return dict(self.accounts[token])

    def delete_accounts(self, tokens, include_items=False):
        removed = 0
        for token in tokens:
            self.events.append(f"delete:{token}")
            removed += self.accounts.pop(token, None) is not None
        return {"removed": int(removed)}

    def _is_image_account_schedulable(self, account):
        return (
            account.get("status") == "正常"
            and int(account.get("quota") or 0) > 0
            and account.get("panda_receive_state") == "verified_ready"
        )


class ResetFallbackAccountService(FakeAccountService):
    def __init__(self) -> None:
        super().__init__(refresh_ok=True)
        self.login_calls = 0

    def _login_with_password(self, email, password, otp_resolver=None):
        self.login_calls += 1
        self.events.append(f"login:{self.login_calls}")
        if self.login_calls == 1:
            return {
                "ok": False,
                "error": "password_verify_failed_401",
                "detail": {"error": {"code": "invalid_username_or_password"}},
            }
        self.asserted_otp = otp_resolver() if otp_resolver else None
        return {
            "ok": True,
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "id_token": "new-id",
        }


class PandaOutlookRecoveryTests(unittest.TestCase):
    def test_extract_error_code_accepts_top_level_account_deactivated_response(self) -> None:
        self.assertEqual(
            recovery._extract_error_code(
                {
                    "message": "You do not have an account because it has been deleted or deactivated.",
                    "code": "account_deactivated",
                }
            ),
            "account_deactivated",
        )

    def test_outlook_code_boundary_tolerates_received_at_clock_skew_after_priming(self) -> None:
        now = datetime(2026, 7, 13, 8, 30, 0, tzinfo=timezone.utc)

        tolerant = recovery.outlook_code_not_before(now, tolerate_clock_skew=True)
        strict = recovery.outlook_code_not_before(now, tolerate_clock_skew=False)

        self.assertEqual((now - tolerant).total_seconds(), 45)
        self.assertEqual(strict, now)

    def _preflight_ok(self):
        return patch.object(
            recovery,
            "preflight_mailbox_access",
            return_value={"ok": True, "message_count": 1, "imap_host": "outlook.live.com"},
        )

    def test_build_mail_config_prefers_live_host_for_consumer_outlook(self) -> None:
        service = FakeAccountService(refresh_ok=True)
        config = recovery.build_mail_config(
            service,
            {
                "email": "user@outlook.com",
                "password": "mail-password",
                "client_id": "client",
                "refresh_token": "mail-refresh",
            },
            timeout=60,
            interval=1,
            proxy_url="http://user:pass@proxy-one.example:8080",
        )
        provider = config["providers"][0]
        self.assertEqual(provider["mode"], "auto")
        self.assertEqual(provider["imap_host"], "outlook.live.com")
        self.assertEqual(config["proxy"], "http://user:pass@proxy-one.example:8080")
        self.assertTrue(config["api_use_register_proxy"])

    def test_recover_one_fails_fast_when_mailbox_preflight_fails(self) -> None:
        service = FakeAccountService(refresh_ok=True)
        account = service.get_account("old-token")
        credential = {
            "email": "user@outlook.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "mail-refresh",
        }
        proxy = recovery.ProxySpec("http://user:pass@1.2.3.4:8080", "1.2.3.4:8080")

        with (
            patch.object(recovery, "choose_working_proxy", return_value=(proxy, [{"ok": True}])),
            patch.object(recovery, "preflight_mailbox_access", side_effect=RuntimeError("graph denied; imap broken")),
        ):
            row = recovery.recover_one_account(
                index=1,
                account=account,
                credential=credential,
                proxy_specs=[proxy],
                account_service=service,
                wait_for_code=lambda config, mailbox: "123456",
                otp_timeout=60,
                otp_interval=1,
                proxy_attempts=1,
                proxy_timeout=5,
            )

        self.assertFalse(row["ok"])
        self.assertEqual(row["stage"], "mailbox_preflight")
        self.assertIn("mailbox_preflight", row["error"])
        self.assertEqual(service.events, [])

    def test_recover_one_accepts_chatgpt_email_otp_access_token_without_refresh_token(self) -> None:
        service = ResetFallbackAccountService()
        account = service.get_account("old-token")
        credential = {
            "email": "user@outlook.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "mail-refresh",
        }
        proxy = recovery.ProxySpec("http://user:pass@1.2.3.4:8080", "1.2.3.4:8080")
        email_login_result = {
            "ok": True,
            "access_token": "new-token",
            "refresh_token": "",
            "id_token": "",
            "chatgpt_session_token": "session-token",
            "chatgpt_session_expires": "2026-07-20T00:00:00Z",
        }

        with (
            patch.object(recovery, "choose_working_proxy", return_value=(proxy, [{"ok": True}])),
            self._preflight_ok(),
            patch.object(recovery, "login_with_chatgpt_email_otp", return_value=email_login_result) as email_otp_login,
        ):
            row = recovery.recover_one_account(
                index=1,
                account=account,
                credential=credential,
                proxy_specs=[proxy],
                account_service=service,
                wait_for_code=lambda config, mailbox: "123456",
                otp_timeout=60,
                otp_interval=1,
                proxy_attempts=1,
                proxy_timeout=5,
            )

        self.assertTrue(row["ok"])
        self.assertTrue(row["login_via_chatgpt_email_otp"])
        self.assertFalse(row["has_refresh_token"])
        self.assertTrue(row["has_chatgpt_session_token"])
        self.assertEqual(service.accounts["new-token"]["chatgpt_session_token"], "session-token")
        self.assertEqual(email_otp_login.call_args.kwargs["proxy"], proxy.url)

    def test_account_deactivated_email_otp_is_terminal_and_does_not_reset_password(self) -> None:
        service = FakeAccountService(refresh_ok=True)
        account = service.get_account("old-token")
        credential = {
            "email": "user@outlook.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "mail-refresh",
        }
        proxy = recovery.ProxySpec("http://user:pass@1.2.3.4:8080", "1.2.3.4:8080")
        deactivated = {
            "ok": False,
            "stage": "validate_email_otp",
            "otp_attempts": 1,
            "error_code": "account_deactivated",
            "terminal_reason": "account_deactivated",
            "error": "nextauth_otp_http_403: account_deactivated",
        }

        with (
            patch.object(recovery, "choose_working_proxy", return_value=(proxy, [{"ok": True}])),
            self._preflight_ok(),
            patch.object(
                recovery,
                "login_with_password_retries",
                return_value=({"ok": False, "error": "missing_openai_password"}, 0),
            ),
            patch.object(recovery, "login_with_chatgpt_email_otp", return_value=deactivated),
            patch.object(recovery, "reset_openai_password") as reset_password,
        ):
            row = recovery.recover_one_account(
                index=1,
                account=account,
                credential=credential,
                proxy_specs=[proxy],
                account_service=service,
                wait_for_code=lambda config, mailbox: "123456",
                otp_timeout=60,
                otp_interval=1,
                proxy_attempts=1,
                proxy_timeout=5,
                allow_password_reset=True,
            )

        self.assertFalse(row["ok"])
        self.assertEqual(row["terminal_reason"], "account_deactivated")
        self.assertEqual(row["stage"], "terminal")
        self.assertIn("OpenAI 账号已删除或停用", row["error"])
        self.assertTrue(row["terminal_persisted"])
        terminal = service.accounts["old-token"]
        self.assertEqual(terminal["status"], "禁用")
        self.assertEqual(terminal["quota"], 0)
        self.assertEqual(terminal["outlook_recovery_state"], "terminal")
        self.assertEqual(terminal["outlook_recovery_terminal_reason"], "account_deactivated")
        reset_password.assert_not_called()

    def test_mark_terminal_account_ignores_non_terminal_reason(self) -> None:
        service = FakeAccountService(refresh_ok=True)

        updated = recovery.mark_terminal_account(service, "old-token", "timeout")

        self.assertIsNone(updated)
        self.assertNotIn("outlook_recovery_state", service.accounts["old-token"])

    def test_recover_one_uses_token_returned_by_password_reset_without_second_login(self) -> None:
        service = ResetFallbackAccountService()
        account = service.get_account("old-token")
        credential = {
            "email": "user@outlook.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "mail-refresh",
        }
        proxy = recovery.ProxySpec("http://user:pass@1.2.3.4:8080", "1.2.3.4:8080")
        reset_login_result = {
            "ok": True,
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "id_token": "new-id",
        }

        with (
            patch.object(recovery, "choose_working_proxy", return_value=(proxy, [{"ok": True}])),
            self._preflight_ok(),
            patch.object(recovery, "generate_recovery_password", return_value="R9!replacement-password"),
            patch.object(recovery, "login_with_chatgpt_email_otp", return_value={"ok": False, "stage": "session", "error": "probe failed"}),
            patch.object(
                recovery,
                "reset_openai_password",
                return_value={"ok": True, "otp_attempts": 1, "login_result": reset_login_result},
            ),
        ):
            row = recovery.recover_one_account(
                index=1,
                account=account,
                credential=credential,
                proxy_specs=[proxy],
                account_service=service,
                wait_for_code=lambda config, mailbox: "123456",
                otp_timeout=60,
                otp_interval=1,
                proxy_attempts=1,
                proxy_timeout=5,
                allow_password_reset=True,
            )

        self.assertTrue(row["ok"])
        self.assertTrue(row["login_via_password_reset_code"])
        self.assertEqual(service.login_calls, 1)
        self.assertEqual(service.accounts["new-token"]["password"], "R9!replacement-password")

    def test_recover_one_resets_invalid_password_then_continues_full_chain(self) -> None:
        service = ResetFallbackAccountService()
        account = service.get_account("old-token")
        credential = {
            "email": "user@outlook.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "mail-refresh",
        }
        proxy = recovery.ProxySpec("http://user:pass@1.2.3.4:8080", "1.2.3.4:8080")
        activated: list[str] = []

        with (
            patch.object(recovery, "choose_working_proxy", return_value=(proxy, [{"ok": True}])),
            self._preflight_ok(),
            patch.object(recovery, "generate_recovery_password", return_value="R9!replacement-password"),
            patch.object(recovery, "login_with_chatgpt_email_otp", return_value={"ok": False, "stage": "session", "error": "probe failed"}),
            patch.object(recovery, "reset_openai_password", return_value={"ok": True, "otp_attempts": 2}),
        ):
            row = recovery.recover_one_account(
                index=1,
                account=account,
                credential=credential,
                proxy_specs=[proxy],
                account_service=service,
                wait_for_code=lambda config, mailbox: "123456",
                otp_timeout=60,
                otp_interval=1,
                proxy_attempts=1,
                proxy_timeout=5,
                activate_login_proxy=activated.append,
                allow_password_reset=True,
            )

        self.assertTrue(row["ok"])
        self.assertTrue(row["password_reset"])
        self.assertEqual(row["password_reset_otp_attempts"], 2)
        self.assertEqual(activated, [proxy.url])
        self.assertEqual(service.accounts["new-token"]["password"], "R9!replacement-password")
        self.assertNotIn("old-token", service.accounts)

    def test_generated_recovery_password_meets_current_minimum_without_email_data(self) -> None:
        password = recovery.generate_recovery_password()

        self.assertGreaterEqual(len(password), 12)
        self.assertRegex(password, r"[A-Z]")
        self.assertRegex(password, r"[a-z]")
        self.assertRegex(password, r"[0-9]")
        self.assertRegex(password, r"[^A-Za-z0-9]")
        self.assertNotIn("@", password)

    def test_recover_one_deletes_old_token_only_after_webshare_verification(self) -> None:
        service = FakeAccountService(refresh_ok=True)
        account = service.get_account("old-token")
        credential = {
            "email": "user@outlook.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "mail-refresh",
        }
        proxy = recovery.ProxySpec("http://user:pass@1.2.3.4:8080", "1.2.3.4:8080")

        with (
            patch.object(recovery, "choose_working_proxy", return_value=(proxy, [{"ok": True}])),
            self._preflight_ok(),
        ):
            row = recovery.recover_one_account(
                index=1,
                account=account,
                credential=credential,
                proxy_specs=[proxy],
                account_service=service,
                wait_for_code=lambda config, mailbox: "123456",
                otp_timeout=60,
                otp_interval=1,
                proxy_attempts=1,
                proxy_timeout=5,
            )

        self.assertTrue(row["ok"])
        self.assertTrue(row["schedulable"])
        self.assertNotIn("old-token", service.accounts)
        self.assertIn("new-token", service.accounts)
        # 新号应有独立指纹，不得沿用旧 device-id（旧号本无 fp）
        self.assertIsInstance(service.accounts["new-token"].get("fp"), dict)
        self.assertLess(service.events.index("refresh:new-token"), service.events.index("delete:old-token"))
        self.assertEqual(service.asserted_otp, "123456")

    def test_recover_one_rolls_back_new_token_when_webshare_verification_fails(self) -> None:
        service = FakeAccountService(refresh_ok=False)
        account = service.get_account("old-token")
        credential = {
            "email": "user@outlook.com",
            "password": "mail-password",
            "client_id": "client",
            "refresh_token": "mail-refresh",
        }
        proxy = recovery.ProxySpec("http://user:pass@1.2.3.4:8080", "1.2.3.4:8080")

        with (
            patch.object(recovery, "choose_working_proxy", return_value=(proxy, [{"ok": True}])),
            self._preflight_ok(),
        ):
            row = recovery.recover_one_account(
                index=1,
                account=account,
                credential=credential,
                proxy_specs=[proxy],
                account_service=service,
                wait_for_code=lambda config, mailbox: "123456",
                otp_timeout=60,
                otp_interval=1,
                proxy_attempts=1,
                proxy_timeout=5,
            )

        self.assertFalse(row["ok"])
        self.assertIn("old-token", service.accounts)
        self.assertNotIn("new-token", service.accounts)
        self.assertNotIn("delete:old-token", service.events)
        self.assertTrue(row["new_token_rolled_back"])

    def test_selects_rejected_outlook_even_when_quota_is_zero(self) -> None:
        accounts = [
            {
                "email": "zero@outlook.com",
                "access_token": "old-zero",
                "status": "异常",
                "quota": 0,
                "panda_receive_state": "rejected",
                "invalid_count": 3,
            },
            {
                "email": "healthy@outlook.com",
                "access_token": "healthy",
                "status": "正常",
                "quota": 25,
                "panda_receive_state": "verified_ready",
                "invalid_count": 0,
            },
            {
                "email": "other@example.com",
                "access_token": "other",
                "status": "异常",
                "quota": 25,
                "panda_receive_state": "rejected",
            },
        ]

        selected = recovery.select_recovery_targets(
            accounts,
            credential_emails={"zero@outlook.com", "healthy@outlook.com"},
        )

        self.assertEqual([item["email"] for item in selected], ["zero@outlook.com"])

    def test_terminal_account_deactivated_is_not_selected_again(self) -> None:
        selected = recovery.select_recovery_targets(
            [
                {
                    "email": "terminal@outlook.com",
                    "access_token": "terminal-token",
                    "status": "禁用",
                    "panda_receive_state": "rejected",
                    "outlook_recovery_state": "terminal",
                    "outlook_recovery_terminal_reason": "account_deactivated",
                }
            ],
            credential_emails={"terminal@outlook.com"},
            target_emails={"terminal@outlook.com"},
        )

        self.assertEqual(selected, [])

    def test_does_not_relogin_a_verified_quota_zero_account_without_failure_evidence(self) -> None:
        accounts = [
            {
                "email": "limited@outlook.com",
                "access_token": "limited",
                "status": "限流",
                "quota": 0,
                "panda_receive_state": "verified_ready",
                "invalid_count": 0,
            }
        ]

        selected = recovery.select_recovery_targets(
            accounts,
            credential_emails={"limited@outlook.com"},
        )

        self.assertEqual(selected, [])

    def test_does_not_relogin_a_transient_quota_probe_failure_by_default(self) -> None:
        accounts = [
            {
                "email": "transient@outlook.com",
                "access_token": "transient",
                "status": "正常",
                "quota": 25,
                "panda_receive_state": "verified_ready",
                "quota_refresh_fail_count": 1,
                "last_quota_refresh_error": "operation timed out",
                "quota_refresh_failure_kind": "transient",
            }
        ]

        selected = recovery.select_recovery_targets(
            accounts,
            credential_emails={"transient@outlook.com"},
        )

        self.assertEqual(selected, [])

    def test_explicit_targets_are_exact_and_limit_is_applied_after_filtering(self) -> None:
        accounts = [
            {"email": "a@outlook.com", "access_token": "a", "status": "正常", "quota": 25},
            {"email": "b@outlook.com", "access_token": "b", "status": "异常", "quota": 0},
        ]

        selected = recovery.select_recovery_targets(
            accounts,
            credential_emails={"a@outlook.com", "b@outlook.com"},
            target_emails={"a@outlook.com", "b@outlook.com"},
            limit=1,
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["email"], "a@outlook.com")

    def test_staged_account_does_not_inherit_old_fingerprint_or_failure_state(self) -> None:
        old = {
            "email": "user@outlook.com",
            "password": "openai-password",
            "fp": {"oai-device-id": "old-device"},
            "invalid_count": 9,
            "last_token_refresh_error": "invalid",
            "panda_receive_state": "rejected",
        }
        login = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "id_token": "new-id",
            "expires_at": 123,
        }

        item = recovery.build_staged_account(old, login, "http://proxy", "2026-07-10T00:00:00+00:00")

        self.assertIn("fp", item)
        self.assertNotEqual(str((item.get("fp") or {}).get("oai-device-id") or ""), "old-device")
        self.assertEqual(item["access_token"], "new-access")
        self.assertEqual(item["status"], "异常")
        self.assertEqual(item["quota"], 0)
        self.assertEqual(item["panda_receive_state"], "incoming")
        self.assertEqual(item["invalid_count"], 0)
        self.assertIsNone(item["last_token_refresh_error"])

    def test_verified_updates_clear_all_scheduler_blockers(self) -> None:
        updates = recovery.build_verified_updates("2026-07-10T00:00:00+00:00")

        self.assertEqual(updates["panda_receive_state"], "verified_ready")
        self.assertEqual(updates["panda_sync_state"], "ready")
        self.assertEqual(updates["invalid_count"], 0)
        self.assertEqual(updates["quota_refresh_fail_count"], 0)
        self.assertIsNone(updates["quota_refresh_failure_kind"])
        self.assertIsNone(updates["panda_rejected_at"])
        self.assertIsNone(updates["last_quota_refresh_error"])

    def test_webshare_proxy_parser_encodes_credentials_without_exposing_them_in_label(self) -> None:
        spec = recovery.parse_proxy_line("1.2.3.4:8080:user@example.com:p@ss word")

        self.assertEqual(spec.label, "1.2.3.4:8080")
        self.assertIn("user%40example.com", spec.url)
        self.assertIn("p%40ss%20word", spec.url)
        self.assertNotIn("user@example.com", spec.label)
        self.assertNotIn("p@ss", spec.label)

    def test_sanitize_error_redacts_tokens_and_email(self) -> None:
        value = (
            "Bearer eyJabcdefghijklmnopqrstuvwxyz.abcdefghijklmnopqrstuvwxyz.secret "
            "refresh_token=super-secret-value user@outlook.com"
        )

        sanitized = recovery.sanitize_error(value)

        self.assertNotIn("super-secret-value", sanitized)
        self.assertNotIn("eyJabcdefghijklmnopqrstuvwxyz", sanitized)
        self.assertNotIn("user@outlook.com", sanitized)
        self.assertIn("<redacted>", sanitized)


if __name__ == "__main__":
    unittest.main()
