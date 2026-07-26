#!/usr/bin/env python3
import base64, json, subprocess
py = """
import json
c=json.load(open("/root/gptimage/config.json"))
p=c.get("image_pipeline") or {}
print(json.dumps({
  "prompt_slots": p.get("prompt_slots"),
  "sse_slots": p.get("sse_slots"),
  "image_binding_inflight_max": c.get("image_binding_inflight_max"),
}, indent=2))
"""
b64=base64.b64encode(py.encode()).decode()
out=subprocess.check_output(["ssh","-o","ConnectTimeout=20","panda",f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"'], text=True)
print(out)
