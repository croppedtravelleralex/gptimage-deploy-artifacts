#!/usr/bin/env python3
import json, sqlite3, sys
email = sys.argv[1].strip().lower()
c = sqlite3.connect("/app/data/accounts.db")
for (raw,) in c.execute("select data from accounts"):
    d = json.loads(raw or "{}")
    if str(d.get("email") or "").lower() == email:
        print(json.dumps({
            "email": d.get("email"),
            "status": d.get("status"),
            "quota": d.get("quota"),
            "receive": d.get("panda_receive_state"),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
            "panda_imported_at": d.get("panda_imported_at"),
            "panda_verified_at": d.get("panda_verified_at"),
            "last_refresh_error": d.get("last_refresh_error"),
            "last_token_refresh_error": d.get("last_token_refresh_error"),
            "invalid_count": d.get("invalid_count"),
            "token_hash": str(d.get("access_token") or "")[:16],
            "proxy": (str(d.get("proxy") or "").split("@")[-1])[:40],
            "source_detail": d.get("source_detail"),
        }, ensure_ascii=False, indent=2))
        break
else:
    print(json.dumps({"error": "not_found", "email": email}))
