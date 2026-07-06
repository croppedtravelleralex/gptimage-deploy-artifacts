#!/usr/bin/env python3
"""IMG-012 deploy helper."""
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
FILES = [
    ("api/ai.py", f"{REMOTE_DIR}/api/ai.py"),
    ("services/image_task_service.py", f"{REMOTE_DIR}/services/image_task_service.py"),
    ("services/image_sync_adapter.py", f"{REMOTE_DIR}/services/image_sync_adapter.py"),
    ("services/config.py", f"{REMOTE_DIR}/services/config.py"),
    ("services/account_service.py", f"{REMOTE_DIR}/services/account_service.py"),
    ("services/protocol/conversation.py", f"{REMOTE_DIR}/services/protocol/conversation.py"),
    ("services/protocol/openai_v1_image_generations.py", f"{REMOTE_DIR}/services/protocol/openai_v1_image_generations.py"),
    ("services/protocol/openai_v1_image_edit.py", f"{REMOTE_DIR}/services/protocol/openai_v1_image_edit.py"),
]


def run(cmd: list[str], *, timeout: float = 180) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode} cmd={' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc.stdout or ""


def remote(cmd: str, timeout: float = 180) -> str:
    return run(["ssh", "-o", "ConnectTimeout=15", REMOTE, cmd], timeout=timeout)


def scp(local: Path, remote_path: str) -> None:
    run(["scp", str(local), f"{REMOTE}:{remote_path}"], timeout=180)


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{REMOTE_DIR}/backups/img012-sync-over-async-{stamp}"
    print("[1/7] health baseline")
    health = json.loads(remote("curl -sS http://127.0.0.1:8012/health?format=json", timeout=30))
    accounts = health.get("accounts") or {}
    print(json.dumps({
        "healthy": health.get("healthy"),
        "dispatchable": accounts.get("dispatchable_candidate_count"),
        "image_inflight": accounts.get("image_inflight_count"),
    }, ensure_ascii=False))

    print(f"[2/7] backup {backup}")
    remote(
        f"mkdir -p {backup} && "
        f"cp {REMOTE_DIR}/api/ai.py {REMOTE_DIR}/services/image_task_service.py "
        f"{REMOTE_DIR}/services/config.py {backup}/ && "
        f"test -f {REMOTE_DIR}/services/image_sync_adapter.py && "
        f"cp {REMOTE_DIR}/services/image_sync_adapter.py {backup}/ || true && "
        f"cp {REMOTE_DIR}/config.json {backup}/config.json.bak",
        timeout=60,
    )

    print("[3/7] upload")
    for rel, remote_path in FILES:
        scp(ROOT / rel, remote_path)

    print("[4/7] patch config")
    scp(ROOT / "scripts/img012_patch_config.py", f"{REMOTE_DIR}/scripts/img012_patch_config.py")
    print(remote(f"python3 {REMOTE_DIR}/scripts/img012_patch_config.py", timeout=30))

    print("[5/7] restart")
    remote(f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d", timeout=120)
    time.sleep(10)

    print("[6/7] import smoke")
    remote(
        "docker compose -f /root/gptimage/docker-compose.panda.yml exec -T -e PYTHONDONTWRITEBYTECODE=1 app "
        "python3 -c \"import importlib.util; "
        "spec=importlib.util.spec_from_file_location('ai','/app/api/ai.py'); "
        "m=importlib.util.module_from_spec(spec); print('syntax_ok')\"",
        timeout=90,
    )

    print("[7/7] health verify")
    health2 = json.loads(remote("curl -sS http://127.0.0.1:8012/health?format=json", timeout=30))
    if not health2.get("healthy"):
        raise RuntimeError(f"unhealthy: {health2}")
    rollback = (
        f"#!/bin/bash\n"
        f"set -euo pipefail\n"
        f"cp {backup}/ai.py {REMOTE_DIR}/api/ai.py\n"
        f"cp {backup}/image_task_service.py {backup}/config.py {REMOTE_DIR}/services/\n"
        f"test -f {backup}/image_sync_adapter.py && rm -f {REMOTE_DIR}/services/image_sync_adapter.py || true\n"
        f"cp {backup}/config.json.bak {REMOTE_DIR}/config.json\n"
        f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d\n"
    )
    remote(f"cat > {backup}/ROLLBACK.sh <<'EOF'\n{rollback}EOF\nchmod +x {backup}/ROLLBACK.sh", timeout=30)
    print(json.dumps({"deploy_ok": True, "backup": backup}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"DEPLOY_FAILED: {exc}", file=sys.stderr)
        raise
