#!/usr/bin/env python3
"""Rebind accounts onto Webshare residential-20 pool (max N accounts per egress IP).

Default: only rebind accounts whose proxy is outside the pool or proxy_cf_ok=false.
Use --all to migrate every active account onto the residential pool.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.account_identity import normalize_account_identity, proxy_binding_hash
from services.account_service import account_service
from services.proxy_cf_failover import load_proxy_pool, proxy_endpoint_key
from services.proxy_cf_eligibility import cf_probe_account_fields
from services.proxy_cf_probe import probe_proxy_cf
from services.proxy_health import measure_proxy_egress_ip, validate_http_proxy
from services.proxy_quarantine import clear_gpt_unavailable

DEFAULT_POOL = Path("/app/data/runlogs/webshare_20_proxies.secret.txt")


def _egress_ip(account: dict) -> str:
    return str(account.get("proxy_egress_ip") or "").strip()


def _probe_nodes(pool: list[str], *, timeout: float) -> list[dict]:
    nodes: list[dict] = []
    for url in pool:
        ep = proxy_endpoint_key(url)
        if not ep:
            continue
        clear_gpt_unavailable(url)
        cf = probe_proxy_cf(url, timeout=timeout)
        sample = measure_proxy_egress_ip(url, timeout=min(timeout, 25.0))
        egress_ip = str(sample.get("ip") or ep.split(":", 1)[0]).strip()
        nodes.append(
            {
                "url": url,
                "endpoint": ep.lower(),
                "egress_ip": egress_ip,
                "egress_hash": str(sample.get("egress_hash") or "").strip(),
                "cf_ok": bool(cf.get("ok")),
                "cf_classification": str(cf.get("cf_classification") or ""),
                "binding_hash": proxy_binding_hash(url),
            }
        )
    return nodes


def _pick_node(
    nodes: list[dict],
    load: Counter[str],
    *,
    max_per_egress: int,
    used_endpoints: set[str],
) -> dict | None:
    candidates = [
        n
        for n in nodes
        if n.get("cf_ok")
        and int(load.get(str(n.get("egress_ip") or ""), 0)) < max_per_egress
        and str(n.get("endpoint") or "") not in used_endpoints
    ]
    if not candidates:
        # allow endpoint reuse when egress still has capacity (same IP different port unlikely in this pool)
        candidates = [
            n
            for n in nodes
            if n.get("cf_ok") and int(load.get(str(n.get("egress_ip") or ""), 0)) < max_per_egress
        ]
    if not candidates:
        return None
    candidates.sort(key=lambda n: (int(load.get(str(n.get("egress_ip") or ""), 0)), str(n.get("endpoint") or "")))
    return candidates[0]


def plan_assignments(
    accounts: list[dict],
    nodes: list[dict],
    pool_endpoints: set[str],
    *,
    max_per_egress: int,
    rebind_all: bool,
) -> tuple[list[dict], dict]:
    ok_nodes = [n for n in nodes if n.get("cf_ok")]
    if not ok_nodes:
        raise RuntimeError("no_cf_ok_nodes_in_pool")

    load: Counter[str] = Counter()
    used_endpoints: set[str] = set()
    keep: list[dict] = []
    rebind: list[dict] = []

    for acc in accounts:
        status = str(acc.get("status") or "")
        if status in {"禁用", "异常"}:
            continue
        ep = proxy_endpoint_key(str(acc.get("proxy") or "")).lower()
        egress = _egress_ip(acc)
        cf_bad = acc.get("proxy_cf_ok") is False
        outside = bool(ep) and ep not in pool_endpoints
        if rebind_all or outside or cf_bad or not ep:
            rebind.append(acc)
            continue
        keep.append(acc)
        if egress:
            load[egress] += 1
        if ep:
            used_endpoints.add(ep)

    assignments: list[dict] = []
    for acc in sorted(rebind, key=lambda a: (0 if a.get("proxy_cf_ok") is False else 1, str(a.get("email") or ""))):
        node = _pick_node(ok_nodes, load, max_per_egress=max_per_egress, used_endpoints=used_endpoints)
        if node is None:
            raise RuntimeError(f"no_slot_for:{acc.get('email')}")
        egress = str(node.get("egress_ip") or "")
        load[egress] += 1
        used_endpoints.add(str(node.get("endpoint") or ""))
        assignments.append(
            {
                "email": str(acc.get("email") or ""),
                "access_token": str(acc.get("access_token") or ""),
                "old_endpoint": proxy_endpoint_key(str(acc.get("proxy") or "")),
                "old_egress": _egress_ip(acc),
                "new_endpoint": node.get("endpoint"),
                "new_egress": egress,
                "new_proxy": node.get("url"),
                "clear_isolation": str(acc.get("panda_receive_state") or "").strip().lower() == "identity_isolated",
            }
        )

    meta = {
        "keep": len(keep),
        "rebind": len(assignments),
        "cf_ok_nodes": len(ok_nodes),
        "pool_nodes": len(nodes),
        "max_per_egress": max_per_egress,
        "load_after_plan": dict(load),
    }
    return assignments, meta


def apply_assignment(item: dict, *, timeout: float, validate: bool) -> dict:
    token = str(item.get("access_token") or "")
    email = str(item.get("email") or "")
    proxy = str(item.get("new_proxy") or "")
    row: dict = {"email": email, "ok": False}
    if not token or not proxy:
        row["error"] = "missing_token_or_proxy"
        return row

    if validate:
        check = validate_http_proxy(proxy, timeout=timeout, require_sticky=True, sticky_gap_sec=1.5)
        if not check.get("ok"):
            row["error"] = str(check.get("error") or "sticky_validate_failed")[:200]
            return row

    clear_gpt_unavailable(proxy)
    cf = probe_proxy_cf(proxy, timeout=timeout)
    if not cf.get("ok"):
        row["error"] = f"cf_probe_failed:{cf.get('cf_classification') or 'unknown'}"
        return row

    sample = measure_proxy_egress_ip(proxy, timeout=min(timeout, 25.0))
    egress_ip = str(sample.get("ip") or item.get("new_egress") or "").strip()
    egress_hash = str(sample.get("egress_hash") or "").strip()
    if not egress_hash:
        row["error"] = "missing_egress_hash"
        return row

    updates = {
        "proxy": proxy,
        "proxy_provider": "webshare",
        "proxy_scope": "account_sticky",
        "lifecycle_ip_mode": "sticky_one_ip_full",
        "proxy_binding_hash": proxy_binding_hash(proxy),
        "proxy_egress_hash": egress_hash,
        "proxy_egress_ip": egress_ip,
        "registration_proxy_hash": proxy_binding_hash(proxy),
        "registration_egress_hash": egress_hash,
        "image_fail_cooldown_until": 0,
        "image_fail_streak": 0,
        **cf_probe_account_fields(proxy, cf),
    }
    if item.get("clear_isolation"):
        updates["panda_receive_state"] = "verified_ready"

    account_service.update_account_identity(token, updates, reason="webshare20_residential_rebind", quiet=False)
    after = normalize_account_identity(dict(account_service.get_account(token) or {}))
    row.update(
        {
            "ok": True,
            "new_endpoint": proxy_endpoint_key(proxy),
            "new_egress": egress_ip,
            "proxy_cf_ok": after.get("proxy_cf_ok"),
            "image_schedulable": account_service._is_image_account_schedulable(after, allow_live_cf_probe=False),
        }
    )
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=str(DEFAULT_POOL))
    ap.add_argument("--max-per-egress", type=int, default=5)
    ap.add_argument("--all", action="store_true", help="rebind every active account onto residential pool")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--validate-proxy", action="store_true")
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--probe-only", action="store_true", help="only probe pool nodes")
    args = ap.parse_args()

    pool_path = Path(args.pool)
    if not pool_path.is_file():
        print(json.dumps({"error": "pool_missing", "path": str(pool_path)}, indent=2))
        return 1

    pool = load_proxy_pool(pool_path, include_quarantined=True)
    if not pool:
        print(json.dumps({"error": "empty_pool", "path": str(pool_path)}, indent=2))
        return 1

    nodes = _probe_nodes(pool, timeout=float(args.timeout))
    pool_endpoints = {str(n.get("endpoint") or "").lower() for n in nodes if n.get("endpoint")}
    probe_report = {
        "pool": str(pool_path),
        "nodes": len(nodes),
        "cf_ok": sum(1 for n in nodes if n.get("cf_ok")),
        "rows": [{k: n[k] for k in ("endpoint", "egress_ip", "cf_ok", "cf_classification")} for n in nodes],
    }
    print(json.dumps({"phase": "probe", **probe_report}, ensure_ascii=False, indent=2))
    if args.probe_only:
        return 0 if probe_report["cf_ok"] else 1

    account_service.reload_from_storage()
    accounts = [dict(a) for a in account_service.list_accounts(allow_live_cf_probe=False)]
    assignments, meta = plan_assignments(
        accounts,
        nodes,
        pool_endpoints,
        max_per_egress=max(1, int(args.max_per_egress)),
        rebind_all=bool(args.all),
    )
    safe_assignments = [{k: v for k, v in a.items() if k not in {"access_token", "new_proxy"}} for a in assignments]
    print(json.dumps({"phase": "plan", "meta": meta, "assignments": safe_assignments}, ensure_ascii=False, indent=2))

    if not args.apply:
        return 0

    results = []
    for idx, item in enumerate(assignments, start=1):
        print(json.dumps({"event": "rebind_progress", "index": idx, "total": len(assignments), "email": item.get("email")}, ensure_ascii=False), flush=True)
        try:
            row = apply_assignment(item, timeout=float(args.timeout), validate=bool(args.validate_proxy))
        except Exception as exc:
            row = {"email": item.get("email"), "ok": False, "error": f"{type(exc).__name__}:{exc}"[:240]}
        results.append(row)
        time.sleep(0.25)

    account_service.reload_from_storage()
    sched = sum(
        1
        for a in account_service.list_accounts(allow_live_cf_probe=False)
        if a.get("image_schedulable")
    )
    summary = {
        "phase": "apply",
        "ok": sum(1 for r in results if r.get("ok")),
        "fail": sum(1 for r in results if not r.get("ok")),
        "image_schedulable": sched,
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
