#!/usr/bin/env python3
"""每半小时探测指定「信号」账号是否仍存活（RT refresh + get_user_info）。

默认目标：mthomas4jl6@yumail.co（yumail Camoufox 注册信号号）。
出口强制走账号绑定 sticky proxy（account > explicit > runtime）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_EMAIL = "mthomas4jl6@yumail.co"
LOG_DIR = ROOT / "data" / "runlogs"
LAST_PATH = LOG_DIR / "signal_account_probe_last.json"
JSONL_PATH = LOG_DIR / "signal_account_probe.jsonl"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_account(email: str) -> dict[str, Any] | None:
    from services.account_service import account_service

    target = email.strip().lower()
    for account in account_service.list_accounts():
        if str(account.get("email") or "").strip().lower() == target:
            return dict(account)
    return None


def _proxy_profile(account: dict[str, Any]) -> dict[str, str]:
    from services.proxy_service import proxy_settings

    profile = proxy_settings.get_profile(account=account, upstream=True)
    return {
        "proxy_url": str(profile.proxy_url or ""),
        "proxy_source": str(profile.proxy_source or ""),
    }


def probe_once(email: str) -> dict[str, Any]:
    from services.account_service import AccountService, account_service
    from services.proxy_service import proxy_settings

    started = _iso_now()
    account = _find_account(email)
    if not account:
        return {
            "ok": False,
            "alive": False,
            "email": email,
            "error": "account_not_found",
            "probed_at": started,
        }

    token = str(account.get("access_token") or "").strip()
    proxy_info = _proxy_profile(account)
    # 再确认 session 出口与账号字段一致
    session_kwargs = proxy_settings.build_session_kwargs(account=account, upstream=True)
    session_proxy = ""
    if isinstance(session_kwargs, dict):
        session_proxy = str(session_kwargs.get("proxy") or session_kwargs.get("proxies") or "")
    result: dict[str, Any] = {
        "ok": False,
        "alive": False,
        "email": email,
        "status_before": account.get("status"),
        "account_proxy": str(account.get("proxy") or ""),
        "proxy_provider": str(account.get("proxy_provider") or ""),
        "proxy_scope": str(account.get("proxy_scope") or ""),
        "resolved_proxy_url": proxy_info["proxy_url"],
        "resolved_proxy_source": proxy_info["proxy_source"],
        "session_proxy": session_proxy,
        "has_rt": bool(str(account.get("refresh_token") or "").strip()),
        "probed_at": started,
        "event": "signal_account_probe",
    }

    if not token:
        result["error"] = "access_token_missing"
        return result
    if not result["has_rt"]:
        result["error"] = "refresh_token_missing"
        return result

    try:
        after = account_service.fetch_remote_info(
            token,
            "signal_account_probe",
            defer_invalid_removal=True,
        )
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
        # 失败也写回探测时间，便于排障
        try:
            account_service.update_account(
                token,
                {
                    "signal_probe_last_at": started,
                    "signal_probe_last_error": result["error"],
                    "signal_probe_last_alive": False,
                },
                quiet=True,
            )
        except Exception:
            pass
        return result

    if not after:
        result["error"] = "fetch_remote_info_empty"
        return result

    alive = AccountService._is_image_account_available(after) and str(after.get("status") or "") != "禁用"
    refresh_err = str(after.get("last_token_refresh_error") or "").strip()
    result.update(
        {
            "ok": True,
            "alive": bool(alive),
            "status": after.get("status"),
            "quota": after.get("quota"),
            "invalid": after.get("invalid"),
            "last_token_refresh_error": refresh_err or None,
            "access_token_rotated": str(after.get("access_token") or "") != token,
        }
    )
    if not alive and not result.get("error"):
        result["error"] = refresh_err or "not_available_after_probe"

    try:
        account_service.update_account(
            str(after.get("access_token") or token),
            {
                "signal_probe_last_at": started,
                "signal_probe_last_error": None if alive else (result.get("error") or "dead"),
                "signal_probe_last_alive": bool(alive),
            },
            quiet=True,
        )
    except Exception:
        pass
    return result


def _persist(result: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAST_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with JSONL_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe yumail Camoufox signal account liveness")
    parser.add_argument("--email", default=DEFAULT_EMAIL, help="signal account email")
    args = parser.parse_args()

    result = probe_once(args.email.strip())
    _persist(result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result.get("alive"):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
