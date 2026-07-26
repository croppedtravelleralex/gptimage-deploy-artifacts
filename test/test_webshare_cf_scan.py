from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.proxy_cf_probe import classify_cf_probe, probe_proxy_cf
from services.webshare_cf_scan_service import WebshareCfScanService, _pool_path, _select_batch, build_pool_inventory


class ProxyCfProbeTests(unittest.TestCase):
    def test_classify_cf403_on_requirements_block(self) -> None:
        label = classify_cf_probe(
            home_status=200,
            requirements_status=403,
            requirements_ok=False,
            home_cf=False,
            requirements_cf=True,
        )
        self.assertEqual(label, "cf403")

    def test_classify_home_soft_fail_when_prepare_not_ok(self) -> None:
        label = classify_cf_probe(
            home_status=403,
            requirements_status=403,
            requirements_ok=False,
            home_cf=True,
            requirements_cf=True,
        )
        self.assertEqual(label, "cf403")

    @patch("services.proxy_cf_probe.measure_proxy_egress_ip", return_value={"ok": True, "ip": "1.2.3.4"})
    @patch("curl_cffi.requests.Session")
    def test_probe_proxy_cf_marks_cf403(self, session_cls, _egress) -> None:
        session = session_cls.return_value
        home = unittest.mock.Mock(status_code=403, text="<html>cloudflare</html>")
        prep = unittest.mock.Mock(status_code=403, text="blocked")
        session.get.return_value = home
        session.post.return_value = prep
        out = probe_proxy_cf("http://user:pass@1.2.3.4:8080")
        self.assertTrue(out["cf403"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["cf_classification"], "cf403")


class WebshareCfScanServiceTests(unittest.TestCase):
    def test_select_batch_rotates_and_skips_quarantined(self) -> None:
        pool = ["http://a:1", "http://b:2", "http://c:3"]
        with patch("services.webshare_cf_scan_service.is_gpt_unavailable_proxy", side_effect=lambda p: "b:2" in p):
            batch, next_offset = _select_batch(pool, offset=0, batch_size=2, skip_quarantined=True)
        self.assertEqual(batch, ["http://a:1", "http://c:3"])
        self.assertEqual(next_offset, 0)

    @patch("services.webshare_cf_scan_service.mark_gpt_unavailable")
    @patch("services.webshare_cf_scan_service.probe_proxy_cf")
    @patch("services.webshare_cf_scan_service.load_proxy_pool", return_value=["http://a:1", "http://b:2"])
    def test_run_once_quarantines_cf403(self, _load, probe_mock, quarantine_mock) -> None:
        probe_mock.side_effect = [
            {"ok": True, "cf403": False, "proxy_endpoint": "a:1"},
            {"ok": False, "cf403": True, "proxy_endpoint": "b:2"},
        ]
        svc = WebshareCfScanService()
        with patch(
            "services.webshare_cf_scan_service._settings",
            return_value={
                "enabled": True,
                "batch_size": 2,
                "workers": 2,
                "auto_quarantine": True,
                "skip_quarantined": False,
                "probe_timeout_sec": 10.0,
            },
        ):
            report = svc.run_once(force=True)
        self.assertEqual(report["summary"]["cf403"], 1)
        quarantine_mock.assert_called_once()

    @patch("services.webshare_cf_scan_service.load_proxy_pool", return_value=["http://u:p@1.1.1.1:1111", "http://u:p@2.2.2.2:2222"])
    @patch("services.webshare_cf_scan_service.list_quarantine_entries", return_value=[{"endpoint": "2.2.2.2:2222", "host": "2.2.2.2", "reason": "cf403_scan", "former_account": None}])
    @patch("services.webshare_cf_scan_service.is_gpt_unavailable_proxy", side_effect=lambda endpoint: str(endpoint).startswith("2.2.2.2"))
    def test_build_pool_inventory_counts(self, _blocked, _quarantine, _pool) -> None:
        inventory = build_pool_inventory(
            {},
            latest_report={
                "generated_at": "2026-07-23T00:00:00+00:00",
                "nodes": [
                    {"proxy_endpoint": "1.1.1.1:1111", "ok": True, "egress": {"ip": "1.1.1.1"}},
                    {"proxy_endpoint": "2.2.2.2:2222", "ok": False, "cf403": True, "home_status": 403},
                ],
            },
        )
        self.assertEqual(inventory["pool_total"], 2)
        self.assertEqual(inventory["available_count"], 1)
        self.assertEqual(inventory["cf403_count"], 1)
        self.assertEqual(inventory["cf403_nodes"][0]["endpoint"], "2.2.2.2:2222")
        self.assertEqual(inventory["available_endpoints"][0]["endpoint"], "1.1.1.1:1111")

    def test_pool_path_uses_absolute_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool_file = Path(tmp) / "webshare_pool.txt"
            pool_file.write_text("1.2.3.4:8080:user:pass\n", encoding="utf-8")
            resolved = _pool_path({"pool_path": str(pool_file)})
            self.assertEqual(resolved, pool_file)


if __name__ == "__main__":
    unittest.main()
