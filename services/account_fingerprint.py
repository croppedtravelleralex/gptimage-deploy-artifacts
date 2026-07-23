"""Per-account browser fingerprint helpers (TLS/UA/CH aligned)."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any


DEFAULT_IMPERSONATE = "chrome120"
DEFAULT_CHROME_MAJOR = "120"

# Stable, reproducible profile pool for new / incomplete fingerprints.
# Existing complete fps are never rewritten by ensure_complete_fp.
FP_PROFILES: tuple[dict[str, str], ...] = (
    {
        "major": "120",
        "impersonate": "chrome120",
        "platform": "Windows",
        "platform_version": "10.0.0",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
    },
    {
        "major": "124",
        "impersonate": "chrome124",
        "platform": "Windows",
        "platform_version": "10.0.0",
        "accept-language": "en-US,en;q=0.9",
    },
    {
        "major": "131",
        "impersonate": "chrome131",
        "platform": "Windows",
        "platform_version": "15.0.0",
        "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    {
        "major": "120",
        "impersonate": "chrome120",
        "platform": "macOS",
        "platform_version": "14.0.0",
        "accept-language": "en-US,en;q=0.9",
    },
    {
        "major": "124",
        "impersonate": "chrome124",
        "platform": "macOS",
        "platform_version": "14.5.0",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    },
    {
        "major": "131",
        "impersonate": "chrome131",
        "platform": "macOS",
        "platform_version": "15.0.0",
        "accept-language": "en-GB,en;q=0.9,en-US;q=0.8",
    },
)

_REQUIRED_FP_KEYS = (
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
)


def _seed_digest(seed: str) -> int:
    raw = str(seed or "").strip().encode("utf-8")
    if not raw:
        raw = uuid.uuid4().bytes
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


def pick_fp_profile(seed: str = "") -> dict[str, str]:
    """Pick a stable profile from the pool using seed hash."""
    idx = _seed_digest(seed) % len(FP_PROFILES)
    return dict(FP_PROFILES[idx])


def build_aligned_chrome_fp(
    *,
    major: str = DEFAULT_CHROME_MAJOR,
    impersonate: str = DEFAULT_IMPERSONATE,
    device_id: str = "",
    session_id: str = "",
    user_agent: str = "",
    platform: str = "Windows",
    platform_version: str = "10.0.0",
    accept_language: str = "",
) -> dict[str, str]:
    chrome_major = str(major or DEFAULT_CHROME_MAJOR).strip() or DEFAULT_CHROME_MAJOR
    full = f"{chrome_major}.0.0.0"
    plat = str(platform or "Windows").strip() or "Windows"
    plat_ver = str(platform_version or "10.0.0").strip() or "10.0.0"
    if plat.lower() in {"macos", "mac os", "mac"}:
        plat = "macOS"
        ua = str(user_agent or "").strip() or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full} Safari/537.36"
        )
        arch = '"arm"'
    else:
        plat = "Windows"
        ua = str(user_agent or "").strip() or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full} Safari/537.36"
        )
        arch = '"x86"'
    lang = str(accept_language or "").strip() or "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7"
    return {
        "user-agent": ua,
        "impersonate": str(impersonate or DEFAULT_IMPERSONATE).strip() or DEFAULT_IMPERSONATE,
        "oai-device-id": str(device_id or uuid.uuid4()).strip() or str(uuid.uuid4()),
        "oai-session-id": str(session_id or uuid.uuid4()).strip() or str(uuid.uuid4()),
        "accept-language": lang,
        "sec-ch-ua": f'"Google Chrome";v="{chrome_major}", "Not?A_Brand";v="8", "Chromium";v="{chrome_major}"',
        "sec-ch-ua-arch": arch,
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version": f'"{full}"',
        "sec-ch-ua-full-version-list": (
            f'"Chromium";v="{full}", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="{full}"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{plat}"',
        "sec-ch-ua-platform-version": f'"{plat_ver}"',
    }


def build_diversified_fp(
    seed: str = "",
    *,
    device_id: str = "",
    session_id: str = "",
) -> dict[str, str]:
    """Build a seed-stable diversified fingerprint for new accounts."""
    profile = pick_fp_profile(seed)
    return build_aligned_chrome_fp(
        major=profile["major"],
        impersonate=profile["impersonate"],
        platform=profile["platform"],
        platform_version=profile["platform_version"],
        accept_language=profile["accept-language"],
        device_id=device_id,
        session_id=session_id,
    )


def normalize_fp(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in raw.items() if str(v or "").strip()}


def _fp_seed_from_account(account: dict[str, Any], fp: dict[str, str]) -> str:
    for key in ("email", "access_token", "token_hash"):
        value = str(account.get(key) or "").strip()
        if value:
            return value
    for key in ("oai-device-id", "oai-session-id"):
        value = str(fp.get(key) or "").strip()
        if value:
            return value
    return ""


def ensure_complete_fp(account: dict[str, Any] | None) -> tuple[dict[str, str], bool]:
    """Return (fp, created_or_filled).

    Incomplete fps are filled from a diversified profile derived from account seed.
    Existing device/session ids are preserved; a complete fp is not rewritten.
    """
    account = account if isinstance(account, dict) else {}
    fp = normalize_fp(account.get("fp"))
    for key in _REQUIRED_FP_KEYS + ("accept-language",):
        value = str(account.get(key) or "").strip()
        if value:
            fp[key] = value
    before = dict(fp)
    was_incomplete = any(not str(fp.get(k) or "").strip() for k in _REQUIRED_FP_KEYS)
    seed = _fp_seed_from_account(account, fp)
    if was_incomplete:
        defaults = build_diversified_fp(
            seed,
            device_id=fp.get("oai-device-id", ""),
            session_id=fp.get("oai-session-id", ""),
        )
    else:
        # Complete fingerprint: only fill optional accept-language if missing.
        defaults = {
            "accept-language": str(fp.get("accept-language") or "").strip()
            or "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        }
    for key, value in defaults.items():
        fp.setdefault(key, value)
    # Fix known mismatch: Edge UA + chrome impersonate
    impersonate = str(fp.get("impersonate") or "").lower()
    ua = str(fp.get("user-agent") or "")
    if "edg/" in ua.lower() and impersonate.startswith("chrome"):
        fixed = build_diversified_fp(
            seed or fp.get("oai-device-id", ""),
            device_id=fp.get("oai-device-id", ""),
            session_id=fp.get("oai-session-id", ""),
        )
        fp.update(fixed)
    legacy_arch = str(fp.get("sec-ch-ua-arch") or "").strip().strip('"').lower()
    if legacy_arch in {"x86_64", "amd64"}:
        fp["sec-ch-ua-arch"] = '"x86"'
    filled = fp != before
    return fp, filled
