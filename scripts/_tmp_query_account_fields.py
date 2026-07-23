#!/usr/bin/env python3
import json
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "/root/gptimage/data/accounts.db"
targets = {e.strip().lower() for e in sys.argv[2:] if e.strip()}
con = sqlite3.connect(db)
for (raw,) in con.execute("select data from accounts"):
    d = json.loads(raw or "{}")
    email = str(d.get("email") or "").strip().lower()
    if targets and email not in targets:
        continue
    print(
        json.dumps(
            {
                "email": email,
                "status": d.get("status"),
                "has_openai_pw": bool(str(d.get("password") or "").strip()),
                "has_outlook_client_id": bool(str(d.get("outlook_client_id") or d.get("client_id") or "").strip()),
                "has_outlook_refresh": bool(str(d.get("outlook_refresh_token") or "").strip()),
                "proxy_host": (d.get("proxy") or "").split("@")[-1][:40] if d.get("proxy") else "",
                "proxy_egress_ip": d.get("proxy_egress_ip"),
            },
            ensure_ascii=False,
        )
    )
