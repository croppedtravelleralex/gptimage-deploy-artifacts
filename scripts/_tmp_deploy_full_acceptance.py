#!/usr/bin/env python3
"""Deploy slot scheduling + unique proxy rebind + FE lazy routes; verify on Panda."""
from __future__ import annotations

import base64
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
BACKUP = f"{REMOTE_DIR}/backups/full-accept-{STAMP}"

PY_FILES = [
    "services/protocol/conversation.py",
    "services/account_service.py",
    "services/config.py",
    "scripts/panda_rebind_unique_proxies.py",
]


def run(cmd: list[str], *, timeout: float = 900) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout or ""


def remote(cmd: str, *, timeout: float = 600) -> str:
    return run(["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd], timeout=timeout)


def scp(local: Path, remote_path: str) -> None:
    run(["scp", str(local), f"{REMOTE}:{remote_path}"], timeout=300)


def main() -> int:
    if not (ROOT / "web_dist" / "index.html").is_file():
        raise RuntimeError("run scripts/build_static_frontend.ps1 first")

    remote(f"mkdir -p {BACKUP}/services/protocol {BACKUP}/scripts && "
           f"cp -a {REMOTE_DIR}/config.json {BACKUP}/ && "
           f"tar -czf {BACKUP}/web_dist.tgz -C {REMOTE_DIR} web_dist")
    for rel in PY_FILES:
        dest = f"{REMOTE_DIR}/{rel}"
        remote(f"mkdir -p $(dirname {dest})")
        scp(ROOT / rel, dest)

    archive = ROOT / f"web_dist-deploy-{STAMP}.tgz"
    run(["tar", "-czf", str(archive), "-C", str(ROOT), "web_dist"], timeout=300)
    scp(archive, f"/tmp/web_dist-deploy-{STAMP}.tgz")
    remote(f"tar -xzf /tmp/web_dist-deploy-{STAMP}.tgz -C {REMOTE_DIR}")

    patch = """
import json
p='/root/gptimage/config.json'
c=json.load(open(p))
c['dispatch_hot_only']=False
c['proxy_binding_max_accounts']=1
pipe=c.setdefault('image_pipeline', {})
pipe['prompt_slots']=10
pipe['sse_slots']=10
json.dump(c, open(p,'w'), ensure_ascii=False, indent=2)
print(json.dumps({'dispatch_hot_only':c.get('dispatch_hot_only'),'proxy_binding_max_accounts':c.get('proxy_binding_max_accounts'),'sse_slots':pipe.get('sse_slots')}))
"""
    print(remote(f"python3 -c \"import base64; exec(base64.b64decode('{base64.b64encode(patch.encode()).decode()}').decode())\""))

    remote(f"cd {REMOTE_DIR} && docker compose -f docker-compose.panda.yml up -d --force-recreate", timeout=180)
    time.sleep(12)

    rebind = remote(
        f"docker exec chatgpt2api-local uv run python3 /app/scripts/panda_rebind_unique_proxies.py --apply --timeout 25 2>&1 | tail -n 80",
        timeout=900,
    )
    print(rebind)

    verify_py = """
import json, urllib.request
auth=json.load(open('/root/gptimage/config.json'))['auth-key']
hdr={'Authorization':'Bearer '+auth}

def get(path):
    return json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8012'+path, headers=hdr), timeout=30))

health=get('/health?format=json')
breakdown=get('/api/accounts/schedulable-breakdown')
snap=get('/api/ops/image-pipeline/snapshot')
print(json.dumps({
  'healthy': health.get('healthy'),
  'image_schedulable': health.get('accounts',{}).get('image_schedulable'),
  'dispatchable': health.get('accounts',{}).get('dispatchable_candidate_count'),
  'dup_binding': breakdown.get('buckets',{}).get('excluded_by_dup_binding'),
  'schedulable': breakdown.get('buckets',{}).get('schedulable'),
  'ss_limit': snap.get('ss',{}).get('limit'),
  'dispatch_hot_only': json.load(open('/root/gptimage/config.json')).get('dispatch_hot_only'),
}, ensure_ascii=False, indent=2))
"""
    out = remote(f"python3 -c \"import base64; exec(base64.b64decode('{base64.b64encode(verify_py.encode()).decode()}').decode())\"", timeout=60)
    print(out)
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            data = json.loads(line)
            break
    else:
        raise RuntimeError(f"verify output missing json: {out[:500]}")
    if not data.get("healthy"):
        raise RuntimeError(data)
    archive.unlink(missing_ok=True)
    print(json.dumps({"backup": BACKUP, "verify": data}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
