#!/usr/bin/env python3
import base64
import json
import subprocess
import sys

tag = sys.argv[1] if len(sys.argv) > 1 else "PROD-mixed-conc10-20260727T022823Z"
py = f"""
import json, urllib.request, re
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
req=urllib.request.Request(
    "http://127.0.0.1:8012/api/logs?type=call&limit=500",
    headers={{"Authorization": f"Bearer {{auth}}"}},
)
items=json.load(urllib.request.urlopen(req, timeout=60)).get("items", [])
tag={json.dumps(tag)}
rows=[]
for i in items:
    d=i.get("detail") or {{}}
    blob=json.dumps(d, ensure_ascii=False)
    if tag not in blob:
        continue
    if "调用完成" not in str(i.get("summary") or "") and "调用失败" not in str(i.get("summary") or ""):
        continue
    m=re.search(r"\\|r(\\d{{2}})\\|(\\w+)\\|", blob)
    pt=d.get("phase_timings_ms") if isinstance(d.get("phase_timings_ms"), dict) else {{}}
    st=d.get("schedule_trace") if isinstance(d.get("schedule_trace"), dict) else {{}}
    rows.append({{
        "run": int(m.group(1)) if m else 0,
        "profile": m.group(2) if m else "",
        "duration_ms": d.get("duration_ms"),
        "worker_duration_ms": d.get("worker_duration_ms"),
        "total_wall_ms": d.get("total_wall_ms"),
        "phase_keys": sorted(pt.keys()),
        "phase_nonempty": {{k:v for k,v in pt.items() if int(v or 0)>0}},
        "st_events": st.get("event_count"),
        "st_phases": st.get("phases_ms"),
        "has_schedule_trace": bool(st),
    }})
rows.sort(key=lambda r: r.get("run") or 0)
print(json.dumps(rows, ensure_ascii=False, indent=2))
print("COUNT", len(rows))
"""
b64 = base64.b64encode(py.encode()).decode()
out = subprocess.run(
    ["ssh", "-o", "ConnectTimeout=20", "panda", f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"'],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
)
print(out.stdout)
if out.returncode:
    print(out.stderr, file=sys.stderr)
    raise SystemExit(out.returncode)
