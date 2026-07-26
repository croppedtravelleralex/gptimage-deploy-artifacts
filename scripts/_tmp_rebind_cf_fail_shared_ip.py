#!/usr/bin/env python3
"""Rebind cf_fail accounts to known CF-ok Webshare nodes (up to 2 accounts per egress IP)."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CF_FAIL_EMAILS = [
    "qaflowakjewai6ps@proton.me",
    "ivetterock54353@outlook.com",
    "barnettregina91891@outlook.com",
    "andersmia76491@outlook.com",
    "enricoalfred9264@outlook.com",
    "blakekyle5108@outlook.com",
    "qaflowyi59i282fx@proton.me",
    "qaflowwrg2ptcd05@proton.me",
    "qaflowxho1z6hynk@proton.me",
]

# Stable spare nodes from S-cf-ok-spare-webshare-20260725.json
SPARE_ENDPOINTS = [
    "82.29.223.33:7847",
    "82.29.223.32:7846",
    "82.21.231.169:7483",
    "82.21.231.220:7534",
    "82.21.231.227:7541",
    "82.21.231.74:7388",
    "82.29.223.232:8046",
    "82.21.231.31:7345",
]

POOL = Path("/app/data/runlogs/webshare_100_proxies.secret.txt")
MAX_PER_EGRESS = 2


def _build_proxy_index(pool_path: Path) -> dict[str, str]:
    from scripts.panda_rebind_unique_proxies import _endpoint_key
    from services.proxy_cf_failover import load_proxy_pool

    out: dict[str, str] = {}
    for url in load_proxy_pool(pool_path, include_quarantined=True):
        ep = _endpoint_key(url).lower()
        if ep:
            out[ep] = url
    return out


def _probe_endpoints(
    by_ep: dict[str, str],
    spare_eps: list[str],
    *,
    timeout: float,
    retries: int = 3,
) -> dict[str, dict]:
    from services.proxy_cf_probe import probe_proxy_cf
    from services.proxy_health import measure_proxy_egress_ip, validate_http_proxy

    ok: dict[str, dict] = {}
    for ep in spare_eps:
        url = by_ep.get(ep.lower())
        if not url:
            continue
        for attempt in range(max(1, retries)):
            cf = probe_proxy_cf(url, timeout=timeout)
            if not cf.get("ok"):
                continue
            sticky = validate_http_proxy(url, timeout=20.0, require_sticky=True, sticky_gap_sec=1.5)
            if not sticky.get("ok"):
                continue
            sample = measure_proxy_egress_ip(url, timeout=20.0)
            egress_ip = str(sample.get("ip") or ep.split(":", 1)[0]).strip()
            egress_hash = str(sample.get("egress_hash") or "").strip()
            if not egress_ip or not egress_hash:
                continue
            ok[ep.lower()] = {
                "url": url,
                "endpoint": ep,
                "egress_ip": egress_ip,
                "egress_hash": egress_hash,
                "cf": cf,
            }
            break
    return ok


def _assign_endpoints(
    emails: list[str],
    spare_eps: list[str],
    egress_counts: Counter[str],
    *,
    max_per_egress: int,
    live_eps: set[str],
) -> list[tuple[str, str]]:
    """Return (email, endpoint) pairs using only CF-verified endpoints."""
    slots: Counter[str] = Counter()
    pairs: list[tuple[str, str]] = []
    for email in emails:
        assigned = False
        for ep in spare_eps:
            if ep.lower() not in live_eps:
                continue
            host = ep.split(":", 1)[0]
            used = egress_counts.get(host, 0) + slots.get(host, 0)
            if used >= max_per_egress:
                continue
            pairs.append((email, ep))
            slots[host] += 1
            assigned = True
            break
        if not assigned:
            raise RuntimeError(f"no spare slot for {email}")
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write rebinding to DB")
    ap.add_argument("--max-per-egress", type=int, default=MAX_PER_EGRESS)
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args()

    from scripts.panda_rebind_unique_proxies import _endpoint_key
    from services.account_identity import proxy_binding_hash
    from services.account_service import account_service
    from services.proxy_cf_eligibility import cf_probe_account_fields
    from services.proxy_quarantine import clear_gpt_unavailable

    account_service.reload_from_storage()
    by_ep = _build_proxy_index(POOL)
    missing = [ep for ep in SPARE_ENDPOINTS if ep.lower() not in by_ep]
    if missing:
        print(json.dumps({"error": "missing_endpoints_in_pool", "missing": missing}, indent=2))
        return 1

    verified = _probe_endpoints(by_ep, SPARE_ENDPOINTS, timeout=args.timeout)
    if len(verified) < (len(CF_FAIL_EMAILS) + args.max_per_egress - 1) // args.max_per_egress:
        print(
            json.dumps(
                {
                    "error": "insufficient_cf_ok_endpoints",
                    "verified": sorted(verified),
                    "need_slots": len(CF_FAIL_EMAILS),
                },
                indent=2,
            )
        )
        return 1

    targets = {e.strip().lower() for e in CF_FAIL_EMAILS}
    egress_counts: Counter[str] = Counter()
    accounts_by_email: dict[str, dict] = {}
    for acc in account_service.list_accounts():
        email = str(acc.get("email") or "").strip().lower()
        if email in targets:
            accounts_by_email[email] = acc
            continue
        host = str(acc.get("proxy_egress_ip") or "").strip()
        if host:
            egress_counts[host] += 1

    if len(accounts_by_email) != len(CF_FAIL_EMAILS):
        found = sorted(accounts_by_email)
        print(
            json.dumps(
                {
                    "error": "target_accounts_missing",
                    "expected": len(CF_FAIL_EMAILS),
                    "found": len(accounts_by_email),
                    "found_emails": found,
                },
                indent=2,
            )
        )
        return 1

    plan = _assign_endpoints(
        CF_FAIL_EMAILS,
        SPARE_ENDPOINTS,
        egress_counts,
        max_per_egress=max(1, args.max_per_egress),
        live_eps=set(verified),
    )

    results: list[dict] = []
    for email, ep in plan:
        acc = accounts_by_email[email.lower()]
        token = str(acc.get("access_token") or "").strip()
        picked = verified[ep.lower()]
        url = picked["url"]
        old_ep = _endpoint_key(acc.get("proxy"))
        old_egress = str(acc.get("proxy_egress_ip") or "")
        egress_ip = picked["egress_ip"]
        egress_hash = picked["egress_hash"]
        cf = picked["cf"]

        row = {
            "email": email,
            "old_endpoint": old_ep,
            "old_egress": old_egress,
            "new_endpoint": ep,
            "new_egress": egress_ip,
            "cf_ok": True,
            "apply": args.apply,
        }

        if not args.apply:
            row["ok"] = True
            row["dry_run"] = True
            results.append(row)
            continue

        clear_gpt_unavailable(url)
        ok = account_service.update_account_identity(
            token,
            {
                "proxy": url,
                "proxy_provider": "webshare",
                "proxy_scope": "account_sticky",
                "lifecycle_ip_mode": "sticky_one_ip_full",
                "proxy_binding_hash": proxy_binding_hash(url),
                "proxy_egress_hash": egress_hash,
                "proxy_egress_ip": egress_ip,
                "registration_proxy_hash": proxy_binding_hash(url),
                "registration_egress_hash": egress_hash,
                **cf_probe_account_fields(url, cf),
            },
            reason="cf_fail_shared_ip_rebind",
            quiet=False,
        )
        account_service.reset_observability_lights(token)
        with account_service._lock:
            resolved = account_service._resolve_access_token_locked(token)
            row_obj = account_service._accounts.get(resolved)
            if isinstance(row_obj, dict):
                account_service._persist_upsert_accounts([row_obj])
        row["ok"] = bool(ok)
        results.append(row)

    account_service.reload_from_storage()
    egress_after: Counter[str] = Counter()
    for acc in account_service.list_accounts():
        host = str(acc.get("proxy_egress_ip") or "").strip()
        if host:
            egress_after[host] += 1
    over = {ip: n for ip, n in egress_after.items() if n > args.max_per_egress}

    out = {
        "apply": args.apply,
        "max_per_egress": args.max_per_egress,
        "verified_endpoints": sorted(verified),
        "results": results,
        "egress_over_limit": over,
        "all_ok": all(r.get("ok") for r in results),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["all_ok"] and not over else 1


if __name__ == "__main__":
    raise SystemExit(main())
