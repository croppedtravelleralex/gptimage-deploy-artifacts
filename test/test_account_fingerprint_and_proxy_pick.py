from __future__ import annotations

import unittest

from services.account_fingerprint import (
    build_aligned_chrome_fp,
    build_diversified_fp,
    ensure_complete_fp,
    pick_fp_profile,
)


class AccountFingerprintTests(unittest.TestCase):
    def test_build_aligned_chrome_fp_not_edge(self) -> None:
        fp = build_aligned_chrome_fp()
        self.assertEqual(fp["impersonate"], "chrome120")
        self.assertNotIn("Edg/", fp["user-agent"])
        self.assertIn("Chrome/", fp["user-agent"])
        self.assertTrue(fp["oai-device-id"])
        self.assertTrue(fp["oai-session-id"])
        self.assertEqual(fp["sec-ch-ua-arch"], '"x86"')
        self.assertTrue(fp.get("accept-language"))

    def test_diversified_fp_stable_for_same_seed(self) -> None:
        a = build_diversified_fp("observe-ivetterock@example.com")
        b = build_diversified_fp("observe-ivetterock@example.com")
        self.assertEqual(a["impersonate"], b["impersonate"])
        self.assertEqual(a["sec-ch-ua-platform"], b["sec-ch-ua-platform"])
        self.assertEqual(a["accept-language"], b["accept-language"])
        self.assertEqual(a["user-agent"], b["user-agent"])

    def test_diversified_fp_differs_across_seeds(self) -> None:
        profiles = {pick_fp_profile(f"seed-{i}")["impersonate"] + pick_fp_profile(f"seed-{i}")["platform"] for i in range(40)}
        self.assertGreaterEqual(len(profiles), 3)

    def test_ensure_complete_fp_preserves_complete_device_ids(self) -> None:
        account = {
            "email": "keep@example.com",
            "fp": {
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "impersonate": "chrome120",
                "oai-device-id": "dev-keep",
                "oai-session-id": "sess-keep",
                "sec-ch-ua": '"Google Chrome";v="120", "Not?A_Brand";v="8", "Chromium";v="120"',
                "sec-ch-ua-arch": '"x86"',
                "sec-ch-ua-bitness": '"64"',
                "sec-ch-ua-full-version": '"120.0.0.0"',
                "sec-ch-ua-full-version-list": '"Chromium";v="120.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="120.0.0.0"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-ch-ua-platform-version": '"10.0.0"',
            },
        }
        fp, filled = ensure_complete_fp(account)
        self.assertEqual(fp["oai-device-id"], "dev-keep")
        self.assertEqual(fp["oai-session-id"], "sess-keep")
        self.assertEqual(fp["impersonate"], "chrome120")
        self.assertTrue(fp.get("accept-language"))

    def test_ensure_complete_fp_fills_incomplete_from_seed(self) -> None:
        fp, filled = ensure_complete_fp({"email": "new-observe@example.com", "fp": {}})
        self.assertTrue(filled)
        self.assertTrue(fp["oai-device-id"])
        self.assertTrue(fp["impersonate"].startswith("chrome"))
        self.assertIn(fp["sec-ch-ua-platform"], {'"Windows"', '"macOS"'})

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
        self.assertTrue(fp["impersonate"].startswith("chrome"))
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


if __name__ == "__main__":
    unittest.main()
