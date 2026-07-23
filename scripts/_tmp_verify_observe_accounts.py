#!/usr/bin/env python3
import json, sqlite3, sys
emails = {x.strip().lower() for x in sys.argv[1:]}
c = sqlite3.connect(sys.argv[0] if False else "/app/data/accounts.db")
for (raw,) in c.execute("select data from accounts"):
    d = json.loads(raw or "{}")
    email = str(d.get("email") or "").lower()
    if email in emails:
        print(json.dumps({
            "email": email,
            "status": d.get("status"),
            "quota": d.get("quota"),
            "receive": d.get("panda_receive_state"),
            "proxy": (str(d.get("proxy") or "").split("@")[-1])[:40],
        }, ensure_ascii=False))
