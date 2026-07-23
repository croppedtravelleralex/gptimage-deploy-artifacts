from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.proxy_quarantine import is_gpt_unavailable_proxy, mark_gpt_unavailable, proxy_endpoint_key


class ProxyQuarantineTests(unittest.TestCase):
    def test_endpoint_key(self) -> None:
        self.assertEqual(
            proxy_endpoint_key("http://u:p@92.113.246.215:5800"),
            "92.113.246.215:5800",
        )

    def test_mark_and_detect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpt_unavailable_proxies.json"
            mark_gpt_unavailable(
                "http://u:p@92.113.246.215:5800",
                reason="gpt_unavailable",
                former_account="a@b.com",
                path=path,
            )
            self.assertTrue(is_gpt_unavailable_proxy("http://x:y@92.113.246.215:5800", path=path))
            self.assertFalse(is_gpt_unavailable_proxy("http://x:y@92.113.246.12:5597", path=path))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("92.113.246.215:5800", payload["endpoints"])


if __name__ == "__main__":
    unittest.main()
