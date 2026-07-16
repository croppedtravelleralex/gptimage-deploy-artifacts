from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.account_identity import (
    PANDA_REQUIRED_IDENTITY_FIELDS,
    missing_panda_identity_fields,
    proxy_binding_hash,
)
from services.account_refresh_all_service import AccountRefreshAllService
from services.account_service import AccountService
from services.storage.json_storage import JSONStorageBackend


class AccountIdentityPersistenceTests(unittest.TestCase):
    def test_legacy_account_gets_one_persistent_complete_fp_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "accounts.json"
            path.write_text(
                json.dumps([{"access_token": "legacy-token", "status": "正常"}]),
                encoding="utf-8",
            )

            first = AccountService(JSONStorageBackend(path)).get_account("legacy-token") or {}
            persisted = json.loads(path.read_text(encoding="utf-8"))[0]
            second = AccountService(JSONStorageBackend(path)).get_account("legacy-token") or {}

            self.assertGreaterEqual(len(first.get("fp") or {}), 12)
            self.assertEqual(persisted.get("fp"), first.get("fp"))
            self.assertEqual(second.get("fp"), first.get("fp"))

    def test_ordinary_update_and_import_do_not_clear_or_rebind_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            original_proxy = "http://user-one:pass-one@proxy.example:8080"
            original = {
                "access_token": "token",
                "proxy": original_proxy,
                "proxy_provider": "webshare",
                "proxy_scope": "account_sticky",
                "proxy_egress_hash": "egress-one",
                "registration_proxy_hash": proxy_binding_hash(original_proxy),
                "lifecycle_ip_mode": "sticky_one_ip_full",
                "fp": {"oai-device-id": "device-one", "oai-session-id": "session-one"},
            }
            service.add_account_items([original], include_items=False)

            service.update_account(
                "token",
                {
                    "proxy": "http://user-two:pass-two@proxy.example:8080",
                    "proxy_egress_hash": "egress-two",
                    "fp": {"oai-device-id": "device-two"},
                    "quota": 9,
                },
                quiet=True,
            )
            service.import_account_items(
                [{"access_token": "token", "proxy": "", "fp": {}, "status": "正常"}],
                include_items=False,
            )

            account = service.get_account("token") or {}
            self.assertEqual(account.get("proxy"), original_proxy)
            self.assertEqual(account.get("proxy_egress_hash"), "egress-one")
            self.assertEqual((account.get("fp") or {}).get("oai-device-id"), "device-one")
            self.assertEqual(account.get("quota"), 9)
            self.assertGreaterEqual(int(account.get("identity_conflict_count") or 0), 1)
            self.assertIn("proxy", account.get("identity_last_conflict_fields") or [])

    def test_explicit_identity_update_can_rebind_and_recomputes_binding_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [{"access_token": "token", "proxy": "http://old:pass@old.example:8080"}],
                include_items=False,
            )
            new_proxy = "http://new:pass@new.example:8080"

            updated = service.update_account_identity(
                "token",
                {
                    "proxy": new_proxy,
                    "proxy_provider": "webshare",
                    "proxy_scope": "account_sticky",
                    "proxy_egress_hash": "egress-new",
                    "registration_proxy_hash": proxy_binding_hash(new_proxy),
                    "lifecycle_ip_mode": "sticky_one_ip_full",
                },
                reason="identity_backfill",
            ) or {}

            self.assertEqual(updated.get("proxy"), new_proxy)
            self.assertEqual(updated.get("proxy_binding_hash"), proxy_binding_hash(new_proxy))
            self.assertEqual(updated.get("identity_revision"), 1)
            self.assertEqual(updated.get("identity_update_reason"), "identity_backfill")

    def test_proxy_binding_hash_distinguishes_credential_selected_nodes(self) -> None:
        left = proxy_binding_hash("http://user-one:pass@proxy.example:8080")
        right = proxy_binding_hash("http://user-two:pass@proxy.example:8080")

        self.assertNotEqual(left, right)
        self.assertEqual(len(left), 24)
        self.assertNotIn("user-one", left)

    def test_panda_upload_requires_complete_reachable_identity(self) -> None:
        local = {
            "access_token": "local-token",
            "proxy": "http://127.0.0.1:41003",
            "proxy_provider": "webshare",
            "lifecycle_ip_mode": "sticky_one_ip_full",
        }
        self.assertIn("proxy_reachable_from_panda", missing_panda_identity_fields(local))
        with self.assertRaisesRegex(ValueError, "account_identity_incomplete"):
            AccountRefreshAllService._prepare_account_for_panda_upload(local)

        proxy = "http://user:pass@203.0.113.10:8000"
        complete = {
            "access_token": "remote-token",
            "proxy": proxy,
            "proxy_provider": "webshare",
            "proxy_scope": "account_sticky",
            "proxy_binding_hash": proxy_binding_hash(proxy),
            "proxy_egress_hash": "egress-hash",
            "registration_proxy_hash": proxy_binding_hash(proxy),
            "lifecycle_ip_mode": "sticky_one_ip_full",
            "fp": {
                "user-agent": "ua",
                "impersonate": "chrome120",
                "oai-device-id": "device",
                "oai-session-id": "session",
                "sec-ch-ua": "ch",
                "sec-ch-ua-arch": '"x86"',
                "sec-ch-ua-bitness": '"64"',
                "sec-ch-ua-full-version": '"120.0.0.0"',
                "sec-ch-ua-full-version-list": "list",
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-ch-ua-platform-version": '"10.0.0"',
            },
        }

        self.assertEqual(missing_panda_identity_fields(complete), [])
        self.assertTrue(PANDA_REQUIRED_IDENTITY_FIELDS)
        prepared = AccountRefreshAllService._prepare_account_for_panda_upload(complete)
        self.assertEqual(prepared.get("proxy"), proxy)
        self.assertEqual(prepared.get("proxy_egress_hash"), "egress-hash")


if __name__ == "__main__":
    unittest.main()
