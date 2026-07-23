#!/usr/bin/env python3
import json
import sys
import urllib.request
from pathlib import Path

cfg = json.loads(Path("/root/gptimage/config.json").read_text(encoding="utf-8"))
auth = str(cfg.get("auth-key") or "").strip()
req = urllib.request.Request(
    "http://127.0.0.1:8012/api/accounts",
    headers={"Authorization": f"Bearer {auth}"},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())
items = data.get("items") or data.get("accounts") or []
targets = {e.strip().lower() for e in sys.argv[1:]}
for item in items:
    email = str(item.get("email") or "").strip().lower()
    if email in targets:
        print(
            json.dumps(
                {
                    "email": email,
                    "status": item.get("status"),
                    "quota": item.get("quota"),
                    "recv": item.get("panda_receive_state"),
                    "last_refresh_error": item.get("last_refresh_error"),
                    "panda_verify_last_error": item.get("panda_verify_last_error"),
                    "token_prefix": str(item.get("access_token") or "")[:16],
                },
                ensure_ascii=False,
            )
        )
