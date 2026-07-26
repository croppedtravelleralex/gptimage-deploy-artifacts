#!/usr/bin/env python3
"""Diagnose egress IP collisions + schedulable exclusions on Panda."""
import base64, json, subprocess

py = r"""
import json, urllib.request
from collections import defaultdict

auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
hdr={"Authorization":"Bearer "+auth}

def get(path):
    return json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8012"+path, headers=hdr), timeout=60))

accounts=get("/api/accounts").get("items") or []
by_egress=defaultdict(list)
by_binding=defaultdict(list)
sched=[]
not_sched=[]
for a in accounts:
    email=str(a.get("email") or "")
    egress=str(a.get("proxy_egress_ip") or "").strip()
    binding=str(a.get("proxy_binding_hash") or "")[:12]
    quota=int(a.get("quota") or 0)
    status=str(a.get("status") or "")
    panda=str(a.get("panda_receive_state") or "")
    img=bool(a.get("image_schedulable"))
    if egress:
        by_egress[egress].append(email)
    if binding:
        by_binding[binding].append(email)
    row={"email":email,"quota":quota,"status":status,"panda":panda,"image_schedulable":img,"egress":egress,"binding":binding}
    if img:
        sched.append(row)
    elif quota>0 and status=="正常":
        not_sched.append(row)

shared_egress={k:v for k,v in by_egress.items() if len(v)>1}
shared_binding={k:v for k,v in by_binding.items() if len(v)>1}
print(json.dumps({
  "total": len(accounts),
  "image_schedulable": len(sched),
  "quota_normal_not_schedulable": len(not_sched),
  "shared_egress_count": len(shared_egress),
  "shared_egress": {k: len(v) for k,v in sorted(shared_egress.items(), key=lambda x:-len(x[1]))[:8]},
  "shared_binding_count": len(shared_binding),
  "not_sched_samples": not_sched[:12],
  "health": get("/health?format=json").get("accounts",{}),
  "breakdown": get("/api/accounts/schedulable-breakdown").get("buckets"),
}, ensure_ascii=False, indent=2))
"""
b64 = base64.b64encode(py.encode()).decode()
print(subprocess.check_output(["ssh", "-o", "ConnectTimeout=20", "panda", f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"'], text=True))
