"""Lightweight Webshare CF probe: home + chat-requirements/prepare (no account token)."""

from __future__ import annotations

import time
from typing import Any

from services.proxy_health import measure_proxy_egress_ip
from services.proxy_quarantine import proxy_endpoint_key

BASE = "https://chatgpt.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _is_cf_html(status: int, body: str = "") -> bool:
    if int(status) == 403:
        return True
    text = str(body or "").lower()
    return any(
        marker in text
        for marker in (
            "cloudflare",
            "cf-browser-verification",
            "just a moment",
            "attention required",
            "edge_html_block",
        )
    )


def classify_cf_probe(
    *,
    home_status: int | None,
    requirements_status: int | None,
    requirements_ok: bool,
    home_cf: bool,
    requirements_cf: bool,
) -> str:
    if requirements_cf or (requirements_status == 403):
        return "cf403"
    if home_cf and not requirements_ok:
        return "cf403"
    if int(home_status or 0) == 403 and not requirements_ok:
        return "home_403_soft_fail"
    return "none"


def probe_proxy_cf(proxy_url: str, *, timeout: float = 45.0) -> dict[str, Any]:
    """Probe one sticky Webshare proxy for CF edge blocks."""
    from curl_cffi import requests

    from utils.pow import build_legacy_requirements_token, parse_pow_resources

    proxy = str(proxy_url or "").strip()
    started = time.time()
    endpoint = proxy_endpoint_key(proxy)
    egress = measure_proxy_egress_ip(proxy, timeout=min(timeout, 25.0))
    home_status: int | None = None
    requirements_status: int | None = None
    requirements_ok = False
    home_cf = False
    requirements_cf = False
    error = ""
    session = requests.Session(
        impersonate="chrome131",
        verify=False,
        proxies={"http": proxy, "https": proxy},
        timeout=timeout,
    )
    try:
        home = session.get(
            BASE + "/",
            headers={"User-Agent": DEFAULT_USER_AGENT},
            timeout=timeout,
        )
        home_status = int(getattr(home, "status_code", 0) or 0)
        home_body = str(getattr(home, "text", "") or "")
        home_cf = _is_cf_html(home_status, home_body)
        scripts, build = parse_pow_resources(home_body) if home_status < 400 else ([], "")
        p_token = build_legacy_requirements_token(DEFAULT_USER_AGENT, scripts, build)
        prep = session.post(
            BASE + "/backend-api/sentinel/chat-requirements/prepare",
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": BASE,
                "Referer": BASE + "/",
            },
            json={"p": p_token},
            timeout=timeout,
        )
        requirements_status = int(getattr(prep, "status_code", 0) or 0)
        prep_body = str(getattr(prep, "text", "") or "")
        requirements_cf = _is_cf_html(requirements_status, prep_body)
        requirements_ok = requirements_status == 200 and not requirements_cf
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()

    classification = classify_cf_probe(
        home_status=home_status,
        requirements_status=requirements_status,
        requirements_ok=requirements_ok,
        home_cf=home_cf,
        requirements_cf=requirements_cf,
    )
    cf403 = classification in {"cf403", "home_403_soft_fail"} and not requirements_ok
    return {
        "ok": requirements_ok and not cf403,
        "cf403": bool(cf403),
        "cf_classification": classification,
        "proxy_endpoint": endpoint,
        "egress": egress,
        "home_status": home_status,
        "requirements_status": requirements_status,
        "requirements_ok": requirements_ok,
        "error": error[:240] if error else "",
        "elapsed_ms": int((time.time() - started) * 1000),
    }
