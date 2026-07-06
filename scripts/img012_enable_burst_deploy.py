#!/usr/bin/env python3
"""启用 IMG-012 burst 8 条件升档。"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "panda"
REMOTE_DIR = "/root/gptimage"


def run(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr or p.stdout)
    return p.stdout


def remote(cmd, timeout=120):
    return run(["ssh", "-o", "ConnectTimeout=15", REMOTE, cmd], timeout=timeout)


def main():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{REMOTE_DIR}/backups/img012-burst8-{stamp}"
    remote(f"mkdir -p {backup} && cp {REMOTE_DIR}/config.json {backup}/config.json.bak")
    patch = ROOT / "scripts" / "img012_enable_burst.py"
    patch.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "p=Path('/root/gptimage/config.json')\n"
        "d=json.loads(p.read_text(encoding='utf-8'))\n"
        "q=d.setdefault('image_task_queue',{})\n"
        "q['burst_enabled']=True\n"
        "q['per_user_running_burst']=8\n"
        "p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\\n',encoding='utf-8')\n"
        "print(json.dumps({'burst_enabled':q.get('burst_enabled'),'base':q.get('per_user_running_base'),'burst':q.get('per_user_running_burst')},ensure_ascii=False))\n",
        encoding="utf-8",
    )
    run(["scp", str(patch), f"{REMOTE}:{REMOTE_DIR}/scripts/img012_enable_burst.py"], timeout=60)
    print(remote(f"python3 {REMOTE_DIR}/scripts/img012_enable_burst.py"))
    remote(f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d")
    time.sleep(8)
    health = json.loads(remote("curl -sS http://127.0.0.1:8012/health?format=json", timeout=30))
    print(json.dumps({"burst_deploy_ok": health.get("healthy"), "backup": backup}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
