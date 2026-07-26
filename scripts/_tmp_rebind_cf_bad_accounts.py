#!/usr/bin/env python3
"""Rebind CF-bad accounts to clean unique egress (measured + CF probed)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EMAILS = [
    "felicitypamela2673@outlook.com",
    "ivorbrown70573@outlook.com",
    "qaflow0ytb7bbp0z@proton.me",
    "qaflowfbdb3ovksr@proton.me",
    "qaflowgq5wyuxhe9@proton.me",
    "blakekyle5108@outlook.com",
]
POOL = Path("/app/data/runlogs/webshare_100_proxies.secret.txt")


def main() -> int:
    from scripts.panda_rebind_unique_proxies import _endpoint_key, _pick_unique_proxy, load_proxy_pool
    from services.account_identity import proxy_binding_hash
    from services.account_service import account_service
    from services.proxy_cf_probe import probe_proxy_cf
    from services.proxy_health import validate_http_proxy

    account_service.reload_from_storage()
    pool = load_proxy_pool(POOL)
    all_acc = account_service.list_accounts()
    used_endpoints: set[str] = set()
    used_egress: set[str] = set()
    for acc in all_acc:
        proxy = str(acc.get("proxy") or "").strip()
        ep = _endpoint_key(proxy)
        if ep:
            used_endpoints.add(ep)
        egress = str(acc.get("proxy_egress_ip") or "").strip()
        if egress:
            used_egress.add(egress)

    targets = {e.strip().lower() for e in EMAILS}
    results: list[dict] = []
    for acc in all_acc:
        email = str(acc.get("email") or "").strip().lower()
        if email not in targets:
            continue
        token = str(acc.get("access_token") or "").strip()
        old_egress = str(acc.get("proxy_egress_ip") or "")
        old_ep = _endpoint_key(acc.get("proxy"))
        used_endpoints.discard(old_ep)
        used_egress.discard(old_egress)

        picked = None
        for _ in range(40):
            candidate = _pick_unique_proxy(
                pool,
                used_endpoints=used_endpoints,
                used_egress=used_egress,
                timeout=20.0,
            )
            if not candidate:
                break
            url, egress_ip, egress_hash = candidate
            ep = _endpoint_key(url)
            used_endpoints.add(ep)
            cf = probe_proxy_cf(url, timeout=45.0)
            if cf.get("ok"):
                check = validate_http_proxy(url, timeout=20.0, require_sticky=True, sticky_gap_sec=1.5)
                if check.get("ok"):
                    picked = (url, egress_ip, egress_hash)
                    break

        if not picked:
            if old_ep:
                used_endpoints.add(old_ep)
            if old_egress:
                used_egress.add(old_egress)
            results.append({"email": email, "ok": False, "error": "no_cf_ok_unique_proxy", "old_egress": old_egress})
            continue

        new_proxy, egress_ip, egress_hash = picked
        ok = account_service.update_account_identity(
            token,
            {
                "proxy": new_proxy,
                "proxy_provider": "webshare",
                "proxy_scope": "account_sticky",
                "lifecycle_ip_mode": "sticky_one_ip_full",
                "proxy_binding_hash": proxy_binding_hash(new_proxy),
                "proxy_egress_hash": egress_hash,
                "proxy_egress_ip": egress_ip,
                "registration_proxy_hash": proxy_binding_hash(new_proxy),
                "registration_egress_hash": egress_hash,
            },
            reason="cf_bad_rebind",
            quiet=False,
        )
        account_service.reset_observability_lights(token)
        results.append(
            {
                "email": email,
                "ok": bool(ok),
                "old_egress": old_egress,
                "new_egress": egress_ip,
                "cf_ok": True,
            }
        )

    account_service.reload_from_storage()
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
