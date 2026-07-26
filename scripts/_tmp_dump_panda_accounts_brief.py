#!/usr/bin/env python3
"""Dump Panda accounts with status/proxy for recovery planning."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    db = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/data/accounts.db")
    conn = sqlite3.connect(str(db))
    rows = []
    hosts: set[str] = set()
    for (raw,) in conn.execute("select data from accounts"):
        d = json.loads(raw or "{}")
        email = str(d.get("email") or "").strip().lower()
        if not email:
            continue
        proxy = str(d.get("proxy") or "")
        m = re.search(r"@([^:/]+)", proxy)
        host = m.group(1) if m else ""
        if host:
            hosts.add(host)
        rows.append(
            {
                "email": email,
                "status": d.get("status"),
                "quota": d.get("quota"),
                "panda_receive_state": d.get("panda_receive_state"),
                "last_refresh_error": str(d.get("last_refresh_error") or "")[:120],
                "outlook_recovery_state": d.get("outlook_recovery_state"),
                "proxy_host": host,
                "scheduling_enabled": d.get("scheduling_enabled"),
            }
        )
    dead = [
        r
        for r in rows
        if "invalid" in str(r.get("last_refresh_error") or "").lower()
        or str(r.get("status") or "") in {"异常", "禁用", "rejected"}
        or str(r.get("outlook_recovery_state") or "") == "terminal"
    ]
    outlook = [r for r in rows if email_ends_outlook(r["email"])]
    print(
        json.dumps(
            {
                "total": len(rows),
                "outlook_count": len(outlook),
                "dead_candidates": dead,
                "outlook": outlook,
                "hosts": sorted(hosts),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def email_ends_outlook(email: str) -> bool:
    return str(email).lower().endswith(("@outlook.com", "@hotmail.com", "@live.com"))


if __name__ == "__main__":
    raise SystemExit(main())
