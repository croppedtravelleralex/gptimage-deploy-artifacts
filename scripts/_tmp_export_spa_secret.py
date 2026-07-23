#!/usr/bin/env python3
"""Export account secret JSON from Panda DB for SPA bench."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

email = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
out = Path(sys.argv[2] if len(sys.argv) > 2 else f"/tmp/spa_secret_{email.split('@')[0]}.json")
con = sqlite3.connect("/root/gptimage/data/accounts.db")
con.row_factory = sqlite3.Row
hit = None
token = None
for r in con.execute("select access_token, data from accounts"):
    d = json.loads(r["data"] or "{}")
    if str(d.get("email") or "").strip().lower() == email:
        hit = d
        token = r["access_token"]
        break
if not hit:
    raise SystemExit(f"not found {email}")
secret = {
    "email": hit.get("email"),
    "access_token": token,
    "proxy": hit.get("proxy"),
    "fp": hit.get("fp") if isinstance(hit.get("fp"), dict) else {},
    "proxy_egress_ip": hit.get("proxy_egress_ip"),
    "status": hit.get("status"),
}
out.write_text(json.dumps(secret, ensure_ascii=False, indent=2), encoding="utf-8")
print(str(out))
