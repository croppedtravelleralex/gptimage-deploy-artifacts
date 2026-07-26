#!/usr/bin/env python3
"""Verify 1-account-1-egress and API/UI field consistency on Panda."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter


def remote(cmd: str) -> str:
    proc = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", "panda", cmd],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", default="panda")
    args = ap.parse_args()
    if args.remote != "panda":
        print("only panda supported", file=sys.stderr)
        return 1

    py = r'''
import json, urllib.request
from collections import Counter
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
hdr={"Authorization":"Bearer "+auth}

def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60))

health=get("http://127.0.0.1:8012/health?format=json")
accounts=get("http://127.0.0.1:8012/api/accounts?limit=500").get("items",[])
breakdown=get("http://127.0.0.1:8012/api/accounts/schedulable-breakdown")

eg=Counter(); bind=Counter(); mism=[]
for a in accounts:
    if not isinstance(a, dict):
        continue
    e=str(a.get("proxy_egress_ip") or "").strip()
    b=str(a.get("proxy_binding_hash") or "").strip()
    if e: eg[e]+=1
    if b: bind[b]+=1
    sched=bool(a.get("image_schedulable"))
    if sched and e and eg[e]>1:
        pass

dup_eg=[(k,v) for k,v in eg.items() if v>1]
dup_bind=[(k,v) for k,v in bind.items() if v>1]
sched_emails=sorted(str(a.get("email") or "") for a in accounts if a.get("image_schedulable"))

out={
    "healthy": health.get("healthy"),
    "total": len(accounts),
    "image_schedulable_api": sum(1 for a in accounts if a.get("image_schedulable")),
    "image_schedulable_health": (health.get("accounts") or {}).get("image_schedulable"),
    "dispatchable": (health.get("accounts") or {}).get("dispatchable_candidate_count"),
    "dup_egress_groups": dup_eg,
    "dup_binding_groups": len(dup_bind),
    "unique_egress": len(eg),
    "excluded_by_dup_egress": (breakdown.get("buckets") or {}).get("excluded_by_dup_egress"),
    "schedulable_count_breakdown": (breakdown.get("buckets") or {}).get("schedulable"),
    "process_memory": health.get("process_memory"),
    "schedulable_sample": sched_emails[:5],
}
print(json.dumps(out, ensure_ascii=False))
'''
    import base64

    b64 = base64.b64encode(py.encode()).decode()
    out = remote(f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"')
    report = json.loads(out.strip())
    ok = (
        report.get("healthy")
        and not report.get("dup_egress_groups")
        and report.get("image_schedulable_api") == report.get("image_schedulable_health")
    )
    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
