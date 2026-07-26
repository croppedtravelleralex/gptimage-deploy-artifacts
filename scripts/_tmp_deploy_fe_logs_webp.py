#!/usr/bin/env python3
"""Deploy: WebP thumbs + log phases UI + explicit image_pipeline 10 slots + web_dist."""
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
BACKUP = f"{REMOTE_DIR}/backups/fe-logs-webp-{STAMP}"

PY_FILES = [
    "services/image_service.py",
]


def run(cmd: list[str], *, timeout: float = 600) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout or ""


def remote(cmd: str, *, timeout: float = 300) -> str:
    return run(["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd], timeout=timeout)


def scp(local: Path, remote_path: str) -> None:
    run(["scp", str(local), f"{REMOTE}:{remote_path}"], timeout=300)


def main() -> int:
    if not (ROOT / "web_dist" / "index.html").is_file():
        raise RuntimeError("run build_static_frontend.ps1 first")

    remote(f"mkdir -p {BACKUP}/services && cp -a {REMOTE_DIR}/config.json {BACKUP}/ && tar -czf {BACKUP}/web_dist.tgz -C {REMOTE_DIR} web_dist")

    archive = ROOT / f"web_dist-deploy-{STAMP}.tgz"
    run(["tar", "-czf", str(archive), "-C", str(ROOT), "web_dist"], timeout=300)
    scp(archive, f"/tmp/web_dist-deploy-{STAMP}.tgz")
    for rel in PY_FILES:
        scp(ROOT / rel, f"{REMOTE_DIR}/{rel}")

    remote(f"tar -xzf /tmp/web_dist-deploy-{STAMP}.tgz -C {REMOTE_DIR}")
    patch_py = """
import json
p='/root/gptimage/config.json'
c=json.load(open(p))
pipe=c.setdefault('image_pipeline', {})
pipe['prompt_slots']=10
pipe['sse_slots']=10
json.dump(c, open(p,'w'), ensure_ascii=False, indent=2)
print(json.dumps(pipe))
"""
    import base64
    b64 = base64.b64encode(patch_py.encode()).decode()
    print(remote(f"python3 -c \"import base64; exec(base64.b64decode('{b64}').decode())\"", timeout=60))

    remote(f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d --force-recreate", timeout=180)
    time.sleep(10)

    snap = json.loads(remote(
        "python3 -c \"import json,urllib.request; c=json.load(open('/root/gptimage/config.json')); "
        "auth=c['auth-key']; r=urllib.request.Request('http://127.0.0.1:8012/api/ops/image-pipeline/snapshot',"
        "headers={'Authorization':'Bearer '+auth}); print(json.dumps(json.load(urllib.request.urlopen(r,timeout=20))['ss']))\""
    ))
    health = json.loads(remote("curl -fsS --max-time 20 'http://127.0.0.1:8012/health?format=json'"))
    if not health.get("healthy"):
        raise RuntimeError(health)
    print(json.dumps({"healthy": True, "ss_pool": snap, "backup": BACKUP}, indent=2))
    archive.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"DEPLOY_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
