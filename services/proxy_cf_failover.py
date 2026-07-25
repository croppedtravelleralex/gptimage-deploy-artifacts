"""Swap account sticky Webshare after repeated CF403; quarantine dead endpoints."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

from services.account_identity import proxy_binding_hash
from services.account_service import account_service
from services.proxy_health import measure_proxy_egress_ip
from services.proxy_quarantine import is_gpt_unavailable_proxy, mark_gpt_unavailable, proxy_endpoint_key

_LOCK = threading.Lock()
_CF_STREAK: dict[str, int] = {}
DEFAULT_POOL = Path(__file__).resolve().parents[1] / "data" / "runlogs" / "webshare_pool_100.txt"
PANDA_POOL = Path("/app/data/runlogs/webshare_100_proxies.secret.txt")
DEFAULT_CF_THRESHOLD = 2


def _parse_pool_line(line: str) -> str:
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    parts = text.split(":")
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return ""


def load_proxy_pool(path: Path | None = None, *, include_quarantined: bool = False) -> list[str]:
    target = path or _default_pool_path()
    if not target.is_file():
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        url = _parse_pool_line(line)
        if not url:
            continue
        key = proxy_endpoint_key(url)
        if not key or key in seen:
            continue
        if not include_quarantined and is_gpt_unavailable_proxy(url):
            continue
        seen.add(key)
        urls.append(url)
    return urls


def _default_pool_path() -> Path:
    override = str(os.environ.get("GPTIMAGE_WEBSHARE_POOL") or "").strip()
    if override:
        return Path(override)
    for candidate in (PANDA_POOL, DEFAULT_POOL):
        if candidate.is_file():
            return candidate
    return DEFAULT_POOL


def reset_cf_streak(access_token: str) -> None:
    token = str(access_token or "").strip()
    if not token:
        return
    with _LOCK:
        _CF_STREAK.pop(token, None)


def bump_cf_streak(access_token: str) -> int:
    token = str(access_token or "").strip()
    with _LOCK:
        count = int(_CF_STREAK.get(token) or 0) + 1
        _CF_STREAK[token] = count
        return count


def pick_clean_proxy(*, exclude: set[str] | None = None, pool_path: Path | None = None) -> str:
    from services.proxy_cf_eligibility import pick_cf_verified_proxy, require_cf_ok_for_image

    blocked = set(exclude or set())
    pool = load_proxy_pool(pool_path)
    if require_cf_ok_for_image():
        picked = pick_cf_verified_proxy(pool, exclude=blocked)
        return picked[0] if picked else ""
    for url in pool:
        key = proxy_endpoint_key(url)
        if key and key not in blocked:
            return url
    return ""


def _used_egress_ips(*, exclude_token: str = "") -> set[str]:
    used: set[str] = set()
    try:
        accounts = account_service.list_accounts()
    except Exception:
        return used
    exclude = str(exclude_token or "").strip()
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if exclude and str(account.get("access_token") or "").strip() == exclude:
            continue
        ip = str(account.get("proxy_egress_ip") or "").strip()
        if ip:
            used.add(ip)
    return used


def pick_swap_proxy(
    *,
    exclude: set[str] | None = None,
    exclude_egress: set[str] | None = None,
    pool_path: Path | None = None,
) -> str:
    """Pick next CF-verified Webshare proxy for poll CF swap."""
    from services.proxy_cf_eligibility import pick_cf_verified_proxy, require_cf_ok_for_image

    blocked = set(exclude or set())
    blocked_egress = set(exclude_egress or set())
    pool = load_proxy_pool(pool_path, include_quarantined=False)
    if require_cf_ok_for_image():
        picked = pick_cf_verified_proxy(pool, exclude=blocked, exclude_egress=blocked_egress)
        return picked[0] if picked else ""

    def _pick(*, include_quarantined: bool) -> str:
        for url in load_proxy_pool(pool_path, include_quarantined=include_quarantined):
            key = proxy_endpoint_key(url)
            if not key or key in blocked:
                continue
            if blocked_egress:
                sample = measure_proxy_egress_ip(url, timeout=12.0)
                if not sample.get("ok"):
                    continue
                ip = str(sample.get("ip") or "").strip()
                if ip and ip in blocked_egress:
                    continue
            return url
        return ""

    picked = _pick(include_quarantined=False)
    if picked:
        return picked
    return _pick(include_quarantined=True)


def swap_account_proxy_on_cf(
    access_token: str,
    *,
    pool_path: Path | None = None,
    threshold: int = DEFAULT_CF_THRESHOLD,
    reason: str = "cf403",
    exclude_endpoints: set[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """After ``threshold`` CF signals, quarantine old endpoint and bind a clean Webshare proxy."""
    token = str(access_token or "").strip()
    if not token:
        return {"ok": False, "error": "missing_token"}
    streak = bump_cf_streak(token)
    if not force and streak < max(1, int(threshold)):
        return {"ok": False, "skipped": True, "streak": streak, "threshold": threshold}

    account = account_service.get_account(token) or {}
    old_proxy = str(account.get("proxy") or "").strip()
    if not old_proxy:
        return {"ok": False, "error": "missing_proxy", "streak": streak}

    blocked = set(exclude_endpoints or set())
    old_endpoint = proxy_endpoint_key(old_proxy)
    if old_endpoint:
        blocked.add(old_endpoint)
    used_egress = _used_egress_ips(exclude_token=token)
    new_proxy = pick_swap_proxy(exclude=blocked, exclude_egress=used_egress, pool_path=pool_path)
    if not new_proxy:
        return {"ok": False, "error": "no_clean_proxy_available", "streak": streak}

    from services.proxy_cf_eligibility import cf_probe_account_fields, require_cf_ok_for_image

    cf_probe: dict[str, Any] = {}
    if require_cf_ok_for_image():
        from services.proxy_cf_eligibility import assert_proxy_cf_ok_for_image

        try:
            cf_probe = assert_proxy_cf_ok_for_image(new_proxy)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "streak": streak, "new_endpoint": proxy_endpoint_key(new_proxy)}

    mark_gpt_unavailable(
        old_proxy,
        reason=reason,
        former_account=str(account.get("email") or ""),
    )
    sample = measure_proxy_egress_ip(new_proxy, timeout=20.0)
    if not sample.get("ok"):
        return {
            "ok": False,
            "error": str(sample.get("error") or "new_proxy_egress_failed"),
            "streak": streak,
            "new_endpoint": proxy_endpoint_key(new_proxy),
        }

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
    }
    if cf_probe:
        updates.update(cf_probe_account_fields(new_proxy, cf_probe))
    updated = account_service.update_account_identity(
        token,
        updates,
        reason="cf403_proxy_swap",
        quiet=False,
    )
    account_service.reset_observability_lights(token)
    reset_cf_streak(token)
    return {
        "ok": bool(updated),
        "streak": streak,
        "old_endpoint": proxy_endpoint_key(old_proxy),
        "new_endpoint": proxy_endpoint_key(new_proxy),
        "new_egress_ip": egress_ip,
        "quarantined": True,
        "lights_reset": True,
    }


def maybe_swap_after_cf_layers(
    access_token: str,
    cf_layers: dict[str, Any] | None,
    *,
    pool_path: Path | None = None,
    threshold: int = DEFAULT_CF_THRESHOLD,
) -> dict[str, Any]:
    layers = cf_layers if isinstance(cf_layers, dict) else {}
    propagated = int(layers.get("propagated_cf") or 0) > 0
    home_soft = bool(layers.get("home_403_soft_fail"))
    tasks_cf = int(layers.get("tasks_cf403") or 0) > 0
    if not (propagated or tasks_cf or home_soft):
        return {"ok": False, "skipped": True, "reason": "no_cf_signal"}
    return swap_account_proxy_on_cf(
        access_token,
        pool_path=pool_path,
        threshold=threshold,
        reason="cf403_propagated" if propagated else "home_403_soft_fail",
    )
