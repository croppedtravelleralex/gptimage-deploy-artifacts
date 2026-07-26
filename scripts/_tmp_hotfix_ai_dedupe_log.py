#!/usr/bin/env python3
"""Hotfix: deploy api/ai.py (skip LoggedCall duplicate success log)."""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
BACKUP = f"/root/gptimage/backups/image-observability-ai-{STAMP}"


def run(cmd: list[str], *, timeout: float = 180) -> str:
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout or ""


def remote(cmd: str, *, timeout: float = 180) -> str:
    return run(["ssh", "-o", "ConnectTimeout=20", "panda", cmd], timeout=timeout)


def main() -> int:
    print(f"[1] backup {BACKUP}")
    remote(f"mkdir -p {BACKUP}/api && cp -a /root/gptimage/api/ai.py {BACKUP}/api/ai.py")
    print("[2] scp api/ai.py")
    run(["scp", str(ROOT / "api" / "ai.py"), "panda:/root/gptimage/api/ai.py"])
    print("[3] recreate")
    remote("cd /root/gptimage && docker compose -f docker-compose.panda.yml up -d --force-recreate", timeout=180)
    time.sleep(10)
    health = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    print(json.dumps({"healthy": health.get("healthy"), "backup": BACKUP}, ensure_ascii=False, indent=2))
    if not health.get("healthy"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
