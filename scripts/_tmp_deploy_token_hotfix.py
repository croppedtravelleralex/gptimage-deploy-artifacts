#!/usr/bin/env python3
"""Hotfix deploy conversation.py token NameError to Panda."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "panda"
REMOTE_DIR = "/root/gptimage"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
BACKUP = f"{REMOTE_DIR}/backups/token-hotfix-{STAMP}"
REL = "services/protocol/conversation.py"


def run(cmd: list[str], *, timeout: float = 300) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode} cmd={' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc.stdout or ""


def remote(cmd: str, *, timeout: float = 300) -> str:
    return run(["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd], timeout=timeout)


def main() -> int:
    remote(f"mkdir -p {BACKUP}/services/protocol && cp -a {REMOTE_DIR}/{REL} {BACKUP}/{REL}")
    run(["scp", str(ROOT / REL), f"{REMOTE}:{REMOTE_DIR}/{REL}"], timeout=120)
    remote(f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d --force-recreate", timeout=180)
    time.sleep(8)
    health = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    if not health.get("healthy"):
        raise RuntimeError(f"unhealthy: {health}")
    remote(
        "docker exec chatgpt2api-local /app/.venv/bin/python3 -c "
        "'import inspect; from services.protocol import conversation as c; "
        "src=inspect.getsource(c.stream_image_outputs); "
        "assert \"access_token = str(getattr(backend\" in src; print(\"hotfix_ok\")'"
    )
    print(json.dumps({"healthy": True, "backup": BACKUP}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"HOTFIX_FAILED: {exc}", file=sys.stderr)
        raise
