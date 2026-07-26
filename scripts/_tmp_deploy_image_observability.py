#!/usr/bin/env python3
"""DEPLOY-001: deploy image observability backend + web_dist to Panda."""
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
BACKUP = f"{REMOTE_DIR}/backups/image-observability-{STAMP}"

PY_FILES = [
    "api/system.py",
    "services/image_task_service.py",
    "services/image_storage_service.py",
    "services/image_service.py",
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
    print("[1/8] health baseline")
    health = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    accounts = health.get("accounts") or {}
    print(
        json.dumps(
            {
                "healthy": health.get("healthy"),
                "image_schedulable": accounts.get("image_schedulable"),
                "dispatchable": accounts.get("dispatchable_candidate_count"),
                "inflight": accounts.get("image_inflight_count"),
            },
            ensure_ascii=False,
        )
    )

    print(f"[2/8] backup {BACKUP}")
    remote(
        f"mkdir -p {BACKUP}/api {BACKUP}/services && "
        f"cp -a {REMOTE_DIR}/config.json {BACKUP}/ && "
        f"tar -czf {BACKUP}/web_dist.tgz -C {REMOTE_DIR} web_dist",
        timeout=180,
    )
    for rel in PY_FILES:
        remote(f"test -f {REMOTE_DIR}/{rel} && cp -a {REMOTE_DIR}/{rel} {BACKUP}/{rel} || true", timeout=60)

    print("[3/8] package web_dist")
    if not (ROOT / "web_dist" / "index.html").is_file():
        raise RuntimeError("web_dist/index.html missing; run build_static_frontend.ps1 first")
    archive = ROOT / f"web_dist-deploy-{STAMP}.tgz"
    run(["tar", "-czf", str(archive), "-C", str(ROOT), "web_dist"], timeout=300)

    print("[4/8] upload artifacts")
    scp(archive, f"/tmp/web_dist-deploy-{STAMP}.tgz")
    for rel in PY_FILES:
        local = ROOT / rel
        if not local.is_file():
            raise RuntimeError(f"missing local file: {rel}")
        remote_parent = f"{REMOTE_DIR}/{'/'.join(rel.split('/')[:-1])}"
        remote(f"mkdir -p {remote_parent}")
        scp(local, f"{REMOTE_DIR}/{rel}")

    print("[5/8] install web_dist")
    remote(
        f"set -euo pipefail; cd {REMOTE_DIR}; "
        f"tar -xzf /tmp/web_dist-deploy-{STAMP}.tgz -C {REMOTE_DIR}; "
        f"test -f {REMOTE_DIR}/web_dist/index.html; "
        f"test -f {REMOTE_DIR}/web_dist/logs/index.html",
        timeout=180,
    )

    print("[6/8] recreate container (remount web_dist)")
    remote(
        f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d --force-recreate",
        timeout=180,
    )
    time.sleep(10)

    print("[7/8] smoke markers")
    smoke = remote(
        "docker exec chatgpt2api-local /app/.venv/bin/python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "import services.image_task_service as its\n"
        "import services.image_storage_service as iss\n"
        "import services.image_service as ims\n"
        "assert hasattr(its, '_call_log_usage_fields')\n"
        "assert hasattr(its, '_emit_pending_call_log')\n"
        "assert hasattr(ims, 'ensure_thumbnail')\n"
        "wd = Path('/app/web_dist')\n"
        "assert (wd / 'index.html').is_file()\n"
        "assert (wd / 'logs' / 'index.html').is_file()\n"
        "print('smoke_ok')\n"
        "PY",
        timeout=60,
    )
    print(smoke.strip())

    print("[8/8] health verify")
    health2 = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    if not health2.get("healthy"):
        raise RuntimeError(f"unhealthy after deploy: {health2}")
    accounts2 = health2.get("accounts") or {}
    print(
        json.dumps(
            {
                "healthy": health2.get("healthy"),
                "image_schedulable": accounts2.get("image_schedulable"),
                "dispatchable": accounts2.get("dispatchable_candidate_count"),
                "inflight": accounts2.get("image_inflight_count"),
                "backup": BACKUP,
                "stamp": STAMP,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    archive.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"DEPLOY_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
