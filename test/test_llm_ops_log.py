from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.log_service import LOG_TYPE_LLM_OPS, LogService, log_llm_ops


class LlmOpsLogTests(unittest.TestCase):
    def test_log_llm_ops_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs.jsonl"
            svc = LogService(path)
            with patch("services.log_service.log_service", svc):
                log_llm_ops(
                    source="L0",
                    kind="chat",
                    access_token="secret-token-value",
                    latency_ms=42,
                    outcome="ok",
                    prompt_shape={"chars": 12, "has_images": False},
                )
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            import json

            item = json.loads(lines[0])
            self.assertEqual(item["type"], LOG_TYPE_LLM_OPS)
            detail = item.get("detail") or {}
            self.assertEqual(detail["source"], "L0")
            self.assertEqual(detail["kind"], "chat")
            self.assertEqual(detail["latency_ms"], 42)
            self.assertEqual(detail["outcome"], "ok")
            self.assertNotIn("secret-token-value", lines[0])
            self.assertEqual(len(detail["account_hash"]), 12)


if __name__ == "__main__":
    unittest.main()
