"""Lightweight proxy URL helpers shared by refresh / register paths."""

from __future__ import annotations

from urllib.parse import urlparse


def is_local_only_proxy_url(value: object) -> bool:
    raw = str(getattr(value, "url", value) or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    return str(parsed.hostname or "").strip().lower() in {
        "127.0.0.1",
        "localhost",
        "::1",
        "0.0.0.0",
    }
