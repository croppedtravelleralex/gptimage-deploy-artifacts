#!/usr/bin/env python3
import json
import subprocess

def remote(cmd: str) -> str:
    p = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "panda", cmd], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(p.stderr or p.stdout)
    return p.stdout.strip()

health = json.loads(remote("curl -sS http://127.0.0.1:8012/health?format=json"))
cfg = json.loads(remote("python3 -c 'import json;print(json.dumps(json.load(open(\"/root/gptimage/config.json\"))))'"))
accounts = health.get("accounts") or {}
queue = cfg.get("image_task_queue") or {}
print(json.dumps({
    "healthy": health.get("healthy"),
    "dispatchable": accounts.get("dispatchable_candidate_count"),
    "inflight": accounts.get("image_inflight_count"),
    "global_limit_reached": accounts.get("image_global_limit_reached"),
    "per_user_running_max": queue.get("per_user_running_max"),
    "burst_enabled": queue.get("burst_enabled"),
    "image_return_window_size": cfg.get("image_return_window_size"),
    "has_auth_key": bool(str(cfg.get("auth-key") or "").strip()),
}, ensure_ascii=False, indent=2))
