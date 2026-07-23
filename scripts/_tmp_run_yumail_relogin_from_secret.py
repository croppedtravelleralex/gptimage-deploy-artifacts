#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    secret_path = Path(sys.argv[1]).resolve()
    browser_proxy = str(sys.argv[2] if len(sys.argv) > 2 else "").strip()
    payload = json.loads(secret_path.read_text(encoding="utf-8"))
    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "").strip()
    proxy = str(payload.get("proxy") or "").strip()
    if not email or not password or not proxy:
        raise SystemExit("secret missing email/password/proxy")

    os.environ.setdefault("YUMAIL_API_BASE", "http://127.0.0.1:8782/api/v1")
    os.environ.setdefault(
        "YUMAIL_API_KEY_FILE",
        str(ROOT / "data" / "runlogs" / "yumail_api_key.panda.secret.txt"),
    )

    argv = [
        sys.executable,
        str(ROOT / "scripts" / "yumail_camoufox_openai_register.py"),
        "--relogin",
        email,
        password,
        proxy,
    ]
    if browser_proxy:
        # yumail script only accepts one proxy today; prefer browser chain when provided.
        argv[-1] = browser_proxy
    return subprocess.call(argv, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
