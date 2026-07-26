#!/usr/bin/env python3
"""Import a Camoufox observe blob onto Panda as identity_isolated."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def backup_db(data_dir: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    src = data_dir / "accounts.db"
    if not src.is_file():
        raise FileNotFoundError(f"accounts.db missing: {src}")
    with sqlite3.connect(src) as source, sqlite3.connect(backup_dir / "accounts.db") as target:
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
    for item in account_service.list_accounts():
        if str(item.get("email") or "").strip().lower() == email:
            raise SystemExit(f"account already exists: {email}")

    backup_db(root / "data", backup_dir)
    now = utc_now()
    from services.account_service import AccountService

    grace_sec = AccountService.observe_import_refresh_grace_seconds()
    refresh_after = (datetime.now(timezone.utc) + timedelta(seconds=grace_sec)).isoformat()
    staged = dict(blob)
    staged.setdefault("status", "正常")
    staged.setdefault("quota", 0)
    staged["panda_receive_state"] = "identity_isolated"
    staged["panda_sync_state"] = "ready"
    staged["panda_imported_at"] = now
    staged["panda_observe_refresh_after"] = refresh_after
    staged["updated_at"] = now
    staged["invalid_count"] = 0
    staged["last_refresh_error"] = None
    staged["last_token_refresh_error"] = None
    staged["last_quota_refresh_error"] = None
    staged["panda_probe_last_error"] = None
    staged["panda_verify_last_error"] = None

    account_service.add_account_items([staged], include_items=False)
    token = str(staged.get("access_token") or "").strip()
    if not token:
        raise SystemExit("blob missing access_token")

    from services.openai_backend_api import OpenAIBackendAPI

    resolved = account_service.resolve_access_token(token) or token
    me_payload: dict[str, Any] = {}
    try:
        api = OpenAIBackendAPI(resolved)
        try:
            me_payload = api._get_me()  # noqa: SLF001 — observe import only needs token validity
        finally:
            close = getattr(api, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        account_service.delete_accounts([token], include_items=False)
        raise SystemExit(f"panda /me verify failed: {exc}") from exc

    verified_email = str(me_payload.get("email") or email).strip().lower()
    if verified_email != email:
        account_service.delete_accounts([token], include_items=False)
        raise SystemExit(f"verified email mismatch: {verified_email} != {email}")

    account_service.update_account_identity(
        resolved,
        {
            "email": verified_email,
            "user_id": me_payload.get("id"),
            "panda_receive_state": "identity_isolated",
            "panda_sync_state": "ready",
            "panda_observe_refresh_after": refresh_after,
            "status": str(staged.get("status") or "正常"),
            "quota": int(staged.get("quota") or 0),
            "panda_probe_last_error": None,
            "panda_verify_last_error": None,
            "last_refresh_error": None,
            "updated_at": utc_now(),
        },
        reason="outlook_camoufox_observe_import",
        quiet=True,
        clear_isolation=False,
    )

    final = account_service.get_account(resolved) or {}
    summary = {
        "ok": True,
        "email": email,
        "token_hash": str(resolved)[:12],
        "quota": int(final.get("quota") or 0),
        "status": str(final.get("status") or ""),
        "panda_receive_state": str(final.get("panda_receive_state") or ""),
        "panda_observe_refresh_after": str(final.get("panda_observe_refresh_after") or ""),
        "proxy": (str(final.get("proxy") or "").split("@")[-1] or "")[:40],
        "backup_dir": str(backup_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    reload_runtime_api()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
