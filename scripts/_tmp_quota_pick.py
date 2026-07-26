#!/usr/bin/env python3
import json
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "/app/data/accounts.db"
con = sqlite3.connect(db)
rows = []
for _, raw in con.execute("select access_token, data from accounts"):
    d = json.loads(raw or "{}")
    q = int(d.get("quota") or 0)
    if q < 1 and not d.get("unlimited"):
        continue
    if not d.get("proxy"):
        continue
    err = " ".join(
        str(d.get(k) or "")
        for k in ("last_refresh_error", "last_token_refresh_error", "panda_verify_last_error")
    ).lower()
    cf = "cf" if "cf403" in err or "cloudflare" in err else "ok"
    rows.append((q, d.get("email"), cf, d.get("status"), d.get("proxy_egress_ip")))
rows.sort(key=lambda x: x[0], reverse=True)
for r in rows[:20]:
    print("|".join(str(x) for x in r))
print("TOTAL", len(rows))
