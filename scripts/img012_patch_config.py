#!/usr/bin/env python3
import json
from pathlib import Path

path = Path("/root/gptimage/config.json")
data = json.loads(path.read_text(encoding="utf-8"))
queue = data.setdefault("image_task_queue", {})
queue["per_user_running_max"] = 6
queue["per_user_running_base"] = 6
queue["per_user_running_burst"] = 8
queue["burst_enabled"] = False
data["image_return_window_size"] = 3
data.setdefault("newapi_image_sync_wait_timeout_secs", 540)
data.setdefault("newapi_image_sync_poll_interval_secs", 1.5)
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({
    "per_user_running_max": queue.get("per_user_running_max"),
    "image_return_window_size": data.get("image_return_window_size"),
    "burst_enabled": queue.get("burst_enabled"),
}, ensure_ascii=False))
