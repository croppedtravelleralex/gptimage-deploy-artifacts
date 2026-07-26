#!/usr/bin/env python3
"""Delete account by email and quarantine its sticky proxy (CF403)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/app")
    ap.add_argument("--email", required=True)
    ap.add_argument("--reason", default="cf403")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    sys.path.insert(0, str(root))
    email = str(args.email).strip().lower()

    from services.account_service import account_service
    from services.proxy_quarantine import mark_gpt_unavailable

    account_service.reload_from_storage()
    token = ""
    proxy = ""
    for item in account_service.list_accounts():
        if str(item.get("email") or "").strip().lower() == email:
            token = str(item.get("access_token") or "").strip()
            proxy = str(item.get("proxy") or "").strip()
            break
    if not token:
        print(json.dumps({"ok": False, "error": "account_not_found", "email": email}, ensure_ascii=False))
        return 1

    removed = account_service.delete_accounts([token], include_items=False)
    quarantine = None
    if proxy:
        quarantine = mark_gpt_unavailable(
            proxy,
            reason=args.reason,
            former_account=email,
            path=root / "data" / "gpt_unavailable_proxies.json",
        )

    print(
        json.dumps(
            {
                "ok": True,
                "email": email,
                "removed": removed,
                "proxy_quarantined": proxy.split("@")[-1][:40] if proxy else "",
                "reason": args.reason,
                "endpoints_count": len((quarantine or {}).get("endpoints") or []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
