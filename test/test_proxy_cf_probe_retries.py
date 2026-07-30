from __future__ import annotations

import unittest
from unittest.mock import patch

from services.proxy_cf_probe import is_transient_cf403_probe, probe_proxy_cf_with_retries


class ProbeProxyCfWithRetriesTest(unittest.TestCase):
    def test_transient_cf403_detection(self) -> None:
        self.assertTrue(is_transient_cf403_probe({"ok": False, "cf_classification": "cf403"}))
        self.assertFalse(is_transient_cf403_probe({"ok": True, "cf_classification": "none"}))
        self.assertFalse(is_transient_cf403_probe({"ok": False, "cf_classification": "error", "error": "timeout"}))

    def test_retries_until_success(self) -> None:
        seq = [
            {"ok": False, "cf403": True, "cf_classification": "cf403", "elapsed_ms": 100},
            {"ok": False, "cf403": True, "cf_classification": "cf403", "elapsed_ms": 120},
            {"ok": True, "cf_classification": "none", "elapsed_ms": 80},
        ]

        def fake_probe(_proxy: str, *, timeout: float = 45.0):
            return dict(seq.pop(0))

        with patch("services.proxy_cf_probe.probe_proxy_cf", side_effect=fake_probe):
            with patch("services.proxy_cf_probe.time.sleep"):
                out = probe_proxy_cf_with_retries(
                    "http://u:p@1.2.3.4:8080",
                    retry_count=3,
                    retry_window_sec=300.0,
                    min_retry_gap_sec=1.0,
                )
        self.assertTrue(out["ok"])
        self.assertEqual(out["probe_attempts"], 3)
        self.assertEqual(out["probe_retries"], 2)
        self.assertEqual(out["elapsed_ms"], 300)

    def test_stops_on_non_transient_failure(self) -> None:
        with patch(
            "services.proxy_cf_probe.probe_proxy_cf",
            return_value={"ok": False, "cf_classification": "error", "error": "timeout", "elapsed_ms": 50},
        ):
            out = probe_proxy_cf_with_retries("http://u:p@1.2.3.4:8080", retry_count=3)
        self.assertFalse(out["ok"])
        self.assertEqual(out["probe_attempts"], 1)
        self.assertEqual(out["probe_retries"], 0)


if __name__ == "__main__":
    unittest.main()
