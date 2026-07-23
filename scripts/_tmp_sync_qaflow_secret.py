#!/usr/bin/env python3
"""Sync qaflow secret (token+webshare) from panda into local gitignored file."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FETCH = ROOT / "scripts" / "_tmp_fetch_qaflow_proxy.py"
SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"


def main() -> int:
    p = subprocess.run(["ssh", "panda", "python3", "-"], input=FETCH.read_bytes(), capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode("utf-8", errors="replace")[:800])
        return 1
    raw = p.stdout.decode("utf-8", errors="replace")
    lines = [ln for ln in raw.splitlines() if ln.strip().startswith("{") and '"ok"' in ln]
    if not lines:
        print(json.dumps({"ok": False, "error": "no_json", "stdout_tail": raw[-400:]}))
        return 1
    data = json.loads(lines[-1])
    old = json.loads(SECRET.read_text(encoding="utf-8")) if SECRET.exists() else {}
    sec = {
        "email": data.get("email") or old.get("email"),
        "access_token": data.get("access_token") or old.get("access_token"),
        "refresh_token": data.get("refresh_token") or old.get("refresh_token"),
        "chatgpt_session_token": old.get("chatgpt_session_token")
        or data.get("chatgpt_session_token")
        or "",
        "fp": data.get("fp") or old.get("fp") or {},
        "proxy": data.get("proxy") or "",
        "proxy_provider": data.get("proxy_provider") or "webshare",
        "openai_password": old.get("openai_password", ""),
        "proton_password": old.get("proton_password", ""),
    }
    SECRET.parent.mkdir(parents=True, exist_ok=True)
    SECRET.write_text(json.dumps(sec, indent=2), encoding="utf-8")
    proxy = sec["proxy"]
    host = proxy.rsplit("@", 1)[-1] if "@" in proxy else ""
    print(
        json.dumps(
            {
                "ok": True,
                "email": sec["email"],
                "has_at": bool(sec["access_token"]),
                "has_proxy": bool(proxy),
                "proxy_host": host,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
