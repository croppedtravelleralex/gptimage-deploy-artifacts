#!/usr/bin/env python3
"""Probe account: /me + optional SPA image start under sticky proxy."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path("/root/gptimage")
sys.path.insert(0, str(ROOT))

from curl_cffi import requests as crequests  # noqa: E402


def load_account(email: str) -> tuple[dict, str]:
    con = sqlite3.connect("/root/gptimage/data/accounts.db")
    con.row_factory = sqlite3.Row
    email = email.strip().lower()
    for r in con.execute("select access_token, data from accounts"):
        d = json.loads(r["data"] or "{}")
        if str(d.get("email") or "").strip().lower() == email:
            return d, str(r["access_token"] or "")
    raise SystemExit(f"not found: {email}")


def proxy_dict(proxy: str) -> dict:
    p = (proxy or "").strip()
    if not p:
        return {}
    return {"http": p, "https": p}


def probe_me(token: str, proxy: str) -> dict:
    s = crequests.Session(impersonate="chrome131", proxies=proxy_dict(proxy), timeout=30)
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json",
        "oai-language": "en-US",
    }
    t0 = time.time()
    try:
        r = s.get("https://chatgpt.com/backend-api/me", headers=headers)
        body = (r.text or "")[:200]
        return {"http": r.status_code, "ms": int((time.time() - t0) * 1000), "body": body}
    except Exception as e:
        return {"http": None, "ms": int((time.time() - t0) * 1000), "error": str(e)[:240]}


def main() -> None:
    email = sys.argv[1] if len(sys.argv) > 1 else "ivetterock54353@outlook.com"
    d, token = load_account(email)
    proxy = str(d.get("proxy") or "").strip()
    print(json.dumps({
        "email": email,
        "status": d.get("status"),
        "quota": d.get("quota"),
        "last_refresh_error": d.get("last_refresh_error"),
        "last_token_refresh_error": str(d.get("last_token_refresh_error") or "")[:200],
        "proxy_egress_ip": d.get("proxy_egress_ip"),
        "has_password": bool(d.get("password")),
        "has_refresh_token": bool(d.get("refresh_token")),
        "token_len": len(token),
    }, ensure_ascii=False))
    print("ME", json.dumps(probe_me(token, proxy), ensure_ascii=False))


if __name__ == "__main__":
    main()
