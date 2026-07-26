#!/usr/bin/env python3
"""Deploy STAB-B1 warmup dispatch + B4 binding default to Panda."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

REMOTE = "panda"
REMOTE_DIR = "/root/gptimage"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
BACKUP = f"{REMOTE_DIR}/backups/stab-b1-{STAMP}"
ROOT = Path(__file__).resolve().parents[1]

FILES = [
    "services/account_service.py",
    "services/account_warmup_service.py",
    "services/config.py",
]


def remote(cmd: str, timeout: float = 300) -> str:
    proc = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd],
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout or ""


def scp(local: Path, dest: str) -> None:
    proc = subprocess.run(
        ["scp", str(local), f"{REMOTE}:{dest}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)


def main() -> int:
    remote(f"mkdir -p {BACKUP}")
    for rel in FILES:
        remote(f"cp {REMOTE_DIR}/{rel} {BACKUP}/ 2>/dev/null || true")
        scp(ROOT / rel, f"{REMOTE_DIR}/{rel}")

    patch_b64 = __import__("base64").b64encode(
        b"""
import json
p='/root/gptimage/config.json'
d=json.load(open(p))
d['image_binding_inflight_max']=1
json.dump(d,open(p,'w'),indent=2,ensure_ascii=False)
print('image_binding_inflight_max',d.get('image_binding_inflight_max'))
""".strip()
    ).decode()
    remote(f"python3 -c \"import base64; exec(base64.b64decode('{patch_b64}').decode())\"")
    remote(f"cd {REMOTE_DIR} && docker compose restart chatgpt2api-local 2>/dev/null || docker restart chatgpt2api-local")
    import time

    time.sleep(8)
    health = remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'")
    print(health)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
