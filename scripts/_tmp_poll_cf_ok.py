#!/usr/bin/env python3
"""Poll Panda CF probe until N good proxies found."""
from __future__ import annotations

import json
import subprocess
import sys
import time

EXCLUDE = (
    "104.252.149.121,45.39.75.27,82.21.231.132,82.21.231.148,82.21.231.233,"
    "82.29.223.111,92.113.231.203,92.113.236.188,92.113.236.79,92.113.241.215,"
    "92.113.246.12,92.113.246.176"
)
CMD = (
    "docker exec chatgpt2api-local /app/.venv/bin/python "
    "/app/scripts/_tmp_probe_webshare_cf_ok.py "
    "--pool /app/data/runlogs/webshare_100_proxies.secret.txt "
    f"--exclude-hosts {EXCLUDE} --count 2 --workers 12"
)


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    for i in range(rounds):
        proc = subprocess.run(["ssh", "panda", CMD], capture_output=True, text=True, timeout=120)
        text = (proc.stdout or proc.stderr or "").strip()
        print(json.dumps({"round": i + 1, "exit": proc.returncode, "raw": text[:500]}, ensure_ascii=False), flush=True)
        try:
            payload = json.loads(proc.stdout)
        except Exception:
            payload = {}
        good = payload.get("good") or []
        if len(good) >= 2:
            print(json.dumps({"found": good[:2]}, ensure_ascii=False, indent=2))
            return 0
        if good:
            print(json.dumps({"partial": good}, ensure_ascii=False, indent=2))
        time.sleep(wait)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
