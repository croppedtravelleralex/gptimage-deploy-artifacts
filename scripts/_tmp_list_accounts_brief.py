#!/usr/bin/env python3
import json
import sqlite3

con = sqlite3.connect("/root/gptimage/data/accounts.db")
rows = []
for r in con.execute("select data from accounts"):
    d = json.loads(r[0] or "{}")
    email = d.get("email")
    st = d.get("status")
    err = str(
        d.get("last_refresh_error")
        or d.get("last_token_refresh_error")
        or d.get("panda_verify_last_error")
        or ""
    )
    px = str(d.get("proxy_egress_ip") or "")
    rows.append(
        (
            st,
            email,
            px,
            err[:140],
            bool(d.get("password")),
            bool(d.get("refresh_token")),
            d.get("quota"),
            d.get("success"),
            d.get("fail"),
            d.get("proxy_provider"),
        )
    )
print("ALL_ACCOUNTS")
for row in sorted(rows, key=lambda x: (str(x[0]), str(x[1]))):
    print("|".join(str(x) for x in row))
