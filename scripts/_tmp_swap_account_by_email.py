#!/usr/bin/env python3
"""Swap one account to a clean Webshare proxy by email (Panda helper)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.account_identity import proxy_binding_hash
from services.account_service import account_service
from services.proxy_cf_failover import pick_clean_proxy
from services.proxy_health import measure_proxy_egress_ip
from services.proxy_quarantine import mark_gpt_unavailable, proxy_endpoint_key


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("--pool", default="/app/data/runlogs/webshare_100_proxies.secret.txt")
    ap.add_argument("--force-host", default="", help="assign proxy whose URL host matches (bypass quarantine)")
    args = ap.parse_args()
    email = str(args.email or "").strip().lower()
    if not email:
        raise SystemExit("usage: swap_account_by_email.py <email>")

    pool_path = Path(str(args.pool or "").strip())
    force_host = str(args.force_host or "").strip().lower()

    def _pick_proxy() -> str:
        if force_host:
            from services.proxy_cf_failover import _parse_pool_line

            for line in pool_path.read_text(encoding="utf-8", errors="replace").splitlines():
                url = _parse_pool_line(line)
                if not url:
                    continue
                if proxy_endpoint_key(url).split(":", 1)[0] == force_host:
                    return url
            return ""
        return pick_clean_proxy(exclude={old_endpoint}, pool_path=pool_path)

    account_service.reload_from_storage()
    token = ""
    account: dict = {}
    for t in account_service.list_tokens():
        row = account_service.get_account(t) or {}
        if str(row.get("email") or "").strip().lower() == email:
            token = str(t)
            account = dict(row)
            break
    if not token:
        raise SystemExit(f"account not found: {email}")

    old_proxy = str(account.get("proxy") or "").strip()
    old_ip = str(account.get("proxy_egress_ip") or "")
    old_endpoint = proxy_endpoint_key(old_proxy)
    if old_proxy:
        mark_gpt_unavailable(old_proxy, reason="manual_bad_egress", former_account=email)

    new_proxy = _pick_proxy()
    if not new_proxy:
        print(json.dumps({"ok": False, "error": "no_clean_proxy"}, ensure_ascii=False))
        return 2

    sample = measure_proxy_egress_ip(new_proxy, timeout=20.0)
    if not sample.get("ok"):
        print(json.dumps({"ok": False, "error": sample.get("error"), "new_endpoint": proxy_endpoint_key(new_proxy)}, ensure_ascii=False))
        return 2

    egress_hash = str(sample.get("egress_hash") or "")
    egress_ip = str(sample.get("ip") or "")
    updates = {
        "proxy": new_proxy,
        "proxy_provider": "webshare",
        "proxy_scope": "account_sticky",
        "lifecycle_ip_mode": "sticky_one_ip_full",
        "proxy_binding_hash": proxy_binding_hash(new_proxy),
        "proxy_egress_hash": egress_hash,
        "proxy_egress_ip": egress_ip,
        "registration_proxy_hash": proxy_binding_hash(new_proxy),
        "registration_egress_hash": egress_hash,
        "image_fail_cooldown_until": 0,
        "image_fail_streak": 0,
    }
    account_service.update_account_identity(token, updates, reason="manual_bad_egress_swap", quiet=False)
    if hasattr(account_service, "reset_observability_lights"):
        account_service.reset_observability_lights(token)
    account_service.fetch_remote_info(token, "post_proxy_swap_quota_check")
    after = account_service.get_account(token) or {}
    out = {
        "ok": True,
        "email": email,
        "old_ip": old_ip,
        "old_endpoint": old_endpoint,
        "new_ip": egress_ip,
        "new_endpoint": proxy_endpoint_key(new_proxy),
        "new_binding": str(after.get("proxy_binding_hash") or "")[:12],
        "schedulable": account_service._is_image_account_schedulable(after),
        "quota": after.get("quota"),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
