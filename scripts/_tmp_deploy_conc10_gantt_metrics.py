#!/usr/bin/env python3
"""Deploy conc10 multi-account + gantt + accounts metrics row to Panda."""
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
BACKUP = f"{REMOTE_DIR}/backups/conc10-gantt-metrics-{STAMP}"

PY_FILES = [
    "api/image_tasks.py",
    "services/config.py",
    "services/image_task_service.py",
    "services/image_pipeline/types.py",
    "services/image_pipeline/orchestrator.py",
    "services/protocol/conversation.py",
    "services/protocol/openai_v1_image_generations.py",
    "utils/image_gantt_segments.py",
    "scripts/_tmp_pipeline_conc10_acceptance.py",
]


def run(cmd: list[str], *, timeout: float = 600) -> str:
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode} cmd={' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
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
                "image_account_concurrency_limit": accounts.get("image_account_concurrency_limit"),
            },
            ensure_ascii=False,
        )
    )

    print(f"[2/8] backup {BACKUP}")
    remote(
        f"mkdir -p {BACKUP}/api {BACKUP}/services/image_pipeline {BACKUP}/services/protocol {BACKUP}/utils {BACKUP}/scripts && "
        f"cp -a {REMOTE_DIR}/config.json {BACKUP}/ && "
        f"tar -czf {BACKUP}/web_dist.tgz -C {REMOTE_DIR} web_dist",
        timeout=180,
    )
    for rel in PY_FILES:
        remote(f"test -f {REMOTE_DIR}/{rel} && cp -a {REMOTE_DIR}/{rel} {BACKUP}/{rel} || true", timeout=60)

    print("[3/8] package web_dist")
    archive = ROOT / f"web_dist-deploy-{STAMP}.tgz"
    run(["tar", "-czf", str(archive), "-C", str(ROOT), "web_dist"], timeout=300)

    print("[4/8] upload artifacts")
    scp(archive, f"/tmp/web_dist-deploy-{STAMP}.tgz")
    for rel in PY_FILES:
        local = ROOT / rel
        if not local.is_file():
            print(f"skip missing {rel}")
            continue
        remote_dir = f"{REMOTE_DIR}/{'/'.join(rel.split('/')[:-1])}"
        remote(f"mkdir -p {remote_dir}")
        scp(local, f"{REMOTE_DIR}/{rel}")

    print("[5/8] install web_dist + patch config")
    remote(
        f"set -euo pipefail; "
        f"cd {REMOTE_DIR}; "
        f"tar -xzf /tmp/web_dist-deploy-{STAMP}.tgz -C {REMOTE_DIR}; "
        f"test -f {REMOTE_DIR}/web_dist/index.html; "
        f"python3 - <<'PY'\n"
        "import json\n"
        "from pathlib import Path\n"
        f"p = Path('{REMOTE_DIR}/config.json')\n"
        "data = json.loads(p.read_text(encoding='utf-8'))\n"
        "data['image_account_concurrency'] = 2\n"
        "p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')\n"
        "print('image_account_concurrency=', data.get('image_account_concurrency'))\n"
        "PY",
        timeout=180,
    )

    print("[6/8] recreate container (remount web_dist)")
    remote(f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d --force-recreate", timeout=180)
    time.sleep(8)

    print("[7/8] smoke import")
    remote(
        "docker exec chatgpt2api-local /app/.venv/bin/python3 -c "
        "'from utils.image_gantt_segments import build_image_task_gantt_segments; print(\"import_ok\")'",
        timeout=60,
    )

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
                "image_account_concurrency_limit": accounts2.get("image_account_concurrency_limit"),
                "available_image_quota": accounts2.get("available_image_quota"),
                "backup": BACKUP,
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
        raise
