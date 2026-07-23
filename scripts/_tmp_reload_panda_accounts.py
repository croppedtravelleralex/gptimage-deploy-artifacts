#!/usr/bin/env python3
import json
import urllib.request
from pathlib import Path

cfg = json.loads(Path("/root/gptimage/config.json").read_text(encoding="utf-8"))
auth = str(cfg.get("auth-key") or "").strip()
req = urllib.request.Request(
    "http://127.0.0.1:8012/api/accounts/reload-from-storage",
    data=b"{}",
    method="POST",
    headers={
        "Authorization": f"Bearer {auth}",
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(req, timeout=60) as resp:
    print(resp.status)
    print(resp.read().decode("utf-8", errors="replace")[:500])
