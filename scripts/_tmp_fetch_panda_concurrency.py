#!/usr/bin/bin/python3
import json, subprocess
out = subprocess.check_output(
    ["ssh", "-o", "ConnectTimeout=20", "panda", "cat /root/gptimage/config.json"],
    text=True,
)
c = json.loads(out)
ip = c.get("image_pipeline") or {}
itq = c.get("image_task_queue") or {}
print(json.dumps({
    "sse_slots": ip.get("sse_slots"),
    "relaxed_per_user_running": ip.get("relaxed_per_user_running"),
    "image_global_concurrency": c.get("image_global_concurrency"),
    "image_account_concurrency": c.get("image_account_concurrency"),
    "image_binding_inflight_max": c.get("image_binding_inflight_max"),
    "proxy_binding_max_accounts": c.get("proxy_binding_max_accounts"),
    "submit_workers": itq.get("submit_workers"),
    "per_user_running_max": itq.get("per_user_running_max"),
    "per_user_running_base": itq.get("per_user_running_base"),
    "per_user_running_burst": itq.get("per_user_running_burst"),
    "newapi_image_sync_admission_max": c.get("newapi_image_sync_admission_max"),
}, indent=2))
