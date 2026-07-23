#!/usr/bin/env python3
"""One-shot: fetch observation account fields from panda accounts.db (no stdout secrets)."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

EMAIL = "qaflow0ytb7bbp0z@proton.me"
DB = Path("/root/gptimage/data/accounts.db")
OUT = Path("/tmp/qaflow_secret.json")


def main() -> int:
    if not DB.exists():
        print(f"MISSING_DB {DB}", file=sys.stderr)
        return 2
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    tabs = [t[0] for t in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print("TABLES", tabs)
    for table in tabs:
        info = con.execute(f"PRAGMA table_info({table})").fetchall()
        names = [c[1] for c in info]
        email_cols = [n for n in names if "email" in n.lower()]
        if not email_cols:
            continue
        ec = email_cols[0]
        row = con.execute(
            f"SELECT * FROM {table} WHERE lower({ec})=?",
            (EMAIL.lower(),),
        ).fetchone()
        if not row:
            rows = con.execute(
                f"SELECT {ec} FROM {table} WHERE {ec} LIKE ?",
                ("%qaflow%",),
            ).fetchall()
            print("FUZZY", table, [r[0] for r in rows][:20])
            continue
        d = dict(row)
        summary = {}
        for k, v in d.items():
            if v is None:
                summary[k] = None
            elif isinstance(v, str) and any(
                x in k.lower() for x in ("password", "token", "secret", "passwd", "cookie")
            ):
                summary[k] = ("SET", len(v))
            elif isinstance(v, str) and len(v) > 100:
                summary[k] = v[:50] + "..."
            else:
                summary[k] = v
        print("HIT", table)
        print(json.dumps(summary, ensure_ascii=False, default=str)[:5000])
        keep = {
            k: v
            for k, v in d.items()
            if any(
                x in k.lower()
                for x in (
                    "email",
                    "password",
                    "pass",
                    "refresh",
                    "access",
                    "token",
                    "client",
                    "proxy",
                    "status",
                    "id",
                    "session",
                    "cookie",
                )
            )
        }
        OUT.write_text(json.dumps(keep, ensure_ascii=False, default=str), encoding="utf-8")
        print("WROTE", str(OUT), "keys", sorted(keep.keys()))
        return 0
    print("NOT_FOUND")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
