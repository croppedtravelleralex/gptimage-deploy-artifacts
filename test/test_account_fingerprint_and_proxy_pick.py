from __future__ import annotations

import unittest

from services.account_fingerprint import build_aligned_chrome_fp, ensure_complete_fp
from services.register.openai_register import PlatformRegistrar, common_headers, navigate_headers, select_register_proxy


class AccountFingerprintTests(unittest.TestCase):
    def test_build_aligned_chrome_fp_not_edge(self) -> None:
        fp = build_aligned_chrome_fp()
        self.assertEqual(fp["impersonate"], "chrome120")
        self.assertNotIn("Edg/", fp["user-agent"])
        self.assertIn("Chrome/", fp["user-agent"])
        self.assertTrue(fp["oai-device-id"])
        self.assertTrue(fp["oai-session-id"])
        self.assertEqual(fp["sec-ch-ua-arch"], '"x86"')

    def test_ensure_complete_fp_fixes_edge_mismatch(self) -> None:
        account = {
            "fp": {
                "user-agent": "Mozilla/5.0 Edg/143.0.0.0",
                "impersonate": "chrome110",
                "oai-device-id": "dev-1",
                "oai-session-id": "sess-1",
            }
        }
        fp, filled = ensure_complete_fp(account)
        self.assertTrue(filled)
        self.assertEqual(fp["impersonate"], "chrome120")
        self.assertNotIn("Edg/", fp["user-agent"])
        self.assertEqual(fp["oai-device-id"], "dev-1")

    def test_ensure_complete_fp_normalizes_legacy_ch_arch(self) -> None:
        for legacy_arch in ('"x86_64"', "x86_64", '"amd64"', "amd64"):
            with self.subTest(legacy_arch=legacy_arch):
                fp, filled = ensure_complete_fp({
                    "fp": {
                        "sec-ch-ua-arch": legacy_arch,
                        "oai-device-id": "dev-legacy",
                        "oai-session-id": "sess-legacy",
                    }
                })

                self.assertTrue(filled)
                self.assertEqual(fp["sec-ch-ua-arch"], '"x86"')
                self.assertEqual(fp["oai-device-id"], "dev-legacy")
                self.assertEqual(fp["oai-session-id"], "sess-legacy")

    def test_registration_fingerprint_uses_canonical_ch_arch(self) -> None:
        registrar = object.__new__(PlatformRegistrar)
        registrar.clearance_user_agent = ""
        registrar.device_id = "dev-new"
        registrar.session_id = "sess-new"

        fp = registrar._account_fingerprint()

        self.assertEqual(common_headers["sec-ch-ua-arch"], '"x86"')
        self.assertEqual(navigate_headers["sec-ch-ua-arch"], '"x86"')
        self.assertEqual(fp["sec-ch-ua-arch"], '"x86"')
        self.assertEqual(fp["oai-device-id"], "dev-new")
        self.assertEqual(fp["oai-session-id"], "sess-new")


class SelectRegisterProxyTests(unittest.TestCase):
    def test_picks_unique_endpoints(self) -> None:
        pool = "\n".join(
            [
                "192.0.2.10:8000:user:pass",
                "192.0.2.11:8000:user:pass",
            ]
        )
        used: set[str] = set()
        first = select_register_proxy(pool, index=1, used_endpoints=used)
        second = select_register_proxy(pool, index=2, used_endpoints=used)
        self.assertIn("192.0.2.10:8000", first)
        self.assertIn("192.0.2.11:8000", second)
        self.assertEqual(len(used), 2)


if __name__ == "__main__":
    unittest.main()
