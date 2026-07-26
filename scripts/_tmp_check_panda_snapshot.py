#!/usr/bin/env python3
import base64, json, subprocess
py = """
import json, urllib.request
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
req=urllib.request.Request("http://127.0.0.1:8012/api/ops/image-pipeline/snapshot", headers={"Authorization": f"Bearer {auth}"})
snap=json.load(urllib.request.urlopen(req, timeout=20))
settings=json.load(open("/root/gptimage/config.json")).get("image_pipeline") or {}
print(json.dumps({"config_image_pipeline": settings, "snapshot_pools": {k: snap.get(k) for k in ("ps","ss","upload","download","in_flight")}}, indent=2))
"""
b64=base64.b64encode(py.encode()).decode()
print(subprocess.check_output(["ssh","-o","ConnectTimeout=20","panda",f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"'], text=True))
