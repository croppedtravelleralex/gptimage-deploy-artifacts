#!/usr/bin/env python3
import json
import sqlite3
import sys

db = sys.argv[1]
emails = [e.strip().lower() for e in sys.argv[2:]]
con = sqlite3.connect(db)
rows = []
for (raw,) in con.execute("select data from accounts"):
    d = json.loads(raw or "{}")
    email = str(d.get("email") or "").strip().lower()
    if email in emails:
        rows.append(
            {
                "email": email,
                "status": d.get("status"),
                "quota": d.get("quota"),
                "recv": d.get("panda_receive_state"),
                "err": d.get("last_refresh_error"),
                "proxy": (d.get("proxy") or "").split("@")[-1],
            }
        )
print(json.dumps(rows, ensure_ascii=False, indent=2))
