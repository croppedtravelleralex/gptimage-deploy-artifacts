#!/usr/bin/env python3
"""给号池账号换独立 Webshare 代理（1 账号 : 1 binding + 1 egress IP）。

默认 --dry-run；加 --apply 才写库。

策略：
- 按 proxy_binding_hash **与** proxy_egress_ip 建连通分量，组内只保留 1 个账号
- 换绑时实测 egress，确保 endpoint 与 egress IP 均不与池内其他号冲突
- identity_isolated 在成功换到独立出口后清为 verified_ready
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.account_identity import normalize_account_identity, proxy_binding_hash
from services.account_service import account_service
from services.proxy_health import measure_proxy_egress_ip, validate_http_proxy

CANARY_EMAIL = "kevinyoung673141@outlook.com"
DEFAULT_POOL = Path("/app/data/runlogs/webshare_100_proxies.secret.txt")


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _mask_email(email: object) -> str:
    text = str(email or "").strip().lower()
    if "@" not in text:
        return "***"
    local, domain = text.split("@", 1)
    if len(local) <= 3:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-1]}@{domain}"


def _mask_proxy(proxy: object) -> str:
    raw = str(proxy or "").strip()
    return re.sub(r"(://[^:/@]+:)[^@/]+@", r"\1***@", raw)


def _endpoint_key(proxy: object) -> str:
    raw = str(proxy or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if (parsed.scheme or "").lower() == "https" else 80)
        return f"{host}:{port}"
    except Exception:
        return raw


def _parse_pool_line(line: str) -> str:
    text = line.strip()
    if not text or text.startswith("#"):
        return ""
    if "://" in text:
        return text
    # host:port:user:pass
    parts = text.split(":")
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return ""


def load_proxy_pool(path: Path) -> list[str]:
    from services.proxy_cf_failover import load_proxy_pool as load_pool

    urls = load_pool(path, include_quarantined=False)
    if not urls:
        urls = load_pool(path, include_quarantined=True)
    return urls


def _keep_score(account: dict[str, Any]) -> tuple:
    email = str(account.get("email") or "").strip().lower()
    status = str(account.get("status") or "")
    receive = str(account.get("panda_receive_state") or "").strip().lower()
    quota = int(account.get("quota") or 0)
    return (
        0 if email == CANARY_EMAIL else 1,
        0 if receive == "verified_ready" else 1,
        0 if status == "正常" else 1,
        0 if receive == "identity_isolated" else 1,
        -quota,
        email,
    )


def _egress_key_acc(account: dict[str, Any]) -> str:
    return str(account.get("proxy_egress_ip") or account.get("proxy_egress_hash") or "").strip()


def _union_groups(accounts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Merge accounts sharing proxy_binding_hash OR egress IP into components."""
    parent = list(range(len(accounts)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_binding: dict[str, list[int]] = defaultdict(list)
    by_egress: dict[str, list[int]] = defaultdict(list)
    for idx, acc in enumerate(accounts):
        proxy = str(acc.get("proxy") or "").strip()
        binding = str(acc.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(proxy)
        egress = _egress_key_acc(acc)
        if binding:
            by_binding[binding].append(idx)
        if egress:
            by_egress[egress].append(idx)

    for indices in by_binding.values():
        if len(indices) > 1:
            head = indices[0]
            for other in indices[1:]:
                union(head, other)
    for indices in by_egress.values():
        if len(indices) > 1:
            head = indices[0]
            for other in indices[1:]:
                union(head, other)

    components: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for idx, acc in enumerate(accounts):
        components[find(idx)].append(acc)
    return list(components.values())


def plan_rebinds(accounts: list[dict[str, Any]], pool: list[str]) -> dict[str, Any]:
    with_proxy = [acc for acc in accounts if str(acc.get("proxy") or "").strip()]
    no_proxy = [acc for acc in accounts if not str(acc.get("proxy") or "").strip()]

    keep: list[dict[str, Any]] = []
    rebind: list[dict[str, Any]] = []
    used_endpoints: set[str] = set()
    used_egress: set[str] = set()

    for group in _union_groups(with_proxy):
        ordered = sorted(group, key=_keep_score)
        keeper = ordered[0]
        keep.append(keeper)
        used_endpoints.add(_endpoint_key(keeper.get("proxy")))
        egress = _egress_key_acc(keeper)
        if egress:
            used_egress.add(egress)
        for peer in ordered[1:]:
            rebind.append(peer)

    rebind.extend(no_proxy)

    assignments = []
    for acc in sorted(rebind, key=lambda a: str(a.get("email") or "").lower()):
        assignments.append(
            {
                "email": str(acc.get("email") or ""),
                "email_masked": _mask_email(acc.get("email")),
                "status": acc.get("status"),
                "quota": acc.get("quota"),
                "old_panda": acc.get("panda_receive_state"),
                "old_binding": (acc.get("proxy_binding_hash") or proxy_binding_hash(acc.get("proxy")))[:12],
                "old_egress": _egress_key_acc(acc),
                "old_proxy_masked": _mask_proxy(acc.get("proxy")),
                "old_endpoint": _endpoint_key(acc.get("proxy")),
                "access_token": acc.get("access_token"),
                "clear_isolation": str(acc.get("panda_receive_state") or "").strip().lower() == "identity_isolated",
            }
        )

    return {
        "keep_count": len(keep),
        "rebind_count": len(assignments),
        "pool_size": len(pool),
        "used_endpoints": sorted(used_endpoints),
        "used_egress": sorted(used_egress),
        "keepers": [
            {
                "email_masked": _mask_email(a.get("email")),
                "status": a.get("status"),
                "quota": a.get("quota"),
                "panda": a.get("panda_receive_state"),
                "binding": (a.get("proxy_binding_hash") or "")[:12],
                "egress": _egress_key_acc(a),
                "endpoint": _endpoint_key(a.get("proxy")),
            }
            for a in sorted(keep, key=_keep_score)
        ],
        "assignments": assignments,
    }


def _pick_unique_proxy(
    pool: list[str],
    *,
    used_endpoints: set[str],
    used_egress: set[str],
    timeout: float,
    require_cf: bool = True,
) -> tuple[str, str, str] | None:
    from services.proxy_cf_eligibility import pick_cf_verified_proxy, require_cf_ok_for_image

    if require_cf and require_cf_ok_for_image():
        picked = pick_cf_verified_proxy(
            pool,
            exclude=used_endpoints,
            exclude_egress=used_egress,
            probe_timeout=timeout,
        )
        if not picked:
            return None
        url, probe = picked
        egress = probe.get("egress") if isinstance(probe.get("egress"), dict) else {}
        egress_ip = str(egress.get("ip") or "").strip()
        egress_hash = str(egress.get("egress_hash") or "").strip()
        if not egress_ip or not egress_hash:
            sample = measure_proxy_egress_ip(url, timeout=timeout)
            if not sample.get("ok"):
                return None
            egress_ip = str(sample.get("ip") or "").strip()
            egress_hash = str(sample.get("egress_hash") or "").strip()
        if egress_ip and egress_ip in used_egress:
            return None
        return url, egress_ip, egress_hash

    for url in pool:
        ep = _endpoint_key(url)
        if not ep or ep in used_endpoints:
            continue
        sample = measure_proxy_egress_ip(url, timeout=timeout)
        if not sample.get("ok"):
            continue
        egress_ip = str(sample.get("ip") or "").strip()
        egress_hash = str(sample.get("egress_hash") or "").strip()
        if egress_ip and egress_ip in used_egress:
            continue
        return url, egress_ip, egress_hash
    return None


def apply_one(
    item: dict[str, Any],
    *,
    pool: list[str],
    used_endpoints: set[str],
    used_egress: set[str],
    validate: bool,
    timeout: float,
) -> dict[str, Any]:
    token = str(item.get("access_token") or "")
    row = {
        "email_masked": item.get("email_masked"),
        "ok": False,
        "old_binding": item.get("old_binding"),
        "old_egress": item.get("old_egress"),
    }
    picked = _pick_unique_proxy(pool, used_endpoints=used_endpoints, used_egress=used_egress, timeout=timeout)
    if not picked:
        row["error"] = "no_unique_egress_proxy"
        return row
    new_proxy, egress_ip, egress_hash = picked
    if validate:
        check = validate_http_proxy(new_proxy, timeout=timeout, require_sticky=True, sticky_gap_sec=1.5)
        row["validate_ok"] = bool(check.get("ok"))
        if not check.get("ok"):
            row["error"] = str(check.get("error") or "proxy_validate_failed")[:200]
            return row
        egress_hash = str(check.get("egress_hash") or egress_hash)
        egress_ip = str(check.get("ip") or check.get("egress_ip") or egress_ip)

    if not egress_hash:
        row["error"] = "missing_egress_hash"
        return row

    from services.proxy_cf_eligibility import cf_probe_account_fields, require_cf_ok_for_image

    if require_cf_ok_for_image():
        from services.proxy_cf_eligibility import assert_proxy_cf_ok_for_image

        try:
            cf_probe = assert_proxy_cf_ok_for_image(new_proxy, probe_timeout=timeout)
            updates_cf = cf_probe_account_fields(new_proxy, cf_probe)
        except RuntimeError as exc:
            row["error"] = str(exc)[:200]
            return row
    else:
        updates_cf = {}

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
    }
    updates.update(updates_cf)
    clear_isolation = bool(item.get("clear_isolation"))
    if clear_isolation:
        updates["panda_receive_state"] = "verified_ready"

    updated = account_service.update_account_identity(
        token,
        updates,
        reason="unique_proxy_rebind",
        quiet=False,
        clear_isolation=clear_isolation,
    )
    account_service._clear_image_preflight_failure(token)
    after = updated or account_service.get_account(token) or {}
    normalized = normalize_account_identity(dict(after))
    used_endpoints.add(_endpoint_key(new_proxy))
    if egress_ip:
        used_egress.add(egress_ip)
    row.update(
        {
            "ok": True,
            "new_binding": str(normalized.get("proxy_binding_hash") or "")[:12],
            "new_egress": egress_ip,
            "new_endpoint": _endpoint_key(new_proxy),
            "after_panda": normalized.get("panda_receive_state"),
            "after_sched": account_service._is_image_account_schedulable(normalized),
            "egress_hash": egress_hash[:12],
        }
    )
    return row


def binding_uniqueness_report(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    by_binding: dict[str, list[str]] = defaultdict(list)
    by_egress: dict[str, list[str]] = defaultdict(list)
    for acc in accounts:
        email = _mask_email(acc.get("email"))
        binding = str(acc.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(acc.get("proxy"))
        egress = _egress_key_acc(acc)
        if binding:
            by_binding[binding[:12]].append(email)
        if egress:
            by_egress[egress].append(email)
    return {
        "account_count": len(accounts),
        "unique_bindings": sum(1 for v in by_binding.values() if len(v) == 1),
        "shared_bindings": {k: v for k, v in by_binding.items() if len(v) > 1},
        "shared_egress": {k: v for k, v in by_egress.items() if len(v) > 1},
        "schedulable": [
            _mask_email(a.get("email"))
            for a in accounts
            if account_service._is_image_account_schedulable(a)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebind accounts to unique Webshare proxies")
    parser.add_argument("--pool", default=str(DEFAULT_POOL))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--validate-proxy", action="store_true", help="sticky validate before write")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--limit", type=int, default=0, help="only first N rebinds (0=all)")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    pool_path = Path(args.pool)
    if not pool_path.is_file():
        raise SystemExit(f"proxy pool missing: {pool_path}")

    accounts = [dict(a) for a in account_service.list_accounts()]
    try:
        account_service.reload_from_storage()
        accounts = [dict(a) for a in account_service.list_accounts()]
    except Exception:
        pass
    pool = load_proxy_pool(pool_path)
    plan = plan_rebinds(accounts, pool)
    if args.limit and args.limit > 0:
        plan["assignments"] = plan["assignments"][: args.limit]
        plan["rebind_count"] = len(plan["assignments"])

    out_dir = Path(args.out_dir) if args.out_dir else Path("/app/data/runlogs") / f"unique-proxy-rebind-{_utc()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # scrub tokens from saved plan
    safe_plan = {
        **plan,
        "assignments": [{k: v for k, v in item.items() if k != "access_token"} for item in plan["assignments"]],
    }
    (out_dir / "plan.json").write_text(json.dumps(safe_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": "dry-run" if not args.apply else "apply", "out": str(out_dir), **{k: plan[k] for k in ("keep_count", "rebind_count", "pool_size")}}, ensure_ascii=False, indent=2))
    print(json.dumps({"keepers": plan["keepers"], "assignment_preview": safe_plan["assignments"][:20]}, ensure_ascii=False, indent=2))

    if not args.apply:
        before = binding_uniqueness_report(accounts)
        (out_dir / "before.json").write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"before": before}, ensure_ascii=False, indent=2))
        return 0

    used_endpoints = set(plan.get("used_endpoints") or [])
    used_egress = set(plan.get("used_egress") or [])
    results = []
    for idx, item in enumerate(plan["assignments"], start=1):
        print(json.dumps({"event": "rebind_progress", "index": idx, "total": len(plan["assignments"]), "email": item.get("email_masked")}, ensure_ascii=False), flush=True)
        try:
            row = apply_one(
                item,
                pool=pool,
                used_endpoints=used_endpoints,
                used_egress=used_egress,
                validate=bool(args.validate_proxy),
                timeout=float(args.timeout),
            )
        except Exception as exc:
            row = {"email_masked": item.get("email_masked"), "ok": False, "error": f"{type(exc).__name__}:{exc}"[:240]}
        results.append(row)
        time.sleep(0.2)

    after_accounts = [dict(a) for a in account_service.list_accounts()]
    after = binding_uniqueness_report(after_accounts)
    summary = {
        "ok": sum(1 for r in results if r.get("ok")),
        "fail": sum(1 for r in results if not r.get("ok")),
        "after": after,
    }
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        import urllib.request

        auth = __import__("json").load(open("/app/config.json"))["auth-key"]
        urllib.request.urlopen(
            urllib.request.Request(
                "http://127.0.0.1:80/api/accounts/reload-from-storage",
                method="POST",
                headers={"Authorization": f"Bearer {auth}"},
            ),
            timeout=30,
        ).read()
    except Exception:
        pass
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["fail"] == 0 and not after.get("shared_bindings") and not after.get("shared_egress") else 1


if __name__ == "__main__":
    raise SystemExit(main())
