#!/usr/bin/env python3
import base64, json, subprocess
py = """
import json, urllib.request
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
h=json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8012/health?format=json", headers={"Authorization":"Bearer "+auth}), timeout=20))
b=json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8012/api/accounts/schedulable-breakdown", headers={"Authorization":"Bearer "+auth}), timeout=20))
wreq=urllib.request.Request("http://127.0.0.1:8012/api/ops/warmup/status", headers={"Authorization":"Bearer "+auth})
w=json.load(urllib.request.urlopen(wreq, timeout=20))
print(json.dumps({"health_accounts": h.get("accounts"), "schedulable_breakdown": b, "warmup": w}, ensure_ascii=False, indent=2))
"""
b64=base64.b64encode(py.encode()).decode()
print(subprocess.check_output(["ssh","-o","ConnectTimeout=20","panda",f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"'], text=True))
