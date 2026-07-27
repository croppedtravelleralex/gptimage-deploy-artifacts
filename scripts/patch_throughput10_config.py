#!/usr/bin/env python3
"""Patch runtime config.json for THROUGHPUT-10 defaults (idempotent)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.config import CONFIG_FILE

PATCH: dict[str, object] = {
    "image_global_concurrency": 10,
    "image_account_concurrency": 2,
    "image_binding_inflight_max": 1,
    "image_quota_refresh_interval_sec": 60,
    "image_task_queue": {
        "submit_workers": 10,
        "submit_workers_max": 12,
        "per_user_running_base": 10,
        "per_user_running_max": 10,
        "per_user_running_burst": 12,
        "global_queue_max": 200,
        "per_user_queue_max": 36,
    },
    "image_pipeline": {
        "enabled": True,
        "prompt_slots": 10,
        "sse_slots": 10,
        "download_concurrency": 8,
        "global_queue_max": 200,
        "relaxed_per_user_running": True,
    },
    "account_warmup": {
        "max_hot": 17,
    },
    "webshare_cf_scan": {
        "probe_on_assign": True,
        "block_unscanned_for_schedule": True,
        "require_cf_ok_for_image": True,
    },
    "newapi_image_sync_admission_max": 10,
    "newapi_image_sync_admission_max_eta_secs": 540,
}


def main() -> None:
    path = Path(CONFIG_FILE)
    data: dict[str, object] = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in PATCH.items():
        if isinstance(value, dict):
            section = data.get(key)
            if not isinstance(section, dict):
                section = {}
            section.update(value)  # type: ignore[arg-type]
            data[key] = section
        else:
            data[key] = value
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path), "patched_keys": list(PATCH)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
