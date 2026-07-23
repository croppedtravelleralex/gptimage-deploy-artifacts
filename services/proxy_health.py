"""Shared residential proxy health checks (Webshare / UDeal / generic HTTP)."""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any


def measure_proxy_egress_ip(proxy_url: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """Return public IP seen through proxy via Cloudflare trace."""
    from curl_cffi import requests

    started = time.time()
    session = requests.Session(
        impersonate="chrome110",
        proxies={"http": proxy_url, "https": proxy_url},
        verify=False,
    )
    try:
        response = session.get("https://cloudflare.com/cdn-cgi/trace", timeout=timeout)
        text = str(response.text or "")
        match = re.search(r"(?m)^ip=([0-9a-fA-F:.]+)\s*$", text)
        ip = match.group(1).strip() if match else ""
        loc_match = re.search(r"(?m)^loc=([A-Z]{2})\s*$", text)
        loc = loc_match.group(1).strip() if loc_match else ""
        return {
            "ok": bool(ip) and int(response.status_code) < 500,
            "ip": ip,
            "loc": loc,
            "egress_hash": hashlib.sha256(ip.encode("utf-8")).hexdigest()[:12] if ip else "",
            "status": int(response.status_code),
            "elapsed_sec": round(time.time() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_sec": round(time.time() - started, 2),
        }
    finally:
        session.close()


def validate_http_proxy(
    proxy_url: str,
    *,
    timeout: float = 20.0,
    require_sticky: bool = True,
    sticky_gap_sec: float = 2.0,
) -> dict[str, Any]:
    """CSRF reachability + optional double egress hash consistency."""
    from curl_cffi import requests

    started = time.time()
    session = requests.Session(
        impersonate="chrome110",
        proxies={"http": proxy_url, "https": proxy_url},
        verify=False,
    )
    try:
        response = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
                ),
            },
            timeout=timeout,
        )
        csrf_ok = int(response.status_code) == 200
        result: dict[str, Any] = {
            "ok": csrf_ok,
            "status": int(response.status_code),
            "elapsed_sec": round(time.time() - started, 2),
        }
        if not csrf_ok:
            return result
        first = measure_proxy_egress_ip(proxy_url, timeout=timeout)
        result["egress"] = first
        if not first.get("ok"):
            result["ok"] = False
            result["error"] = first.get("error") or "egress_failed"
            return result
        if require_sticky:
            time.sleep(max(0.0, sticky_gap_sec))
            second = measure_proxy_egress_ip(proxy_url, timeout=timeout)
            result["egress_recheck"] = second
            if not second.get("ok") or second.get("egress_hash") != first.get("egress_hash"):
                result["ok"] = False
                result["error"] = "egress_not_sticky"
                return result
        result["egress_hash"] = first.get("egress_hash")
        result["ip"] = first.get("ip")
        result["loc"] = first.get("loc")
        return result
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_sec": round(time.time() - started, 2),
        }
    finally:
        session.close()
