#!/usr/bin/env python3
"""Probe each account sticky proxy for CF and stamp proxy_cf_ok_* fields."""
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
from services.proxy_quarantine import clear_gpt_unavailable, mark_gpt_unavailable


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write proxy_cf_ok fields to DB")
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args()

    account_service.reload_from_storage()
    rows = []
    ok_count = 0
    for acc in account_service.list_accounts():
        token = str(acc.get("access_token") or "").strip()
        proxy = str(acc.get("proxy") or "").strip()
        email = str(acc.get("email") or "")
        row = {"email": email, "has_proxy": bool(proxy)}
        if not token or not proxy:
            row["skipped"] = True
            rows.append(row)
            continue
        probe = probe_proxy_cf(proxy, timeout=args.timeout)
        row["cf_ok"] = bool(probe.get("ok"))
        row["classification"] = probe.get("cf_classification")
        row["requirements_status"] = probe.get("requirements_status")
        if probe.get("ok"):
            ok_count += 1
            clear_gpt_unavailable(proxy)
            if args.apply:
                fields = cf_probe_account_fields(proxy, probe)
                account_service.update_account_identity(
                    token,
                    fields,
                    reason="cf_ok_stamp",
                    quiet=True,
                )
        else:
            mark_gpt_unavailable(
                proxy,
                reason=str(probe.get("cf_classification") or "cf403"),
                former_account=email,
            )
            row["error"] = str(probe.get("error") or probe.get("cf_classification") or "cf_probe_failed")[:200]
        rows.append(row)

    print(json.dumps({"ok": ok_count, "total": len(rows), "apply": args.apply, "rows": rows}, ensure_ascii=False, indent=2))
    return 0 if ok_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
