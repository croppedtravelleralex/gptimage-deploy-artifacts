#!/usr/bin/env python3
"""给号池账号换独立 Webshare 代理绑定（1 账号 : 1 binding）。

默认 --dry-run；加 --apply 才写库。

策略：
- 已独占 binding 的账号保留现有代理
- 共享 binding 组内保留 1 个（优先 canary kevin / 最高额度正常号），其余换新代理
- identity_isolated 在成功换到独立 binding 后清为 verified_ready
- rejected/禁用 也尽量独占绑定，但不自动改 receive_state
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
    from services.proxy_quarantine import is_gpt_unavailable_proxy

    urls: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        url = _parse_pool_line(line)
        if not url:
            continue
        key = _endpoint_key(url)
        if not key or key in seen:
            continue
        if is_gpt_unavailable_proxy(url):
            continue
        seen.add(key)
        urls.append(url)
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


def plan_rebinds(accounts: list[dict[str, Any]], pool: list[str]) -> dict[str, Any]:
    by_binding: dict[str, list[dict[str, Any]]] = defaultdict(list)
    no_proxy: list[dict[str, Any]] = []
    for acc in accounts:
        proxy = str(acc.get("proxy") or "").strip()
        binding = str(acc.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(proxy)
        if not proxy or not binding:
            no_proxy.append(acc)
            continue
        by_binding[binding].append(acc)

    keep: list[dict[str, Any]] = []
    rebind: list[dict[str, Any]] = []
    used_endpoints: set[str] = set()

    for binding, group in by_binding.items():
        ordered = sorted(group, key=_keep_score)
        keeper = ordered[0]
        keep.append(keeper)
        used_endpoints.add(_endpoint_key(keeper.get("proxy")))
        for peer in ordered[1:]:
            rebind.append(peer)

    for acc in no_proxy:
        rebind.append(acc)

    available = [url for url in pool if _endpoint_key(url) not in used_endpoints]
    # also exclude endpoints already held by keepers (already in used)
    if len(available) < len(rebind):
        raise RuntimeError(f"not enough free proxies: need={len(rebind)} free={len(available)} pool={len(pool)}")

    assignments = []
    for acc, new_proxy in zip(sorted(rebind, key=lambda a: str(a.get("email") or "").lower()), available):
        assignments.append(
            {
                "email": str(acc.get("email") or ""),
                "email_masked": _mask_email(acc.get("email")),
                "status": acc.get("status"),
                "quota": acc.get("quota"),
                "old_panda": acc.get("panda_receive_state"),
                "old_binding": (acc.get("proxy_binding_hash") or proxy_binding_hash(acc.get("proxy")))[:12],
                "old_proxy_masked": _mask_proxy(acc.get("proxy")),
                "old_endpoint": _endpoint_key(acc.get("proxy")),
                "new_proxy": new_proxy,
                "new_proxy_masked": _mask_proxy(new_proxy),
                "new_endpoint": _endpoint_key(new_proxy),
                "new_binding": proxy_binding_hash(new_proxy),
                "access_token": acc.get("access_token"),
                "clear_isolation": str(acc.get("panda_receive_state") or "").strip().lower() == "identity_isolated",
            }
        )

    return {
        "keep_count": len(keep),
        "rebind_count": len(assignments),
        "free_proxies_after": len(available) - len(assignments),
        "keepers": [
            {
                "email_masked": _mask_email(a.get("email")),
                "status": a.get("status"),
                "quota": a.get("quota"),
                "panda": a.get("panda_receive_state"),
                "binding": (a.get("proxy_binding_hash") or "")[:12],
                "endpoint": _endpoint_key(a.get("proxy")),
            }
            for a in sorted(keep, key=_keep_score)
        ],
        "assignments": assignments,
    }


def apply_one(item: dict[str, Any], *, validate: bool, timeout: float) -> dict[str, Any]:
    token = str(item.get("access_token") or "")
    new_proxy = str(item.get("new_proxy") or "")
    row = {
        "email_masked": item.get("email_masked"),
        "ok": False,
        "old_binding": item.get("old_binding"),
        "new_binding": (item.get("new_binding") or "")[:12],
        "new_endpoint": item.get("new_endpoint"),
        "clear_isolation": bool(item.get("clear_isolation")),
    }
    if validate:
        check = validate_http_proxy(new_proxy, timeout=timeout, require_sticky=True, sticky_gap_sec=1.5)
        row["validate_ok"] = bool(check.get("ok"))
        if not check.get("ok"):
            row["error"] = str(check.get("error") or "proxy_validate_failed")[:200]
            return row
        egress_hash = str(check.get("egress_hash") or "")
        egress_ip = str(check.get("ip") or check.get("egress_ip") or "")
    else:
        sample = measure_proxy_egress_ip(new_proxy, timeout=timeout)
        if not sample.get("ok"):
            row["error"] = str(sample.get("error") or "egress_failed")[:200]
            return row
        egress_hash = str(sample.get("egress_hash") or "")
        egress_ip = str(sample.get("ip") or "")

    if not egress_hash:
        row["error"] = "missing_egress_hash"
        return row

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
    after = updated or account_service.get_account(token) or {}
    normalized = normalize_account_identity(dict(after))
    row.update(
        {
            "ok": True,
            "after_binding": str(normalized.get("proxy_binding_hash") or "")[:12],
            "after_panda": normalized.get("panda_receive_state"),
            "after_sched": account_service._is_image_account_schedulable(normalized),
            "egress_hash": egress_hash[:12],
        }
    )
    return row


def binding_uniqueness_report(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[str]] = defaultdict(list)
    for acc in accounts:
        binding = str(acc.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(acc.get("proxy"))
        if not binding:
            groups["<empty>"].append(_mask_email(acc.get("email")))
            continue
        groups[binding].append(_mask_email(acc.get("email")))
    shared = {k[:12]: v for k, v in groups.items() if k != "<empty>" and len(v) > 1}
    return {
        "account_count": len(accounts),
        "unique_bindings": sum(1 for k, v in groups.items() if k != "<empty>" and len(v) == 1),
        "shared_bindings": shared,
        "empty_proxy": groups.get("<empty>", []),
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
        "assignments": [
            {k: v for k, v in item.items() if k not in {"access_token", "new_proxy"}}
            | {"new_proxy_masked": item.get("new_proxy_masked")}
            for item in plan["assignments"]
        ],
    }
    (out_dir / "plan.json").write_text(json.dumps(safe_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": "dry-run" if not args.apply else "apply", "out": str(out_dir), **{k: plan[k] for k in ("keep_count", "rebind_count", "free_proxies_after")}}, ensure_ascii=False, indent=2))
    print(json.dumps({"keepers": plan["keepers"], "assignment_preview": safe_plan["assignments"][:20]}, ensure_ascii=False, indent=2))

    if not args.apply:
        before = binding_uniqueness_report(accounts)
        (out_dir / "before.json").write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"before": before}, ensure_ascii=False, indent=2))
        return 0

    results = []
    for idx, item in enumerate(plan["assignments"], start=1):
        print(json.dumps({"event": "rebind_progress", "index": idx, "total": len(plan["assignments"]), "email": item.get("email_masked")}, ensure_ascii=False), flush=True)
        try:
            row = apply_one(item, validate=bool(args.validate_proxy), timeout=float(args.timeout))
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
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["fail"] == 0 and not after.get("shared_bindings") else 1


if __name__ == "__main__":
    raise SystemExit(main())
