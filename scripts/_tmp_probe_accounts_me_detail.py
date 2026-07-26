#!/usr/bin/env python3
import json
import sqlite3
import sys
import time
from pathlib import Path

from curl_cffi import requests as crequests

DB = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/data/accounts.db")
EMAILS = [e.strip().lower() for e in (sys.argv[2:] if len(sys.argv) > 2 else [])]


def probe_me(token: str, proxy: str) -> dict:
    s = crequests.Session(impersonate="chrome131", proxies={"http": proxy, "https": proxy}, timeout=30)
    headers = {"authorization": f"Bearer {token}", "accept": "application/json", "oai-language": "en-US"}
    t0 = time.time()
    try:
        r = s.get("https://chatgpt.com/backend-api/me", headers=headers)
        body = r.text or ""
        payload = {}
        try:
            payload = r.json()
        except Exception:
            pass
        return {
            "http": r.status_code,
            "ms": int((time.time() - t0) * 1000),
            "body_head": body[:240],
            "email": payload.get("email"),
            "limits": payload.get("limits"),
        }
    except Exception as exc:
        return {"http": None, "ms": int((time.time() - t0) * 1000), "error": str(exc)[:240]}


def main() -> int:
    conn = sqlite3.connect(str(DB))
    targets = set(EMAILS)
    rows = []
    for token, raw in conn.execute("select access_token, data from accounts"):
        d = json.loads(raw or "{}")
        email = str(d.get("email") or "").strip().lower()
        if targets and email not in targets:
            continue
        proxy = str(d.get("proxy") or "")
        me = probe_me(str(token), proxy) if token and proxy else {"error": "missing token/proxy"}
        lp = d.get("limits_progress") or {}
        ig = ((lp.get("image_gen") or {}) if isinstance(lp, dict) else {})
        rows.append(
            {
                "email": email,
                "status": d.get("status"),
                "quota": d.get("quota"),
                "image_quota_unknown": d.get("image_quota_unknown"),
                "panda_receive_state": d.get("panda_receive_state"),
                "scheduling_enabled": d.get("scheduling_enabled"),
                "last_refresh_error": str(d.get("last_refresh_error") or "")[:160],
                "last_token_refresh_error": str(d.get("last_token_refresh_error") or "")[:160],
                "last_quota_refresh_error": str(d.get("last_quota_refresh_error") or "")[:160],
                "panda_imported_at": d.get("panda_imported_at"),
                "updated_at": d.get("updated_at"),
                "invalid_count": d.get("invalid_count"),
                "limits_progress_image_gen": ig,
                "proxy_host": proxy.split("@")[-1].split(":")[0] if proxy else "",
                "me": me,
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
