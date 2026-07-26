#!/usr/bin/env python3
"""Clear quarantine for account-bound proxies and refresh CF stamps."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.account_service import account_service
from services.proxy_cf_eligibility import cf_probe_account_fields
from services.proxy_cf_probe import probe_proxy_cf
from services.proxy_quarantine import clear_gpt_unavailable, proxy_endpoint_key


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args()

    account_service.reload_from_storage()
    cleared: list[str] = []
    rows: list[dict] = []
    seen: set[str] = set()

    for acc in account_service.list_accounts():
        token = str(acc.get("access_token") or "").strip()
        proxy = str(acc.get("proxy") or "").strip()
        email = str(acc.get("email") or "")
        if not token or not proxy:
            continue
        ep = proxy_endpoint_key(proxy)
        if ep and ep not in seen:
            seen.add(ep)
            if args.apply and clear_gpt_unavailable(proxy):
                cleared.append(ep)

        probe = probe_proxy_cf(proxy, timeout=args.timeout)
        row = {
            "email": email,
            "endpoint": ep,
            "cf_ok": bool(probe.get("ok")),
            "classification": probe.get("cf_classification"),
        }
        if args.apply and probe.get("ok"):
            fields = cf_probe_account_fields(proxy, probe)
            account_service.update_account_identity(
                token,
                fields,
                reason="cf_quarantine_recovery",
                quiet=True,
            )
            with account_service._lock:
                resolved = account_service._resolve_access_token_locked(token)
                item = account_service._accounts.get(resolved)
                if isinstance(item, dict):
                    item["image_schedulable"] = account_service._is_image_account_schedulable(item)
                    account_service._persist_upsert_accounts([item])
        rows.append(row)

    account_service.reload_from_storage()
    sched = sum(1 for a in account_service.list_accounts() if a.get("image_schedulable"))
    out = {
        "apply": args.apply,
        "cleared_endpoints": sorted(cleared),
        "cf_ok": sum(1 for r in rows if r.get("cf_ok")),
        "total": len(rows),
        "image_schedulable": sched,
        "rows": rows,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if sched > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
