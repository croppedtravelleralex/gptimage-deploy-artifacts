#!/usr/bin/env python3
"""Deploy FE perf: route lazy-load + accounts deferred sections."""
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
BACKUP = f"{REMOTE_DIR}/backups/fe-perf-{STAMP}"


def run(cmd: list[str], *, timeout: float = 600, cwd: str | None = None) -> str:
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"rc={proc.returncode} cmd={' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc.stdout or ""


def remote(cmd: str, *, timeout: float = 300) -> str:
    return run(["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd], timeout=timeout)


def scp(local: Path, remote_path: str) -> None:
    run(["scp", str(local), f"{REMOTE}:{remote_path}"], timeout=300)


def main() -> int:
    print("[1/5] health")
    health = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    if not health.get("healthy"):
        raise RuntimeError(health)

    print(f"[2/5] backup {BACKUP}")
    remote(f"mkdir -p {BACKUP} && tar -czf {BACKUP}/web_dist.tgz -C {REMOTE_DIR} web_dist", timeout=180)

    print("[3/5] upload web_dist")
    if not (ROOT / "web_dist" / "index.html").is_file():
        raise RuntimeError("run build_static_frontend.ps1 first")
    archive = ROOT / f"web_dist-deploy-{STAMP}.tgz"
    run(["tar", "-czf", str(archive), "-C", str(ROOT), "web_dist"], timeout=300)
    scp(archive, f"/tmp/web_dist-deploy-{STAMP}.tgz")
    remote(
        f"tar -xzf /tmp/web_dist-deploy-{STAMP}.tgz -C {REMOTE_DIR} && "
        f"test -f {REMOTE_DIR}/web_dist/accounts/index.html",
        timeout=180,
    )

    print("[4/5] recreate")
    remote(f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d --force-recreate", timeout=180)
    time.sleep(8)

    print("[5/5] verify")
    health2 = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    if not health2.get("healthy"):
        raise RuntimeError(health2)
    print(json.dumps({"healthy": True, "backup": BACKUP, "stamp": STAMP}, indent=2))
    archive.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"DEPLOY_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
