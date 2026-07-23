#!/usr/bin/env python3
import json
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "/root/gptimage/data/accounts.db"
con = sqlite3.connect(db)
rows = []
for (raw,) in con.execute("select data from accounts"):
    d = json.loads(raw or "{}")
    err = " ".join(
        str(d.get(k) or "")
        for k in (
            "last_refresh_error",
            "last_token_refresh_error",
            "last_quota_refresh_error",
            "panda_verify_last_error",
        )
    ).lower()
    status = str(d.get("status") or "")
    recv = str(d.get("panda_receive_state") or "").lower()
    if "token invalidated" in err or status == "异常" or recv in {"rejected", "tainted"}:
        rows.append(
            {
                "email": d.get("email"),
                "status": status,
                "quota": d.get("quota"),
                "recv": d.get("panda_receive_state"),
                "err": (d.get("last_refresh_error") or d.get("panda_verify_last_error") or "")[:160],
                "proxy": (d.get("proxy") or "")[:100],
                "has_pw": bool(str(d.get("password") or "").strip()),
                "updated": d.get("updated_at") or d.get("last_refresh_error_at"),
            }
        )
print(json.dumps({"count": len(rows), "accounts": rows}, ensure_ascii=False, indent=2))
