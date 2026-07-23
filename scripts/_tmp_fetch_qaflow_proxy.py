#!/usr/bin/env python3
import json, sqlite3, re
DB="/root/gptimage/data/accounts.db"
EMAIL="qaflow0ytb7bbp0z@proton.me"
con=sqlite3.connect(DB)
print("tables", con.execute("select name from sqlite_master where type='table'").fetchall())
print("info", con.execute("pragma table_info(accounts)").fetchall())
# find row
cols=[r[1] for r in con.execute("pragma table_info(accounts)").fetchall()]
print("cols", cols)
rows=con.execute("select * from accounts").fetchall()
print("nrows", len(rows))
found=None
for row in rows:
    d=None
    for cell in row:
        if isinstance(cell,str) and cell.strip().startswith("{"):
            try:
                d=json.loads(cell)
            except Exception:
                pass
    blob="|".join(str(x) for x in row)
    if EMAIL in blob or (isinstance(d,dict) and d.get("email")==EMAIL):
        found=row
        data=d or {}
        break
if not found:
    print(json.dumps({"ok":False,"error":"not_found"}))
else:
    proxy=str(data.get("proxy") or "")
    out={
      "ok":True,
      "row0": found[0],
      "email": data.get("email"),
      "proxy_provider": data.get("proxy_provider"),
      "lifecycle_ip_mode": data.get("lifecycle_ip_mode"),
      "has_at": bool(data.get("access_token")),
      "has_rt": bool(data.get("refresh_token")),
      "has_sess": bool(data.get("chatgpt_session_token") or data.get("session_token")),
      "access_token": data.get("access_token") or "",
      "refresh_token": data.get("refresh_token") or "",
      "chatgpt_session_token": data.get("chatgpt_session_token") or data.get("session_token") or "",
      "fp": data.get("fp") if isinstance(data.get("fp"), dict) else {},
      "proxy": proxy,
      "interesting": {k:data.get(k) for k in sorted(data) if any(x in k.lower() for x in ("proxy","quota","remain","image","egress","bind","webshare","lifecycle","ip_")) and "token" not in k.lower() and "password" not in k.lower()},
    }
    print(json.dumps(out, ensure_ascii=False))
