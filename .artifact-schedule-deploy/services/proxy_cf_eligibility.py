"""Webshare ↔ CF eligibility gate for image-generation proxies."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from services.proxy_cf_probe import probe_proxy_cf
from services.proxy_quarantine import is_gpt_unavailable_proxy, mark_gpt_unavailable, proxy_endpoint_key


def _cf_policy() -> dict[str, Any]:
    try:
        from services.config import config

        settings = config.get_webshare_cf_scan_settings()
        if isinstance(settings, dict):
            return settings
    except Exception:
        pass
    return {}


def require_cf_ok_for_image() -> bool:
    return bool(_cf_policy().get("require_cf_ok_for_image", True))


def probe_on_assign() -> bool:
    return bool(_cf_policy().get("probe_on_assign", True))


def scan_stale_sec() -> float:
    return max(300.0, float(_cf_policy().get("scan_stale_sec") or 86400))


def block_unscanned_for_schedule() -> bool:
    return bool(_cf_policy().get("block_unscanned_for_schedule", True))


def _scan_report_path() -> Path:
    try:
        from services.config import DATA_DIR

        return Path(DATA_DIR) / "runlogs" / "webshare_cf_scan_latest.json"
    except Exception:
        return Path(__file__).resolve().parents[1] / "data" / "runlogs" / "webshare_cf_scan_latest.json"


def load_scan_index(*, max_age_sec: float | None = None) -> dict[str, dict[str, Any]]:
    """endpoint -> latest scan node (only if report is fresh enough)."""
    path = _scan_report_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    generated_at = str(payload.get("generated_at") or "").strip()
    age_limit = scan_stale_sec() if max_age_sec is None else max(0.0, float(max_age_sec))
    if generated_at:
        try:
            from datetime import datetime, timezone

            ts = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = time.time() - ts.timestamp()
            if age > age_limit:
                return {}
        except Exception:
            pass
    out: dict[str, dict[str, Any]] = {}
    for node in payload.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        endpoint = str(node.get("proxy_endpoint") or node.get("proxy_hash") or "").strip().lower()
        if endpoint:
            out[endpoint] = node
    return out


def scan_verdict(endpoint: str) -> bool | None:
    """True=cf ok, False=cf bad, None=unknown/stale."""
    key = str(endpoint or "").strip().lower()
    if not key:
        return None
    node = load_scan_index().get(key)
    if not node:
        return None
    return bool(node.get("ok"))


def account_cf_cache_ok(account: dict | None, *, proxy_url: str) -> bool:
    if not isinstance(account, dict):
        return False
    if not bool(account.get("proxy_cf_ok")):
        return False
    cached_endpoint = str(account.get("proxy_cf_probe_endpoint") or "").strip().lower()
    current = proxy_endpoint_key(proxy_url)
    if not cached_endpoint or cached_endpoint != current:
        return False
    try:
        ok_at = float(account.get("proxy_cf_ok_at") or 0)
    except (TypeError, ValueError):
        return False
    if ok_at <= 0:
        return False
    return (time.time() - ok_at) <= scan_stale_sec()


def is_proxy_cf_ok_for_image(
    proxy_url: str,
    *,
    account: dict | None = None,
    allow_live_probe: bool = False,
    probe_timeout: float | None = None,
) -> bool:
    """Return whether sticky Webshare may carry image traffic."""
    proxy = str(proxy_url or "").strip()
    if not proxy:
        return False
    if not require_cf_ok_for_image():
        return not is_gpt_unavailable_proxy(proxy)
    if is_gpt_unavailable_proxy(proxy):
        return False
    if account_cf_cache_ok(account, proxy_url=proxy):
        return True
    endpoint = proxy_endpoint_key(proxy)
    verdict = scan_verdict(endpoint)
    if verdict is True:
        return True
    if verdict is False:
        return False
    if block_unscanned_for_schedule() and not allow_live_probe:
        return False
    if allow_live_probe and probe_on_assign():
        result = probe_proxy_cf(proxy, timeout=float(probe_timeout or _cf_policy().get("probe_timeout_sec") or 45.0))
        return bool(result.get("ok"))
    return False


def cf_probe_account_fields(proxy_url: str, probe: dict[str, Any]) -> dict[str, Any]:
    endpoint = proxy_endpoint_key(proxy_url)
    ok = bool(probe.get("ok"))
    fields: dict[str, Any] = {
        "proxy_cf_ok": ok,
        "proxy_cf_ok_at": time.time() if ok else 0,
        "proxy_cf_probe_endpoint": endpoint,
        "proxy_cf_classification": str(probe.get("cf_classification") or ""),
    }
    egress = probe.get("egress")
    if isinstance(egress, dict):
        ip = str(egress.get("ip") or "").strip()
        if ip:
            fields["proxy_egress_ip"] = ip
    return fields


def pick_cf_verified_proxy(
    pool: list[str],
    *,
    exclude: set[str] | None = None,
    exclude_egress: set[str] | None = None,
    probe_timeout: float | None = None,
    measure_egress: Any | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Pick first pool entry that passes egress uniqueness + live CF probe."""
    from services.proxy_health import measure_proxy_egress_ip

    blocked = {str(item).strip().lower() for item in (exclude or set()) if str(item).strip()}
    blocked_egress = {str(item).strip() for item in (exclude_egress or set()) if str(item).strip()}
    timeout = float(probe_timeout or _cf_policy().get("probe_timeout_sec") or 45.0)
    egress_fn = measure_egress_ip if callable(measure_egress) else measure_proxy_egress_ip

    for url in pool:
        key = proxy_endpoint_key(url)
        if not key or key in blocked:
            continue
        if is_gpt_unavailable_proxy(url):
            continue
        if blocked_egress:
            sample = egress_fn(url, timeout=min(timeout, 25.0))
            if not sample.get("ok"):
                continue
            ip = str(sample.get("ip") or "").strip()
            if ip and ip in blocked_egress:
                continue
        if not probe_on_assign():
            if is_proxy_cf_ok_for_image(url, allow_live_probe=False):
                return url, {"ok": True, "cf_classification": "scan_cache", "proxy_endpoint": key}
            continue
        probe = probe_proxy_cf(url, timeout=timeout)
        if not probe.get("ok"):
            if bool(_cf_policy().get("auto_quarantine", True)):
                mark_gpt_unavailable(url, reason=str(probe.get("cf_classification") or "cf403"))
            continue
        return url, probe
    return None


def assert_proxy_cf_ok_for_image(proxy_url: str, *, probe_timeout: float | None = None) -> dict[str, Any]:
    """Live-probe and return probe payload; raises RuntimeError when CF blocks."""
    proxy = str(proxy_url or "").strip()
    if not proxy:
        raise RuntimeError("missing_proxy")
    if is_gpt_unavailable_proxy(proxy):
        raise RuntimeError("proxy_quarantined")
    probe = probe_proxy_cf(proxy, timeout=float(probe_timeout or _cf_policy().get("probe_timeout_sec") or 45.0))
    if not probe.get("ok"):
        if bool(_cf_policy().get("auto_quarantine", True)):
            mark_gpt_unavailable(proxy, reason=str(probe.get("cf_classification") or "cf403"))
        raise RuntimeError(f"cf_probe_failed:{probe.get('cf_classification') or 'unknown'}")
    return probe
