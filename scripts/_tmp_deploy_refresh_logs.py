#!/usr/bin/env python3
"""Deploy: accounts refresh fix (reload-from-storage) + log timezone Asia/Shanghai + web_dist."""
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
BACKUP = f"{REMOTE_DIR}/backups/refresh-logs-{STAMP}"

PY_FILES = [
    "services/log_service.py",
]


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
    print("[1/7] health baseline")
    health = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    if not health.get("healthy"):
        raise RuntimeError(f"unhealthy before deploy: {health}")

    print(f"[2/7] backup {BACKUP}")
    remote(
        f"mkdir -p {BACKUP}/services && "
        f"cp -a {REMOTE_DIR}/config.json {BACKUP}/ && "
        f"tar -czf {BACKUP}/web_dist.tgz -C {REMOTE_DIR} web_dist",
        timeout=180,
    )
    for rel in PY_FILES:
        remote(f"cp -a {REMOTE_DIR}/{rel} {BACKUP}/{rel} 2>/dev/null || true", timeout=60)

    print("[3/7] package web_dist")
    if not (ROOT / "web_dist" / "index.html").is_file():
        raise RuntimeError("web_dist/index.html missing; run scripts/build_static_frontend.ps1 first")
    archive = ROOT / f"web_dist-deploy-{STAMP}.tgz"
    run(["tar", "-czf", str(archive), "-C", str(ROOT), "web_dist"], timeout=300)

    print("[4/7] upload")
    scp(archive, f"/tmp/web_dist-deploy-{STAMP}.tgz")
    for rel in PY_FILES:
        scp(ROOT / rel, f"{REMOTE_DIR}/{rel}")

    print("[5/7] install web_dist + smoke log tz")
    remote(
        f"set -euo pipefail; cd {REMOTE_DIR}; "
        f"tar -xzf /tmp/web_dist-deploy-{STAMP}.tgz -C {REMOTE_DIR}; "
        f"test -f {REMOTE_DIR}/web_dist/accounts/index.html",
        timeout=180,
    )
    smoke = remote(
        "docker exec chatgpt2api-local /app/.venv/bin/python3 - <<'PY'\n"
        "from zoneinfo import ZoneInfo\n"
        "from services.log_service import LogService\n"
        "from pathlib import Path\n"
        "import inspect\n"
        "src = inspect.getsource(LogService.add)\n"
        "assert 'Asia/Shanghai' in src\n"
        "wd = Path('/app/web_dist/accounts/index.html')\n"
        "assert wd.is_file()\n"
        "print('smoke_ok')\n"
        "PY",
        timeout=60,
    )
    print(smoke.strip())

    print("[6/7] recreate container")
    remote(
        f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d --force-recreate",
        timeout=180,
    )
    time.sleep(10)

    print("[7/7] health verify")
    health2 = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    if not health2.get("healthy"):
        raise RuntimeError(f"unhealthy after deploy: {health2}")
    print(json.dumps({"healthy": True, "backup": BACKUP, "stamp": STAMP}, ensure_ascii=False, indent=2))
    archive.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"DEPLOY_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
