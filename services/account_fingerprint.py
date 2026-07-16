"""Per-account browser fingerprint helpers (TLS/UA/CH aligned)."""

from __future__ import annotations

import uuid
from typing import Any


DEFAULT_IMPERSONATE = "chrome120"
DEFAULT_CHROME_MAJOR = "120"


def build_aligned_chrome_fp(
    *,
    major: str = DEFAULT_CHROME_MAJOR,
    impersonate: str = DEFAULT_IMPERSONATE,
    device_id: str = "",
    session_id: str = "",
    user_agent: str = "",
) -> dict[str, str]:
    chrome_major = str(major or DEFAULT_CHROME_MAJOR).strip() or DEFAULT_CHROME_MAJOR
    full = f"{chrome_major}.0.0.0"
    ua = str(user_agent or "").strip() or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full} Safari/537.36"
    )
    return {
        "user-agent": ua,
        "impersonate": str(impersonate or DEFAULT_IMPERSONATE).strip() or DEFAULT_IMPERSONATE,
        "oai-device-id": str(device_id or uuid.uuid4()).strip() or str(uuid.uuid4()),
        "oai-session-id": str(session_id or uuid.uuid4()).strip() or str(uuid.uuid4()),
        "sec-ch-ua": f'"Google Chrome";v="{chrome_major}", "Not?A_Brand";v="8", "Chromium";v="{chrome_major}"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version": f'"{full}"',
        "sec-ch-ua-full-version-list": (
            f'"Chromium";v="{full}", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="{full}"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-platform-version": '"10.0.0"',
    }


def normalize_fp(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in raw.items() if str(v or "").strip()}


def ensure_complete_fp(account: dict[str, Any] | None) -> tuple[dict[str, str], bool]:
    """Return (fp, created_or_filled)."""
    account = account if isinstance(account, dict) else {}
    fp = normalize_fp(account.get("fp"))
    for key in (
        "user-agent",
        "impersonate",
        "oai-device-id",
        "oai-session-id",
        "sec-ch-ua",
        "sec-ch-ua-arch",
        "sec-ch-ua-bitness",
        "sec-ch-ua-full-version",
        "sec-ch-ua-full-version-list",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-ch-ua-platform-version",
    ):
        value = str(account.get(key) or "").strip()
        if value:
            fp[key] = value
    before = dict(fp)
    defaults = build_aligned_chrome_fp(
        device_id=fp.get("oai-device-id", ""),
        session_id=fp.get("oai-session-id", ""),
        user_agent=fp.get("user-agent", ""),
        impersonate=fp.get("impersonate", "") or DEFAULT_IMPERSONATE,
    )
    for key, value in defaults.items():
        fp.setdefault(key, value)
    # Fix known mismatch: Edge UA + chrome110
    impersonate = str(fp.get("impersonate") or "").lower()
    ua = str(fp.get("user-agent") or "")
    if "edg/" in ua.lower() and impersonate.startswith("chrome"):
        fixed = build_aligned_chrome_fp(
            device_id=fp.get("oai-device-id", ""),
            session_id=fp.get("oai-session-id", ""),
        )
        fp.update(fixed)
    legacy_arch = str(fp.get("sec-ch-ua-arch") or "").strip().strip('"').lower()
    if legacy_arch in {"x86_64", "amd64"}:
        fp["sec-ch-ua-arch"] = '"x86"'
    filled = fp != before
    return fp, filled
