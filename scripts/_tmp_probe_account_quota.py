#!/usr/bin/env python3
"""Probe account local cache + remote image quota."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import os

if str(os.environ.get("GPTIMAGE_ROOT") or "").strip():
    ROOT = Path(os.environ["GPTIMAGE_ROOT"]).resolve()
sys.path.insert(0, str(ROOT))

from services.account_service import account_service  # noqa: E402
from services.openai_backend_api import OpenAIBackendAPI  # noqa: E402


def main() -> None:
    email = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    db = Path(os.environ.get("ACCOUNTS_DB") or ROOT / "data" / "accounts.db")
    con = sqlite3.connect(str(db))
    for tok, raw in con.execute("select access_token, data from accounts"):
        d = json.loads(raw or "{}")
        if str(d.get("email") or "").lower() != email:
            continue
        local = {
            "email": d.get("email"),
            "status": d.get("status"),
            "quota": d.get("quota"),
            "image_quota_unknown": d.get("image_quota_unknown"),
            "restore_at": d.get("restore_at"),
            "unlimited": d.get("unlimited"),
            "proxy_egress_ip": d.get("proxy_egress_ip"),
            "last_quota_refresh_at": d.get("last_quota_refresh_at"),
            "last_refresh_error": d.get("last_refresh_error"),
            "schedulable": d.get("schedulable"),
        }
        print("LOCAL", json.dumps(local, ensure_ascii=False))
        try:
            info = account_service.fetch_remote_info(tok, "probe_account_quota")
            print("REMOTE", json.dumps(info, ensure_ascii=False)[:800])
        except Exception as exc:
            print("REMOTE_ERROR", str(exc)[:400])
        try:
            api = OpenAIBackendAPI(access_token=tok)
            me = api.session.get(
                api.base_url + "/backend-api/me",
                headers=api._headers("/backend-api/me"),
                timeout=20,
            )
            print("ME", me.status_code, (me.text or "")[:160])
            api.close()
        except Exception as exc:
            print("ME_ERROR", str(exc)[:400])
        return
    raise SystemExit(f"not found: {email}")


if __name__ == "__main__":
    main()
