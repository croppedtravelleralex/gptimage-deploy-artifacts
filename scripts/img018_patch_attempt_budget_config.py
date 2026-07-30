#!/usr/bin/env python3
"""IMG-018: apply 180s SSE+poll dual-needle budget on Panda config.json.

Run on Panda after git pull (config bind-mount hot update, no image rebuild required
for knob changes; restart container to pick up Python code from git pull).

  python3 /root/gptimage/scripts/img018_patch_attempt_budget_config.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/root/gptimage/config.json")

PATCH = {
    "newapi_image_attempt_budget_secs": 180,
    "newapi_image_sync_wait_timeout_secs": 180,
    "image_attempt_sse_phase_secs": 120,
    "image_attempt_poll_phase_secs": 60,
    "image_poll_after_slow_sse_ms": 45000,
    "image_poll_after_slow_sse_initial_wait_secs": 2.0,
    "newapi_image_sync_handoff_on_timeout_pending": False,
    "image_pipeline": {
        "ss_stage_wall_timeout_secs": 120,
    },
    "image_task_queue": {
        "generation_poll_timeout_secs": 60,
        "timeout_pending_max_attempts": 0,
    },
}


def apply_patch(data: dict) -> dict:
    for key, value in PATCH.items():
        if key in {"image_pipeline", "image_task_queue"}:
            bucket = data.setdefault(key, {})
            if not isinstance(bucket, dict):
                bucket = {}
                data[key] = bucket
            nested = PATCH[key]
            assert isinstance(nested, dict)
            bucket.update(nested)
            continue
        data[key] = value
    return data


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    if not path.is_file():
        print(f"config not found: {path}", file=sys.stderr)
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    apply_patch(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "config": str(path),
                "newapi_image_attempt_budget_secs": data.get("newapi_image_attempt_budget_secs"),
                "ss_stage_wall_timeout_secs": (data.get("image_pipeline") or {}).get("ss_stage_wall_timeout_secs"),
                "generation_poll_timeout_secs": (data.get("image_task_queue") or {}).get("generation_poll_timeout_secs"),
                "timeout_pending_max_attempts": (data.get("image_task_queue") or {}).get("timeout_pending_max_attempts"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
