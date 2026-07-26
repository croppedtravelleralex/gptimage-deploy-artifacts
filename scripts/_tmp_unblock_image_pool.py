#!/usr/bin/env python3
"""Clear preflight backoff + stale inflight on Panda, reload accounts."""
import base64, json, subprocess

py = """
import json, urllib.request
from services.account_service import account_service

account_service.reload_from_storage()
pref = len(account_service._image_preflight_failed_until)
account_service._image_preflight_failed_until.clear()
infl = dict(account_service._image_inflight)
cleared = []
for tok, n in list(infl.items()):
    if int(n or 0) > 0:
        for _ in range(int(n)):
            account_service.release_image_slot(tok)
        cleared.append(tok[:12])

auth=json.load(open("/app/config.json"))["auth-key"]
hdr={"Authorization":"Bearer "+auth}
urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8012/api/accounts/reload-from-storage", method="POST", headers=hdr), timeout=30).read()
h=json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8012/health?format=json", headers=hdr), timeout=30))
print(json.dumps({
  "cleared_preflight": pref,
  "cleared_inflight_tokens": len(cleared),
  "dispatchable": h["accounts"].get("dispatchable_candidate_count"),
  "image_schedulable": h["accounts"].get("image_schedulable"),
  "inflight": h["accounts"].get("image_inflight_count"),
}, indent=2))
"""
b64 = base64.b64encode(py.encode()).decode()
print(subprocess.check_output([
    "ssh", "-o", "ConnectTimeout=20", "panda",
    f"docker exec chatgpt2api-local uv run python3 -c \"import base64; exec(base64.b64decode('{b64}').decode())\"",
], text=True))
