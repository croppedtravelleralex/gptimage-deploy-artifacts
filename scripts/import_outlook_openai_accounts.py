from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services.account_service import account_service
from services.outlook_mail import parse_outlook_credentials, wait_for_code


def _mask_email(email: str) -> str:
    local, sep, domain = str(email or "").partition("@")
    if not sep:
        return "***"
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-1]}@{domain}"


def _safe_error(value: Any, limit: int = 240) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    for marker in ("access_token", "refresh_token", "id_token", "password"):
        text = text.replace(marker, f"{marker[:2]}***")
    return text[:limit]


def _mail_config_for(credential: dict[str, str], *, wait_timeout: float, wait_interval: float) -> dict[str, Any]:
    return {
        "request_timeout": 30,
        "wait_timeout": wait_timeout,
        "wait_interval": wait_interval,
        "user_agent": account_service._OAUTH_USER_AGENT,
        "providers": [
            {
                "type": "outlook_token",
                "enable": True,
                "label": "OutlookExisting",
                "mode": "auto",
                "message_limit": 10,
                "mailboxes": [credential],
            }
        ],
    }


def _mailbox_for(credential: dict[str, str], boundary: datetime) -> dict[str, Any]:
    return {
        "provider": "outlook_token",
        "provider_ref": "outlook_token#1",
        "address": credential["email"],
        "label": "OutlookExisting",
        "client_id": credential["client_id"],
        "refresh_token": credential["refresh_token"],
        "_code_not_before": boundary,
    }


def login_one(
    credential: dict[str, str],
    *,
    line: int,
    dry_run: bool,
    verify_refresh: bool,
    wait_timeout: float,
    wait_interval: float,
) -> dict[str, Any]:
    email = str(credential.get("email") or "").strip()
    started = time.monotonic()
    masked_email = _mask_email(email)
    boundary = datetime.now(timezone.utc)
    mail_config = _mail_config_for(credential, wait_timeout=wait_timeout, wait_interval=wait_interval)
    mailbox = _mailbox_for(credential, boundary)

    def resolve_otp() -> str | None:
        return wait_for_code(mail_config, mailbox)

    result = account_service._login_with_password(
        email,
        str(credential.get("password") or ""),
        otp_resolver=resolve_otp,
    )
    elapsed = round(time.monotonic() - started, 2)
    base = {"line": line, "email": masked_email, "elapsed_sec": elapsed}
    if not result.get("ok"):
        return {**base, "ok": False, "error": _safe_error(result.get("error") or "login_failed")}

    item = {
        "email": result.get("email") or email,
        "password": credential.get("password") or "",
        "access_token": result.get("access_token") or "",
        "refresh_token": result.get("refresh_token") or "",
        "id_token": result.get("id_token") or "",
        "account_id": result.get("account_id") or "",
        "expires_at": result.get("expires_at"),
        "source_type": "web",
        "source_detail": "outlook_existing_login",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if not item["access_token"] or not item["refresh_token"]:
        return {**base, "ok": False, "error": "login_succeeded_but_missing_token"}

    import_result: dict[str, Any] = {}
    refresh_result: dict[str, Any] = {}
    if not dry_run:
        import_result = account_service.add_account_items([item], include_items=False)
        if verify_refresh:
            refresh_result = account_service.refresh_accounts([item["access_token"]], defer_invalid_removal=False, include_items=False)
            if int(refresh_result.get("refreshed") or 0) > 0 and not refresh_result.get("errors"):
                now = datetime.now(timezone.utc).isoformat()
                account_service.update_account(
                    item["access_token"],
                    {
                        "panda_sync_state": "ready",
                        "panda_ready_at": now,
                        "panda_probe_last_at": now,
                        "panda_probe_last_error": None,
                        "panda_receive_state": "local_verified",
                        "panda_verified_at": now,
                        "panda_verify_last_error": None,
                    },
                    quiet=True,
                )

    return {
        **base,
        "ok": True,
        "imported": not dry_run,
        "local_added": int(import_result.get("added") or 0),
        "local_updated": int(import_result.get("updated") or 0),
        "local_skipped": int(import_result.get("skipped") or 0),
        "refresh_verified": int(refresh_result.get("refreshed") or 0) if refresh_result else None,
        "refresh_errors": len(refresh_result.get("errors") or []) if refresh_result else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Login existing OpenAI accounts from OutlookToken mailbox file and optionally import into local pool.")
    parser.add_argument("--file", required=True, help="Path to email----password----client_id----refresh_token file.")
    parser.add_argument("--limit", type=int, default=0, help="Max accounts to process. 0 means all.")
    parser.add_argument("--start-line", type=int, default=1, help="1-based parsed credential offset.")
    parser.add_argument("--import-local", action="store_true", help="Persist successful accounts into local accounts.db.")
    parser.add_argument("--no-verify-refresh", action="store_true", help="Do not refresh/verify after local import.")
    parser.add_argument("--wait-timeout", type=float, default=90, help="OTP wait timeout seconds.")
    parser.add_argument("--wait-interval", type=float, default=2, help="OTP polling interval seconds.")
    parser.add_argument("--report", default="", help="Optional non-secret JSON report path.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    path = Path(args.file)
    if not path.exists():
        print(f"file_not_found: {path}", file=sys.stderr)
        return 2

    credentials = parse_outlook_credentials(path.read_text(encoding="utf-8-sig"))
    start = max(1, int(args.start_line or 1))
    selected = credentials[start - 1 :]
    if args.limit and args.limit > 0:
        selected = selected[: args.limit]
    if not selected:
        print("no_credentials_selected", file=sys.stderr)
        return 2

    dry_run = not bool(args.import_local)
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    print(json.dumps({"parsed": len(credentials), "selected": len(selected), "dry_run": dry_run}, ensure_ascii=False))
    for offset, credential in enumerate(selected, start=start):
        try:
            row = login_one(
                credential,
                line=offset,
                dry_run=dry_run,
                verify_refresh=not bool(args.no_verify_refresh),
                wait_timeout=max(5.0, float(args.wait_timeout or 90)),
                wait_interval=max(0.5, float(args.wait_interval or 2)),
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            email = str(credential.get("email") or "")
            row = {"line": offset, "email": _mask_email(email), "ok": False, "error": _safe_error(f"{type(exc).__name__}: {exc}")}
        rows.append(row)
        counts["ok" if row.get("ok") else str(row.get("error") or "failed")] += 1
        print(json.dumps(row, ensure_ascii=False))

    summary = {"summary": dict(counts), "ok": int(counts.get("ok") or 0), "failed": len(rows) - int(counts.get("ok") or 0)}
    print(json.dumps(summary, ensure_ascii=False))
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({"rows": rows, **summary}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["ok"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
