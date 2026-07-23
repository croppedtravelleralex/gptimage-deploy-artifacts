#!/usr/bin/env python3
"""Import a local Camoufox recovery blob onto Panda and replace the old token."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def backup_db(data_dir: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    src = data_dir / "accounts.db"
    dst = backup_dir / "accounts.db"
    if not src.is_file():
        raise FileNotFoundError(f"accounts.db missing: {src}")
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
        source.backup(target)


def reload_runtime_api() -> None:
    for cfg_path in (Path("/root/gptimage/config.json"), Path("/app/config.json")):
        if not cfg_path.is_file():
            continue
        auth = str(json.loads(cfg_path.read_text(encoding="utf-8")).get("auth-key") or "").strip()
        if not auth:
            continue
        req = urllib.request.Request(
            "http://127.0.0.1:8012/api/accounts/reload-from-storage",
            data=b"{}",
            method="POST",
            headers={"Authorization": f"Bearer {auth}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                print(json.dumps({"reload_status": resp.status}, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"reload_status": "skipped", "error": str(exc)[:160]}, ensure_ascii=False))
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/app")
    parser.add_argument("--blob", required=True)
    parser.add_argument("--old-token-hash", default="", help="optional 12-char hash prefix for audit only")
    parser.add_argument("--backup-dir", required=True)
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    blob_path = Path(args.blob).resolve()
    backup_dir = Path(args.backup_dir).resolve()
    sys.path.insert(0, str(root))

    blob = json.loads(blob_path.read_text(encoding="utf-8"))
    email = str(blob.get("email") or "").strip().lower()
    if not email:
        raise SystemExit("blob missing email")

    from services.account_service import account_service

    account_service.reload_from_storage()
    old_token = ""
    for item in account_service.list_accounts():
        if str(item.get("email") or "").strip().lower() == email:
            old_token = str(item.get("access_token") or "").strip()
            break
    if not old_token:
        raise SystemExit(f"old account not found for {email}")

    backup_db(root / "data", backup_dir)

    now = utc_now()
    staged = dict(blob)
    staged.setdefault("status", "异常")
    staged.setdefault("quota", 0)
    staged["panda_receive_state"] = "incoming"
    staged["panda_sync_state"] = "incoming"
    staged["panda_imported_at"] = now
    staged["updated_at"] = now
    staged["invalid_count"] = 0
    staged["last_refresh_error"] = None
    staged["last_token_refresh_error"] = None
    staged["outlook_recovery_last_error"] = None

    account_service.add_account_items([staged], include_items=False)
    new_token = str(staged.get("access_token") or "").strip()
    if not new_token:
        raise SystemExit("blob missing access_token")

    refresh = account_service.refresh_accounts([new_token], defer_invalid_removal=True, include_items=False)
    if refresh.get("errors") or int(refresh.get("refreshed") or 0) <= 0:
        account_service.delete_accounts([new_token], include_items=False)
        raise SystemExit(f"panda verify failed: {refresh.get('errors')}")

    resolved = account_service.resolve_access_token(new_token) or new_token
    verified = account_service.get_account(resolved) or {}
    if str(verified.get("email") or "").strip().lower() != email:
        account_service.delete_accounts([new_token], include_items=False)
        raise SystemExit("verified email mismatch")

    account_service.update_account(
        resolved,
        {
            "status": "正常",
            "panda_receive_state": "verified_ready",
            "panda_sync_state": "ready",
            "panda_verified_at": now,
            "panda_probe_last_error": None,
            "panda_verify_last_error": None,
            "last_refresh_error": None,
            "last_refresh_error_at": None,
            "outlook_recovery_last_error": None,
            "updated_at": now,
        },
        quiet=True,
    )

    removed = 0
    if resolved != old_token:
        delete_result = account_service.delete_accounts([old_token], include_items=False)
        removed = int(delete_result.get("removed") or 0)
        if removed <= 0 and account_service.get_account(old_token) is not None:
            account_service.delete_accounts([resolved], include_items=False)
            raise SystemExit("old token removal failed; rolled back new token")

    final = account_service.get_account(resolved) or {}
    summary = {
        "ok": True,
        "email": email,
        "old_token_hash": args.old_token_hash,
        "new_token_hash": resolved[:12],
        "quota": int(final.get("quota") or 0),
        "status": str(final.get("status") or ""),
        "panda_receive_state": str(final.get("panda_receive_state") or ""),
        "old_removed": bool(removed or account_service.get_account(old_token) is None),
        "backup_dir": str(backup_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    reload_runtime_api()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
