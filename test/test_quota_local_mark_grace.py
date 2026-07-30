from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")
os.environ.setdefault("STORAGE_BACKEND", "json")

from services.account_service import AccountService
from services.config import config
from services.storage.json_storage import JSONStorageBackend


def _service(tmp_dir: str) -> AccountService:
    return AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))


def test_timeout_pending_max_attempts_allows_zero() -> None:
    settings = config.get_image_task_queue_settings()
    original = dict(config.data.get("image_task_queue") or {})
    try:
        config.data["image_task_queue"] = {**original, "timeout_pending_max_attempts": 0}
        refreshed = config.get_image_task_queue_settings()
        assert refreshed.get("timeout_pending_max_attempts") == 0
    finally:
        config.data["image_task_queue"] = original


def test_remote_refresh_does_not_undo_recent_local_quota_decrement() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(tmp_dir)
        service.add_account_items(
            [
                {
                    "access_token": "tok-a",
                    "email": "a@example.com",
                    "status": "正常",
                    "quota": 5,
                    "image_quota_unknown": False,
                    "limits_progress": [
                        {"feature_name": "image_gen", "remaining": 5},
                    ],
                }
            ]
        )
        updated = service.mark_image_result("tok-a", success=True)
        assert updated is not None
        assert int(updated["quota"]) == 4

        refreshed = service.update_account(
            "tok-a",
            {
                "quota": 5,
                "limits_progress": [{"feature_name": "image_gen", "remaining": 5}],
            },
            quiet=True,
        )
        assert refreshed is not None
        assert int(refreshed["quota"]) == 4
        assert int(refreshed["limits_progress"][0]["remaining"]) == 4


def test_remote_refresh_can_increase_quota_after_grace_expires() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(tmp_dir)
        service.add_account_items(
            [
                {
                    "access_token": "tok-b",
                    "email": "b@example.com",
                    "status": "正常",
                    "quota": 2,
                    "image_quota_unknown": False,
                }
            ]
        )
        marked = service.mark_image_result("tok-b", success=True)
        assert marked is not None
        service.update_account("tok-b", {"quota_local_mark_grace_until": time.time() - 1}, quiet=True)

        refreshed = service.update_account("tok-b", {"quota": 2}, quiet=True)
        assert refreshed is not None
        assert int(refreshed["quota"]) == 2
