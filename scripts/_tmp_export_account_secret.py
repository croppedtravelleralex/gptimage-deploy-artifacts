#!/usr/bin/env python3
import json
import sqlite3
import sys

db = sys.argv[1]
email = sys.argv[2].strip().lower()
out = sys.argv[3]
con = sqlite3.connect(db)
for (raw,) in con.execute("select data from accounts"):
    d = json.loads(raw or "{}")
    if str(d.get("email") or "").strip().lower() != email:
        continue
    payload = {
        "email": email,
        "password": str(d.get("password") or ""),
        "proxy": str(d.get("proxy") or ""),
        "old_token": str(d.get("access_token") or ""),
    }
    Path = __import__("pathlib").Path
    Path(out).write_text(json.dumps(payload), encoding="utf-8")
    print(json.dumps({"ok": True, "email": email, "proxy_host": payload["proxy"].split("@")[-1][:40]}, ensure_ascii=False))
    break
else:
    print(json.dumps({"ok": False, "error": "not_found"}))
    raise SystemExit(1)
