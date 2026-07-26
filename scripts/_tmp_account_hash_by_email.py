#!/usr/bin/env python3
import hashlib
import json
import sqlite3
import sys

email = sys.argv[1].strip().lower()
con = sqlite3.connect("/root/gptimage/data/accounts.db")
for row in con.execute("select access_token, data from accounts"):
    data = json.loads(row[1] or "{}")
    if str(data.get("email") or "").strip().lower() == email:
        token = str(row[0] or "")
        print(hashlib.sha256(token.encode()).hexdigest()[:12])
        break
else:
    raise SystemExit(1)
