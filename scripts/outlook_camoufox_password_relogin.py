#!/usr/bin/env python3
"""Outlook 密码重登（Camoufox）+ YuMail OTP → 按邮箱替换 AT/RT。

用法:
  python scripts/outlook_camoufox_password_relogin.py --email x@outlook.com [--password ...] [--proxy ...]
  OUTLOOK_RECOVERY_BACKEND=camoufox 时由 OutlookAccountRecoveryService 调用。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _mask_email(value: object) -> str:
    email = str(value or "").strip().lower()
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 3:
        return f"***@{domain}"
    return f"{local[:2]}***{local[-1]}@{domain}"


def _emit(stage: str, **extra: Any) -> None:
    payload = {"event": "recovery_progress", "stage": stage, **extra}
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _load_proxy(proxy: str, proxy_file: Path | None, account_proxy: str) -> str:
    if str(proxy or "").strip():
        return str(proxy).strip()
    if account_proxy:
        return account_proxy
    if proxy_file and proxy_file.is_file():
        for line in proxy_file.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if "://" not in line and line.count(":") == 3:
                    host, port, user, pwd = line.split(":", 3)
                    return f"http://{user}:{pwd}@{host}:{port}"
                return line
    return ""


def _replace_account_tokens(
    account_service: Any,
    *,
    email: str,
    old_token: str,
    login_result: dict[str, Any],
    proxy: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    new_token = str(login_result.get("access_token") or "").strip()
    if not new_token:
        raise RuntimeError("login response missing access_token")
    staged = {
        "email": email,
        "password": str(login_result.get("password") or ""),
        "access_token": new_token,
        "refresh_token": str(login_result.get("refresh_token") or "").strip(),
        "id_token": str(login_result.get("id_token") or "").strip(),
        "chatgpt_session_token": str(login_result.get("chatgpt_session_token") or "").strip(),
        "chatgpt_session_expires": login_result.get("chatgpt_session_expires"),
        "expires_at": login_result.get("expires_at"),
        "source_type": "web",
        "source_detail": "outlook_camoufox_password_relogin",
        "created_at": now,
        "updated_at": now,
        "proxy": proxy,
        "status": "异常",
        "quota": 0,
        "image_quota_unknown": False,
        "panda_receive_state": "incoming",
        "invalid_count": 0,
        "last_refresh_error": None,
        "last_token_refresh_error": None,
        "outlook_recovery_last_error": None,
    }
    # 保留号池原密码
    old = account_service.get_account(old_token) if old_token else None
    if isinstance(old, dict) and old.get("password"):
        staged["password"] = old.get("password")

    account_service.add_account_items([staged], include_items=False)
    refresh = account_service.refresh_accounts([new_token], defer_invalid_removal=True, include_items=False)
    if refresh.get("errors") or int(refresh.get("refreshed") or 0) <= 0:
        raise RuntimeError(f"panda_webshare_verify_failed: {refresh.get('errors')}")
    resolved = account_service.resolve_access_token(new_token) or new_token
    account_service.update_account(
        resolved,
        {
            "status": "正常",
            "panda_receive_state": "verified_ready",
            "last_refresh_error": None,
            "last_token_refresh_error": None,
            "outlook_recovery_last_error": None,
            "updated_at": now,
            "proxy": proxy or (old.get("proxy") if isinstance(old, dict) else ""),
        },
        quiet=True,
    )
    if old_token and resolved != old_token:
        account_service.delete_accounts([old_token], include_items=False)
    final = account_service.get_account(resolved) or {}
    return {
        "ok": True,
        "quota": int(final.get("quota") or 0),
        "status": str(final.get("status") or ""),
        "schedulable": True,
        "old_removed": True,
        "email": _mask_email(email),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Outlook Camoufox password relogin with YuMail OTP")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", default="")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--proxy-file", default="")
    parser.add_argument("--old-access-token", default="")
    parser.add_argument("--report-dir", default="")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    sys.path.insert(0, str(root))
    from services.account_service import account_service
    from services import yumail_otp
    import scripts.yumail_camoufox_openai_register as cam

    email = str(args.email).strip().lower()
    account_service.reload_from_storage()
    account = None
    old_token = str(args.old_access_token or "").strip()
    if old_token:
        account = account_service.get_account(old_token)
    if account is None:
        for item in account_service.list_accounts():
            if str(item.get("email") or "").strip().lower() == email:
                account = item
                old_token = str(item.get("access_token") or "").strip()
                break
    if not isinstance(account, dict):
        _emit("failed", ok=False, error="account_not_found", email=_mask_email(email))
        return 1

    password = str(args.password or account.get("password") or "").strip()
    if not password:
        _emit("failed", ok=False, error="need_openai_password", email=_mask_email(email))
        return 1
    if not yumail_otp.is_configured():
        _emit("failed", ok=False, error="yumail_not_configured", email=_mask_email(email))
        return 1
    probe = yumail_otp.probe_reachable()
    if not probe.get("ok"):
        _emit("failed", ok=False, error=f"yumail_unreachable: {probe.get('error')}", email=_mask_email(email))
        return 1

    proxy_file = Path(args.proxy_file).expanduser() if args.proxy_file else None
    proxy = _load_proxy(args.proxy, proxy_file, str(account.get("proxy") or ""))
    report_dir = Path(args.report_dir).expanduser().resolve() if args.report_dir else root / "data" / "runlogs" / f"camoufox-relogin-{int(time.time())}"
    report_dir.mkdir(parents=True, exist_ok=True)

    _emit("login", email=_mask_email(email), proxy=urlsplit(proxy).hostname if proxy else "")
    # 复用 Camoufox --relogin 主路径：把 argv 模拟进去
    old_argv = sys.argv[:]
    try:
        sys.argv = [
            "yumail_camoufox_openai_register.py",
            "--relogin",
            email,
            password,
        ]
        if proxy:
            sys.argv.append(proxy)
        # 注入 YuMail API base into mail config via env
        import os

        os.environ.setdefault("YUMAIL_API_BASE", yumail_otp.resolve_api_base())
        rc = cam.main()
    finally:
        sys.argv = old_argv

    if rc != 0:
        _emit("failed", ok=False, error="camoufox_relogin_failed", email=_mask_email(email))
        (report_dir / "summary.json").write_text(
            json.dumps({"restored": 0, "failed": 1, "ok": False}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 1

    # Camoufox 脚本默认 add_account_items；这里再按邮箱收敛：刷额度并删旧行
    _emit("commit", email=_mask_email(email))
    account_service.reload_from_storage()
    # 找最新同邮箱账号
    newest = None
    for item in account_service.list_accounts():
        if str(item.get("email") or "").strip().lower() != email:
            continue
        if newest is None or str(item.get("updated_at") or "") >= str(newest.get("updated_at") or ""):
            newest = item
    if not isinstance(newest, dict):
        _emit("failed", ok=False, error="relogin_account_missing_after_camoufox")
        return 1

    result = _replace_account_tokens(
        account_service,
        email=email,
        old_token=old_token,
        login_result={
            "access_token": newest.get("access_token"),
            "refresh_token": newest.get("refresh_token"),
            "id_token": newest.get("id_token"),
            "chatgpt_session_token": newest.get("chatgpt_session_token"),
            "chatgpt_session_expires": newest.get("chatgpt_session_expires"),
            "expires_at": newest.get("expires_at"),
            "password": password,
        },
        proxy=proxy or str(newest.get("proxy") or ""),
    )
    _emit("done", ok=True, **{k: v for k, v in result.items() if k != "ok"})
    (report_dir / "summary.json").write_text(
        json.dumps({"restored": 1, "failed": 0, **result}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (report_dir / "rows.json").write_text(json.dumps([result], ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
