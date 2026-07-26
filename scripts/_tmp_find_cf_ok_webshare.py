#!/usr/bin/env python3
"""Find N CF-ok Webshare proxies not used by any account (unique egress)."""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_POOL = Path("/app/data/runlogs/webshare_100_proxies.secret.txt")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--need", type=int, default=9)
    ap.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument(
        "--include-quarantined",
        action="store_true",
        help="probe quarantined pool entries too (default: skip quarantine)",
    )
    args = ap.parse_args()

    from scripts.panda_rebind_unique_proxies import _endpoint_key
    from services.account_service import account_service
    from services.proxy_cf_probe import probe_proxy_cf
    from services.proxy_quarantine import is_gpt_unavailable_proxy, proxy_endpoint_key
    from services.proxy_health import measure_proxy_egress_ip

    account_service.reload_from_storage()
    used_ep: set[str] = set()
    used_egress: set[str] = set()
    for acc in account_service.list_accounts():
        ep = _endpoint_key(acc.get("proxy"))
        if ep:
            used_ep.add(ep.lower())
        eg = str(acc.get("proxy_egress_ip") or "").strip()
        if eg:
            used_egress.add(eg)

    from services.proxy_cf_failover import load_proxy_pool as load_pool_raw

    pool = load_pool_raw(args.pool, include_quarantined=args.include_quarantined)
    candidates: list[str] = []
    for url in pool:
        ep = (proxy_endpoint_key(url) or _endpoint_key(url)).lower()
        if not ep or ep in used_ep:
            continue
        if not args.include_quarantined and is_gpt_unavailable_proxy(url):
            continue
        candidates.append(url)

    found: list[dict] = []
    lock_ep = set(used_ep)
    lock_egress = set(used_egress)

    def try_one(url: str) -> dict | None:
        cf = probe_proxy_cf(url, timeout=args.timeout)
        if not cf.get("ok"):
            return None
        eg = measure_proxy_egress_ip(url, timeout=20.0)
        if not eg.get("ok"):
            return None
        ip = str(eg.get("ip") or "").strip()
        if not ip:
            return None
        ep = proxy_endpoint_key(url) or _endpoint_key(url)
        return {
            "proxy_url": url,
            "proxy_endpoint": ep,
            "egress_ip": ip,
            "cf_latency_ms": cf.get("latency_ms"),
            "requirements_ok": cf.get("requirements_ok"),
            "cf_classification": cf.get("cf_classification"),
        }

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = [ex.submit(try_one, u) for u in candidates]
        for fut in as_completed(futures):
            if len(found) >= args.need:
                break
            row = fut.result()
            if not row:
                continue
            ep = str(row["proxy_endpoint"]).lower()
            ip = str(row["egress_ip"])
            if ep in lock_ep or ip in lock_egress:
                continue
            lock_ep.add(ep)
            lock_egress.add(ip)
            found.append(row)
            print(
                f"found {len(found)}/{args.need}: {row['proxy_endpoint']} egress={row['egress_ip']}",
                file=sys.stderr,
            )

    out = {
        "need": args.need,
        "found": len(found),
        "pool_size": len(pool),
        "used_endpoints": len(used_ep),
        "candidate_count": len(candidates),
        "proxies": found,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if len(found) >= args.need else 1


if __name__ == "__main__":
    raise SystemExit(main())
