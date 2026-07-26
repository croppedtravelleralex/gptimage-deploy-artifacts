#!/usr/bin/env python3
"""Extract account timeline fields from Panda accounts.db backups."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def load_email(db: Path, email: str) -> dict | None:
    conn = sqlite3.connect(str(db))
    email = email.strip().lower()
    for token, raw in conn.execute("select access_token, data from accounts"):
        d = json.loads(raw or "{}")
        if str(d.get("email") or "").strip().lower() == email:
            return {
                "db": str(db),
                "token_prefix": str(token)[:16],
                "email": d.get("email"),
                "status": d.get("status"),
                "quota": d.get("quota"),
                "panda_receive_state": d.get("panda_receive_state"),
                "panda_imported_at": d.get("panda_imported_at"),
                "created_at": d.get("created_at"),
                "updated_at": d.get("updated_at"),
                "last_refresh_error": str(d.get("last_refresh_error") or "")[:200],
                "last_token_refresh_error": str(d.get("last_token_refresh_error") or "")[:200],
                "invalid_count": d.get("invalid_count"),
                "proxy": (str(d.get("proxy") or "").split("@")[-1])[:40],
                "source_detail": d.get("source_detail"),
                "register_egress_ip": d.get("register_egress_ip"),
                "proxy_egress_ip": d.get("proxy_egress_ip"),
            }
    return None


def main() -> int:
    email = sys.argv[1]
    for p in sys.argv[2:]:
        row = load_email(Path(p), email)
        print(json.dumps(row or {"db": p, "missing": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
