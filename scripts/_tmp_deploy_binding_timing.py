#!/usr/bin/env python3
"""Deploy binding-inflight + timing logs + UI total_wall_ms to Panda."""
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
BACKUP = f"{REMOTE_DIR}/backups/binding-timing-{STAMP}"

PY_FILES = [
    "services/config.py",
    "services/account_service.py",
    "services/image_task_service.py",
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
        raise RuntimeError(f"rc={proc.returncode} cmd={' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc.stdout or ""


def remote(cmd: str, *, timeout: float = 300) -> str:
    return run(["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd], timeout=timeout)


def scp(local: Path, remote_path: str) -> None:
    run(["scp", str(local), f"{REMOTE}:{remote_path}"], timeout=300)


def patch_config() -> dict:
    script = f"""
import json
from pathlib import Path
p = Path('{REMOTE_DIR}/config.json')
data = json.loads(p.read_text(encoding='utf-8'))
data['image_account_concurrency'] = int(data.get('image_account_concurrency') or 2)
data['image_binding_inflight_max'] = 2
p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')
print(json.dumps({{
    'image_account_concurrency': data.get('image_account_concurrency'),
    'image_binding_inflight_max': data.get('image_binding_inflight_max'),
}}, ensure_ascii=False))
"""
    return json.loads(remote(f"python3 - <<'PY'\n{script}\nPY"))


def main() -> int:
    print("[1/9] health baseline")
    health = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    print(json.dumps({"healthy": health.get("healthy"), "accounts": health.get("accounts")}, ensure_ascii=False)[:500])

    print(f"[2/9] backup {BACKUP}")
    remote(
        f"mkdir -p {BACKUP}/services && cp -a {REMOTE_DIR}/config.json {BACKUP}/ && "
        f"tar -czf {BACKUP}/web_dist.tgz -C {REMOTE_DIR} web_dist",
        timeout=180,
    )
    for rel in PY_FILES:
        remote(f"test -f {REMOTE_DIR}/{rel} && cp -a {REMOTE_DIR}/{rel} {BACKUP}/{rel} || true", timeout=60)

    print("[3/9] build web_dist")
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    run([npm, "run", "build"], cwd=str(ROOT / "web"), timeout=600)

    print("[4/9] package web_dist")
    archive = ROOT / f"web_dist-deploy-{STAMP}.tgz"
    run(["tar", "-czf", str(archive), "-C", str(ROOT), "web_dist"], timeout=300)

    print("[5/9] upload artifacts")
    scp(archive, f"/tmp/web_dist-deploy-{STAMP}.tgz")
    for rel in PY_FILES:
        scp(ROOT / rel, f"{REMOTE_DIR}/{rel}")

    print("[6/9] install web_dist + patch config")
    remote(
        f"set -euo pipefail; cd {REMOTE_DIR}; "
        f"tar -xzf /tmp/web_dist-deploy-{STAMP}.tgz -C {REMOTE_DIR}; "
        f"test -f {REMOTE_DIR}/web_dist/index.html",
        timeout=180,
    )
    cfg = patch_config()
    print("config:", json.dumps(cfg, ensure_ascii=False))

    print("[7/9] recreate container")
    remote(f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d --force-recreate", timeout=180)
    time.sleep(10)

    print("[8/9] smoke import")
    remote(
        "docker exec chatgpt2api-local /app/.venv/bin/python3 -c "
        "'from services.config import config; from services.account_service import AccountService; "
        "print(\"binding_default\", config.image_binding_inflight_max)'",
        timeout=60,
    )

    print("[9/9] health verify")
    health2 = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    if not health2.get("healthy"):
        raise RuntimeError(f"unhealthy after deploy: {health2}")
    print(json.dumps({"healthy": health2.get("healthy"), "backup": BACKUP}, ensure_ascii=False))
    archive.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"DEPLOY_FAILED: {exc}", file=sys.stderr)
        raise
