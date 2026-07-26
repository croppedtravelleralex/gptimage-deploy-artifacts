#!/usr/bin/env python3
"""Deploy log display fix: merged call log + logs UI."""
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
BACKUP = f"{REMOTE_DIR}/backups/log-display-fix-{STAMP}"


def run(cmd: list[str], *, timeout: float = 600, cwd: str | None = None) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace", cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode} cmd={' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc.stdout or ""


def remote(cmd: str, *, timeout: float = 300) -> str:
    return run(["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd], timeout=timeout)


def main() -> int:
    remote(f"mkdir -p {BACKUP}/services && cp -a {REMOTE_DIR}/services/image_task_service.py {BACKUP}/services/ && tar -czf {BACKUP}/web_dist.tgz -C {REMOTE_DIR} web_dist")
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    run([npm, "run", "build"], cwd=str(ROOT / "web"), timeout=600)
    archive = ROOT / f"web_dist-deploy-{STAMP}.tgz"
    run(["tar", "-czf", str(archive), "-C", str(ROOT), "web_dist"], timeout=300)
    run(["scp", str(ROOT / "services/image_task_service.py"), f"{REMOTE}:{REMOTE_DIR}/services/image_task_service.py"], timeout=120)
    run(["scp", str(archive), f"{REMOTE}:/tmp/web_dist-deploy-{STAMP}.tgz"], timeout=300)
    remote(
        f"tar -xzf /tmp/web_dist-deploy-{STAMP}.tgz -C {REMOTE_DIR} && "
        f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d --force-recreate",
        timeout=180,
    )
    time.sleep(8)
    health = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    if not health.get("healthy"):
        raise RuntimeError(f"unhealthy: {health}")
    print(json.dumps({"healthy": True, "backup": BACKUP}, ensure_ascii=False))
    archive.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
