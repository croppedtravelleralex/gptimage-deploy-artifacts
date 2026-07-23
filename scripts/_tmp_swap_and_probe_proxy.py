#!/usr/bin/env python3
"""Find healthy Webshare proxy for account and optionally swap + re-probe."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_ROOT_OVERRIDE = str(os.environ.get("GPTIMAGE_ROOT") or "").strip()
if _ROOT_OVERRIDE and Path(_ROOT_OVERRIDE).is_dir():
    ROOT = Path(_ROOT_OVERRIDE).resolve()
sys.path.insert(0, str(ROOT))

from services.account_identity import proxy_binding_hash
from services.account_service import account_service
from services.proxy_health import measure_proxy_egress_ip, validate_http_proxy


def _load_bench():
    import importlib.util

    path = Path(__file__).resolve().parent / "_tmp_spa_image_bench3.py"
    spec = importlib.util.spec_from_file_location("spa_bench3", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _parse_pool_line(line: str) -> str:
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    parts = text.split(":")
    if len(parts) >= 4:
        from urllib.parse import quote

        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return ""


def load_pool(path: Path) -> list[str]:
    urls = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        url = _parse_pool_line(line)
        if url:
            urls.append(url)
    return urls


def find_account(account_hash: str) -> tuple[str, dict]:
    account_service.reload_from_storage()
    wanted = str(account_hash or "").strip().lower()
    for token in account_service.list_tokens():
        if hashlib.sha256(token.encode()).hexdigest()[:12] == wanted:
            return token, account_service.get_account(token) or {}
    raise SystemExit(f"account not found: {account_hash}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-hash", required=True)
    ap.add_argument("--pool", default="/app/data/runlogs/webshare_100_proxies.secret.txt")
    ap.add_argument("--good-pool", default="/app/data/runlogs/webshare_good_csrf_200.secret.txt")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--force-proxy-hash", default="")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    bench = _load_bench()
    token, account = find_account(args.account_hash)
    fp = account.get("fingerprint") if isinstance(account.get("fingerprint"), dict) else {}
    old_hash = str(account.get("proxy_binding_hash") or "")

    pools: list[str] = []
    for p in (args.good_pool, args.pool):
        path = Path(p)
        if path.is_file():
            pools.extend(load_pool(path))
    seen: set[str] = set()
    candidates: list[str] = []
    force_hash = str(args.force_proxy_hash or "").strip().lower()
    for url in pools:
        h = proxy_binding_hash(url)
        if h in seen or h == old_hash:
            continue
        seen.add(h)
        candidates.append(url)
    all_candidates = list(candidates)
    if force_hash:
        candidates = [url for url in pools if bench.proxy_hash(url) == force_hash]
    else:
        candidates = all_candidates[args.offset : args.offset + max(1, args.limit)]

    results: list[dict] = []
    picked = ""
    for idx, proxy in enumerate(candidates, start=1):
        probe = bench.run_cf_probe(proxy, fp=fp, access_token=token, account=account)
        row = {
            "index": idx,
            "proxy_hash": bench.proxy_hash(proxy),
            "egress_ip": (probe.get("egress") or {}).get("ip"),
            "ok": bool(probe.get("ok")),
            "home_status": probe.get("home_status"),
            "requirements_ok": probe.get("requirements_ok"),
            "cf_classification": probe.get("cf_classification"),
        }
        results.append(row)
        print(json.dumps({"event": "probe", **row}, ensure_ascii=False), flush=True)
        if force_hash and row["proxy_hash"] == force_hash:
            picked = proxy
            break
        if row["ok"] and not picked:
            sticky = validate_http_proxy(proxy, require_sticky=True)
            if sticky.get("ok"):
                picked = proxy
                row["sticky_ok"] = True
            else:
                row["sticky_ok"] = False

    out = {
        "account_hash": args.account_hash,
        "old_proxy_hash": old_hash,
        "scanned": len(results),
        "ok_count": sum(1 for r in results if r.get("ok")),
        "picked_proxy_hash": bench.proxy_hash(picked) if picked else None,
        "picked_egress_ip": None,
        "applied": False,
        "results": results,
    }

    if picked and args.apply:
        sticky = validate_http_proxy(picked, require_sticky=True)
        if not sticky.get("ok"):
            if force_hash:
                print(json.dumps({"event": "sticky_warn", "error": sticky.get("error")}, ensure_ascii=False), flush=True)
                sticky = measure_proxy_egress_ip(picked, timeout=20.0)
            else:
                picked = ""
        if picked:
            updates = {
                "proxy": picked,
                "proxy_provider": "webshare",
                "proxy_scope": "account_sticky",
                "lifecycle_ip_mode": "sticky_one_ip_full",
                "proxy_binding_hash": proxy_binding_hash(picked),
                "proxy_egress_hash": sticky.get("egress_hash"),
                "proxy_egress_ip": sticky.get("ip"),
                "registration_proxy_hash": proxy_binding_hash(picked),
                "registration_egress_hash": sticky.get("egress_hash"),
            }
            account_service.update_account_identity(token, updates, reason="manual_proxy_swap_probe", quiet=False)
            if hasattr(account_service, "reset_observability_lights"):
                account_service.reset_observability_lights(token)
            account_service.update_account(token, {"image_fail_cooldown_until": 0}, quiet=True)
            account_service.fetch_remote_info(token, "post_proxy_swap_quota_check")
            out["applied"] = True
            out["picked_egress_ip"] = sticky.get("ip")
            out["schedulable"] = account_service._is_image_account_schedulable(account_service.get_account(token) or {})
            out["quota"] = (account_service.get_account(token) or {}).get("quota")

    print(json.dumps({"event": "summary", **out}, ensure_ascii=False), flush=True)
    return 0 if picked else 2


if __name__ == "__main__":
    raise SystemExit(main())
