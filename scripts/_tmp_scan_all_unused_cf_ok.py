#!/usr/bin/env python3
"""Scan full Webshare pool for all CF-ok unused proxies (include quarantined)."""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, "/app")

from scripts.panda_rebind_unique_proxies import _endpoint_key
from services.account_service import account_service
from services.proxy_cf_failover import load_proxy_pool
from services.proxy_cf_probe import probe_proxy_cf
from services.proxy_health import measure_proxy_egress_ip

POOL = Path("/app/data/runlogs/webshare_100_proxies.secret.txt")
SKIP_EPS = {e.strip().lower() for e in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if e.strip()}

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

pool = load_proxy_pool(POOL, include_quarantined=True)
candidates = []
for url in pool:
    ep = _endpoint_key(url).lower()
    if not ep or ep in used_ep or ep in SKIP_EPS:
        continue
    candidates.append(url)

found = []
lock_ep = set(used_ep) | SKIP_EPS
lock_eg = set(used_egress)


def try_one(url: str) -> dict | None:
    cf = probe_proxy_cf(url, timeout=45.0)
    if not cf.get("ok"):
        return None
    eg = measure_proxy_egress_ip(url, timeout=25.0)
    if not eg.get("ok"):
        return None
    ip = str(eg.get("ip") or "").strip()
    if not ip:
        return None
    return {
        "proxy_endpoint": _endpoint_key(url),
        "egress_ip": ip,
        "requirements_ok": cf.get("requirements_ok"),
        "cf_classification": cf.get("cf_classification"),
        "home_ok": cf.get("home_ok"),
    }


with ThreadPoolExecutor(max_workers=15) as ex:
    futs = [ex.submit(try_one, u) for u in candidates]
    for fut in as_completed(futs):
        row = fut.result()
        if not row:
            continue
        ep = str(row["proxy_endpoint"]).lower()
        ip = str(row["egress_ip"])
        if ep in lock_ep or ip in lock_eg:
            continue
        lock_ep.add(ep)
        lock_eg.add(ip)
        found.append(row)

print(
    json.dumps(
        {
            "candidates": len(candidates),
            "cf_ok_unique": len(found),
            "proxies": sorted(found, key=lambda r: r["proxy_endpoint"]),
        },
        ensure_ascii=False,
        indent=2,
    )
)
