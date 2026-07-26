#!/usr/bin/env python3
"""List schedulable accounts with duplicate proxy_egress_ip (same logic as pick_concurrent_accounts)."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path


def load_pool(db_path: Path, *, min_quota: int = 1, limit: int = 90) -> list[dict]:
    """Mirror sentinel_ticket_validation_suite.load_accounts_db (no image_schedulable gate)."""
    rows: list[dict] = []
    con = sqlite3.connect(str(db_path))
    for access_token, raw in con.execute("select access_token, data from accounts"):
        data = json.loads(raw or "{}")
        q = int(data.get("quota") or 0)
        if q < min_quota and not data.get("unlimited"):
            continue
        proxy = str(data.get("proxy") or "").strip()
        if not proxy or not str(access_token or "").strip():
            continue
        egress = str(data.get("proxy_egress_ip") or "").strip()
        if not egress:
            hostpart = proxy.rsplit("@", 1)[-1]
            egress = hostpart.split(":")[0] if ":" in hostpart else hostpart
        rows.append(
            {
                "email": data.get("email"),
                "egress": egress,
                "quota": q,
                "proxy": proxy,
                "proxy_host": proxy.rsplit("@", 1)[-1],
            }
        )
    con.close()
    rows.sort(key=lambda x: int(x.get("quota") or 0), reverse=True)
    return rows[:limit]


def pick_unique(rows: list[dict], workers: int) -> list[dict]:
    ordered = sorted(rows, key=lambda x: int(x.get("quota") or 0), reverse=True)
    picked: list[dict] = []
    seen_proxy: set[str] = set()
    seen_egress: set[str] = set()
    skipped: list[dict] = []
    for acc in ordered:
        if len(picked) >= workers:
            break
        proxy = str(acc.get("proxy") or "").strip()
        egress = str(acc.get("egress") or "").strip()
        if proxy in seen_proxy or egress in seen_egress:
            skipped.append({**acc, "skip_reason": "duplicate_proxy" if proxy in seen_proxy else "duplicate_egress"})
            continue
        seen_proxy.add(proxy)
        seen_egress.add(egress)
        picked.append(acc)
    return picked, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts-db", default="/app/data/accounts.db")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()
    rows = load_pool(Path(args.accounts_db), min_quota=1, limit=90)
    by_egress: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_egress[r["egress"]].append(r)
    dups = {k: v for k, v in by_egress.items() if len(v) > 1}
    picked, skipped = pick_unique(rows, int(args.workers))
    out = {
        "schedulable_with_proxy": len(rows),
        "unique_egress_count": len(by_egress),
        "max_concurrent_unique_egress": len(picked),
        "workers_requested": int(args.workers),
        "duplicate_egress_groups": {
            eg: [{"email": a["email"], "quota": a["quota"], "proxy_host": a["proxy_host"]} for a in accs]
            for eg, accs in sorted(dups.items(), key=lambda x: -len(x[1]))
        },
        "picked_for_conc10": [{"email": a["email"], "egress": a["egress"], "quota": a["quota"]} for a in picked],
        "skipped_due_to_unique": skipped,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
