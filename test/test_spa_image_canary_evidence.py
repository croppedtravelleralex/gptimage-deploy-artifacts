from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts import _tmp_spa_image_bench3 as bench


class SpaImageCanaryEvidenceTest(unittest.TestCase):
    def test_spa_tool_uses_strict_image_envelope(self) -> None:
        prepare = bench._make_image_prepare_body(
            "draw a circle",
            "auto",
            "Asia/Tokyo",
            -540,
            spa_tool_path=True,
        )
        start = bench._make_image_start_body(
            "draw a circle",
            "auto",
            "Asia/Tokyo",
            -540,
            spa_tool_path=True,
        )
        headers = bench.build_image_start_headers(bench._Req("requirements"), "", spa_tool_path=True)

        self.assertEqual(prepare["system_hints"], [])
        self.assertEqual(prepare["partial_query"]["content"]["parts"], ["draw a circle"])
        self.assertEqual(start["system_hints"], [])
        self.assertNotIn("metadata", start["messages"][0])
        self.assertNotIn("create_time", start["messages"][0])
        self.assertNotIn("X-Conduit-Token", headers)
        self.assertNotIn("X-Oai-Turn-Trace-Id", headers)

    def test_atomic_evidence_and_image_metadata_are_complete(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (13, 17), color=(12, 34, 56)).save(buffer, format="PNG")
        payload = buffer.getvalue()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = bench.persist_image(root, 0, payload)
            evidence = {
                "schema_version": "pure-http-image-canary/v1",
                "account": {"hash": bench.account_hash("access-token-value")},
                "proxy": {"provider": "webshare", "hash": bench.proxy_hash("http://user:pass@proxy:80")},
                "egress": {"ok": True, "ip": "203.0.113.8"},
                "request_shapes": {"prepare": {}, "start": {}},
                "conversation": {"id": "cid", "has_image_gen": True},
                "images": [image],
            }
            result_path = root / "canary_result.json"
            bench.write_evidence(result_path, evidence)

            stored = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["images"][0]["format"], "png")
            self.assertEqual(stored["images"][0]["width"], 13)
            self.assertEqual(stored["images"][0]["height"], 17)
            self.assertEqual(stored["images"][0]["bytes"], len(payload))
            self.assertEqual(stored["images"][0]["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual((root / stored["images"][0]["path"]).read_bytes(), payload)
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_error_and_identity_evidence_do_not_leak_credentials(self) -> None:
        email = "user@example.com"
        proxy = "http://webshare-user:super-secret@proxy.example:8080"
        error = bench._sanitize_error(
            f"Bearer token-value failed for {email} via {proxy}; access_token=top-secret"
        )

        self.assertEqual(len(bench.account_hash("access-token-value")), 12)
        self.assertEqual(len(bench.proxy_hash(proxy)), 12)
        self.assertNotIn(email, error)
        self.assertNotIn("webshare-user", error)
        self.assertNotIn("super-secret", error)
        self.assertNotIn("token-value", error)
        self.assertNotIn("top-secret", error)


if __name__ == "__main__":
    unittest.main()
