from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.storage.json_storage import JSONStorageBackend


class JSONStorageBackendTest(unittest.TestCase):
    def test_save_accounts_replaces_file_and_keeps_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "accounts.json"
            path.write_text(json.dumps([{"id": "old"}]), encoding="utf-8")
            storage = JSONStorageBackend(path)

            storage.save_accounts([{"id": "new", "quota": 1}])

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [{"id": "new", "quota": 1}])
            self.assertEqual(json.loads(path.with_name("accounts.json.bak").read_text(encoding="utf-8")), [{"id": "old"}])
            self.assertFalse(list(path.parent.glob(".accounts.json.*.tmp")))

    def test_save_auth_keys_uses_items_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "accounts.json"
            auth_path = Path(tmp_dir) / "auth_keys.json"
            storage = JSONStorageBackend(path, auth_path)

            storage.save_auth_keys([{"id": "key-1", "enabled": True}])

            self.assertEqual(
                json.loads(auth_path.read_text(encoding="utf-8")),
                {"items": [{"id": "key-1", "enabled": True}]},
            )
            self.assertEqual(storage.load_auth_keys(), [{"id": "key-1", "enabled": True}])


if __name__ == "__main__":
    unittest.main()
