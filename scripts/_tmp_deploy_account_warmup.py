#!/usr/bin/env python3
"""Patch Panda config account_warmup + deploy warmup code + restart."""
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
BACKUP = f"{REMOTE_DIR}/backups/account-warmup-{STAMP}"

WARMUP_CONFIG = {
    "enabled": True,
    "interval_sec": 60,
    "max_hot": 10,
    "max_sessions_per_hot": 3,
    "demote_cooldown_sec": 180,
    "freq_window_sec": 60,
    "freq_max_starts": 6,
    "startup_delay_sec": 20,
    "depth": "requirements",
    "rotate_per_tick": 0,
    "hot_refresh_min_interval_sec": 300,
    "schedulable_only": True,
    "cf_fail_max_streak": 2,
    "cf_block_sec": 86400,
}

PY_FILES = [
    "services/config.py",
    "services/account_warmup_service.py",
]


def run(cmd: list[str], *, timeout: float = 300) -> str:
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
    run(["scp", str(local), f"{REMOTE}:{remote_path}"], timeout=120)


def main() -> int:
    print("[1/6] health baseline")
    health = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    print(json.dumps({"healthy": health.get("healthy"), "accounts": health.get("accounts")}, ensure_ascii=False, indent=2)[:800])

    print(f"[2/6] backup -> {BACKUP}")
    remote(
        f"mkdir -p {BACKUP}/services && "
        f"cp -a {REMOTE_DIR}/config.json {BACKUP}/ && "
        + " && ".join(f"cp -a {REMOTE_DIR}/{rel} {BACKUP}/{rel}" for rel in PY_FILES),
        timeout=120,
    )

    print("[3/6] upload warmup code")
    for rel in PY_FILES:
        local = ROOT / rel
        remote(f"mkdir -p {REMOTE_DIR}/{'/'.join(rel.split('/')[:-1])}")
        scp(local, f"{REMOTE_DIR}/{rel}")

    print("[4/6] patch config.json account_warmup")
    patch = json.dumps(WARMUP_CONFIG, ensure_ascii=False)
    remote(
        "python3 - <<'PY'\n"
        "import json\n"
        f"from pathlib import Path\n"
        f"p = Path('{REMOTE_DIR}/config.json')\n"
        "data = json.loads(p.read_text(encoding='utf-8'))\n"
        f"data['account_warmup'] = json.loads('''{patch}''')\n"
        "p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')\n"
        "print(json.dumps(data.get('account_warmup'), ensure_ascii=False, indent=2))\n"
        "PY",
        timeout=60,
    )

    print("[5/6] restart app container")
    remote(f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml restart app", timeout=180)
    time.sleep(10)

    print("[6/6] verify health + warmup status")
    health2 = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    if not health2.get("healthy"):
        raise RuntimeError(f"unhealthy after restart: {health2}")
    auth = remote(
        f"python3 -c \"import json; print(json.load(open('{REMOTE_DIR}/config.json')).get('auth-key',''))\""
    ).strip()
    warmup = remote(
        f"curl -fsS --max-time 20 -H 'Authorization: Bearer {auth}' 'http://127.0.0.1:8012/api/ops/warmup/status'"
    )
    print(json.dumps(json.loads(warmup), ensure_ascii=False, indent=2))
    print(json.dumps({"backup": BACKUP, "healthy": True}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"DEPLOY_FAILED: {exc}", file=sys.stderr)
        raise
