#!/usr/bin/env python3
"""Upload Webshare-20 list to Panda, set max-5-per-IP, run residential rebind."""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANDA = "panda"
APP = "/root/gptimage"
POOL_REMOTE = f"{APP}/data/runlogs/webshare_20_proxies.secret.txt"
SCRIPT_REMOTE = f"{APP}/scripts/rebind_webshare20_residential_pool.py"


def ssh(cmd: str, *, timeout: int = 600) -> str:
    proc = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", PANDA, cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(f"ssh failed rc={proc.returncode}\n{out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", default=r"C:\Users\Lenovo\Downloads\Webshare 20 proxies.txt")
    ap.add_argument("--max-per-egress", type=int, default=5)
    ap.add_argument("--all", action="store_true", help="rebind all active accounts")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="probe + plan only")
    args = ap.parse_args()

    src = Path(args.proxy_file)
    if not src.is_file():
        print(f"missing proxy file: {src}", file=sys.stderr)
        return 1

    content = src.read_text(encoding="utf-8", errors="replace")
    b64 = base64.b64encode(content.encode()).decode()
    ssh(f"mkdir -p {APP}/data/runlogs {APP}/scripts && python3 -c \"import base64; open('{POOL_REMOTE}','wb').write(base64.b64decode('{b64}'))\"")
    print(f"uploaded {src.name} -> {POOL_REMOTE}")

    # push script
    script_b64 = base64.b64encode((ROOT / "scripts" / "rebind_webshare20_residential_pool.py").read_bytes()).decode()
    ssh(f"python3 -c \"import base64; open('{SCRIPT_REMOTE}','wb').write(base64.b64decode('{script_b64}'))\"")

    # config: max 5 per binding/egress
    patch_py = f"""
import json
p='{APP}/config.json'
with open(p,'r',encoding='utf-8') as f:
    c=json.load(f)
c['proxy_binding_max_accounts']={int(args.max_per_egress)}
with open(p,'w',encoding='utf-8') as f:
    json.dump(c,f,ensure_ascii=False,indent=2)
    f.write('\\n')
print(json.dumps({{'proxy_binding_max_accounts': c.get('proxy_binding_max_accounts')}}, indent=2))
"""
    pb64 = base64.b64encode(patch_py.encode()).decode()
    print(ssh(f"python3 -c \"import base64; exec(base64.b64decode('{pb64}').decode())\""))

    mode = []
    if args.all:
        mode.append("--all")
    if args.apply and not args.dry_run:
        mode.append("--apply")
    cmd = (
        "docker exec chatgpt2api-local uv run python3 /app/scripts/rebind_webshare20_residential_pool.py "
        f"--pool /app/data/runlogs/webshare_20_proxies.secret.txt "
        f"--max-per-egress {int(args.max_per_egress)} " + " ".join(mode)
    )
    out = ssh(cmd, timeout=3600)
    print(out)
    if args.apply and not args.dry_run:
        print(ssh("docker restart chatgpt2api-local && sleep 10"))
        print(ssh("curl -fsS 'http://127.0.0.1:8012/health?format=json' | python3 -c \"import json,sys; d=json.load(sys.stdin); print('image_schedulable', d.get('accounts',{}).get('image_schedulable'))\""))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
