#!/usr/bin/env python3
import base64, json, subprocess

py = """
import json, urllib.request
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
hdr={"Authorization":"Bearer "+auth}
req=urllib.request.Request("http://127.0.0.1:8012/api/logs?type=call&limit=30", headers=hdr)
items=json.load(urllib.request.urlopen(req, timeout=60)).get("items", [])
rows=[]
for i in items:
    d=i.get("detail") or {}
    pt=d.get("phase_timings_ms") if isinstance(d.get("phase_timings_ms"), dict) else {}
    if not pt and not d.get("duration_ms"):
        continue
    prompt=str(d.get("prompt") or "")[:120]
    rows.append({
    "time": i.get("time"),
    "summary": i.get("summary"),
    "prompt": prompt,
    "account": d.get("account_email") or d.get("preferred_account_email"),
    "phase_timings_ms": pt,
    "duration_ms": d.get("duration_ms"),
  })
print(json.dumps(rows[:15], ensure_ascii=False, indent=2))
"""
b64=base64.b64encode(py.encode()).decode()
print(subprocess.check_output(["ssh","-o","ConnectTimeout=20","panda",f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"'], text=True))
