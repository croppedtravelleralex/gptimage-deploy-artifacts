#!/usr/bin/env python3
import base64, json, subprocess
py = """
import json, urllib.request
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
hdr={"Authorization":"Bearer "+auth}
urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8012/api/accounts/reload-from-storage", method="POST", headers=hdr), timeout=30).read()
b=json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8012/api/accounts/schedulable-breakdown", headers=hdr), timeout=30))
h=json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8012/health?format=json", headers=hdr), timeout=30))
print(json.dumps({"reload":"ok","image_schedulable":h["accounts"]["image_schedulable"],"dispatchable":h["accounts"]["dispatchable_candidate_count"],"dup_binding":b["buckets"]["excluded_by_dup_binding"],"schedulable":b["buckets"]["schedulable"]}, indent=2))
"""
b64 = base64.b64encode(py.encode()).decode()
print(subprocess.check_output(["ssh", "-o", "ConnectTimeout=20", "panda", f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"'], text=True))
