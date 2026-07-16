from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "repair_panda_account_identity",
    ROOT / "scripts" / "repair_panda_account_identity.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MOD)
run_audit = _MOD.run_audit


class RepairPandaAccountIdentityTests(unittest.TestCase):
    def test_audit_closes_eighteen_synthetic_accounts(self) -> None:
        accounts = []
        for i in range(12):
            accounts.append(
                {
                    "access_token": f"shared-{i}",
                    "email": f"s{i}@outlook.com",
                    "status": "正常",
                    "proxy": "http://user-shared:pass@proxy.example:8080",
                    "proxy_provider": "webshare",
                    "proxy_scope": "account_sticky",
                    "lifecycle_ip_mode": "sticky_one_ip_full",
                    "success": 1,
                }
            )
        for i in range(6):
            accounts.append(
                {
                    "access_token": f"missing-{i}",
                    "email": f"m{i}@outlook.com",
                    "status": "异常",
                    "panda_receive_state": "rejected",
                    "invalid_count": 1,
                    "fail": 1,
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            summary = run_audit(accounts, out)
            self.assertEqual(summary["total"], 18)
            self.assertTrue(summary["closed"])
            inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(len(inventory["rows"]), 18)
            sig = json.loads((out / "signature-comparison.json").read_text(encoding="utf-8"))
            self.assertEqual(sig["hypothesis"], "H1_true_shared_endpoint")
            self.assertTrue((out / "repair-classification.md").exists())


if __name__ == "__main__":
    unittest.main()
