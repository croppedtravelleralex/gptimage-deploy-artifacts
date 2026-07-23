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
    # Write credential line for camoufox stable (outlook fields may be empty)
    outlook_pw = str(d.get("outlook_password") or d.get("mailbox_password") or "").strip()
    client_id = str(d.get("outlook_client_id") or d.get("client_id") or "").strip()
    refresh = str(d.get("outlook_refresh_token") or "").strip()
    openai_pw = str(d.get("password") or "").strip()
    proxy = str(d.get("proxy") or "").strip()
    print(
        json.dumps(
            {
                "email": email,
                "openai_password_len": len(openai_pw),
                "outlook_password_len": len(outlook_pw),
                "client_id_len": len(client_id),
                "refresh_len": len(refresh),
                "proxy": proxy.split("@")[-1] if "@" in proxy else proxy[:60],
            },
            ensure_ascii=False,
        )
    )
