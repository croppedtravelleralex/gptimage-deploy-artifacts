#!/usr/bin/env python3
"""Patch account created_at from backup for relogin recovery metadata fix."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/app/data/accounts.db")
    ap.add_argument("--email", required=True)
    ap.add_argument("--created-at", required=True)
    args = ap.parse_args()

    email = args.email.strip().lower()
    db_path = Path(args.db)
    con = sqlite3.connect(db_path)
    rows = list(con.execute("select rowid, data from accounts"))
    updated = False
    for rowid, raw in rows:
        data = json.loads(raw or "{}")
        if str(data.get("email") or "").lower() != email:
            continue
        data["created_at"] = args.created_at
        con.execute("update accounts set data=? where rowid=?", (json.dumps(data, ensure_ascii=False), rowid))
        updated = True
        print(json.dumps({"ok": True, "email": email, "created_at": data["created_at"]}, ensure_ascii=False))
        break
    if not updated:
        print(json.dumps({"ok": False, "error": "not_found"}))
        return 1
    con.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
