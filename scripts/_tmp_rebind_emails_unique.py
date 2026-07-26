#!/usr/bin/env python3
"""Rebind specific accounts to unique Webshare proxies (1 email : 1 endpoint)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.account_identity import normalize_account_identity, proxy_binding_hash
from services.account_service import account_service
from services.proxy_cf_failover import load_proxy_pool, proxy_endpoint_key
from services.proxy_health import measure_proxy_egress_ip

DEFAULT_POOL = Path("/app/data/runlogs/webshare_100_proxies.secret.txt")


def _used_endpoints() -> set[str]:
    used: set[str] = set()
    for acc in account_service.list_accounts():
        ep = proxy_endpoint_key(str(acc.get("proxy") or ""))
        if ep:
            used.add(ep)
    return used


def _find_token(email: str) -> tuple[str, dict]:
    target = email.strip().lower()
    for token in account_service.list_tokens():
        row = account_service.get_account(token) or {}
        if str(row.get("email") or "").strip().lower() == target:
            return str(token), dict(row)
    return "", {}


def _pick_unique(pool: list[str], blocked: set[str]) -> str:
    for url in pool:
        key = proxy_endpoint_key(url)
        if key and key not in blocked:
            return url
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("emails", nargs="+", help="account emails to rebind")
    ap.add_argument("--pool", default=str(DEFAULT_POOL))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    account_service.reload_from_storage()
    pool = load_proxy_pool(Path(args.pool), include_quarantined=True)
    if not pool:
        raise SystemExit(f"empty proxy pool: {args.pool}")

    blocked = _used_endpoints()
    plan: list[dict] = []
    for email in args.emails:
        token, acc = _find_token(email)
        if not token:
            plan.append({"email": email, "ok": False, "error": "not_found"})
            continue
        new_proxy = _pick_unique(pool, blocked)
        if not new_proxy:
            plan.append({"email": email, "ok": False, "error": "no_free_proxy"})
            continue
        blocked.add(proxy_endpoint_key(new_proxy))
        plan.append(
            {
                "email": email,
                "token": token,
                "old_endpoint": proxy_endpoint_key(str(acc.get("proxy") or "")),
                "old_binding": str(acc.get("proxy_binding_hash") or "")[:12],
                "new_proxy": new_proxy,
                "new_endpoint": proxy_endpoint_key(new_proxy),
            }
        )

    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "plan": [{k: v for k, v in p.items() if k != "token" and k != "new_proxy"} for p in plan]}, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0 if all(p.get("new_endpoint") for p in plan if not p.get("error")) else 1

    results = []
    for item in plan:
        if item.get("error") or not item.get("token"):
            results.append({"email": item.get("email"), "ok": False, "error": item.get("error") or "invalid"})
            continue
        sample = measure_proxy_egress_ip(str(item["new_proxy"]), timeout=20.0)
        if not sample.get("ok"):
            results.append({"email": item["email"], "ok": False, "error": sample.get("error")})
            continue
        egress_hash = str(sample.get("egress_hash") or "")
        egress_ip = str(sample.get("ip") or "")
        updates = {
            "proxy": item["new_proxy"],
            "proxy_provider": "webshare",
            "proxy_scope": "account_sticky",
            "lifecycle_ip_mode": "sticky_one_ip_full",
            "proxy_binding_hash": proxy_binding_hash(item["new_proxy"]),
            "proxy_egress_hash": egress_hash,
            "proxy_egress_ip": egress_ip,
            "registration_proxy_hash": proxy_binding_hash(item["new_proxy"]),
            "registration_egress_hash": egress_hash,
            "image_fail_cooldown_until": 0,
            "image_fail_streak": 0,
        }
        account_service.update_account_identity(
            item["token"],
            updates,
            reason="batch_unique_proxy_rebind",
            quiet=False,
        )
        after = normalize_account_identity(dict(account_service.get_account(item["token"]) or {}))
        results.append(
            {
                "email": item["email"],
                "ok": True,
                "old_endpoint": item.get("old_endpoint"),
                "new_endpoint": item.get("new_endpoint"),
                "new_ip": egress_ip,
                "new_binding": str(after.get("proxy_binding_hash") or "")[:12],
                "image_schedulable": account_service._is_image_account_schedulable(after),
            }
        )
        time.sleep(0.3)

    scheduling = sum(
        1
        for a in account_service.list_accounts()
        if account_service.is_manual_scheduling_enabled(a) and a.get("status") == "正常"
    )
    image_ok = sum(1 for a in account_service.list_accounts() if account_service._is_image_account_schedulable(a))
    summary = {
        "ok": sum(1 for r in results if r.get("ok")),
        "fail": sum(1 for r in results if not r.get("ok")),
        "scheduling_on": scheduling,
        "image_schedulable": image_ok,
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
