#!/usr/bin/env python3
import base64, json, subprocess
path = "/app/data/runlogs/spa_repro/pipeline-conc10/pipe-conc10-20260724T134916Z.json"
py = f"""
import json
d=json.load(open('{path}'))
print(json.dumps(list(d.keys()), indent=2))
print(json.dumps(d, ensure_ascii=False, indent=2)[:12000])
"""
b64 = base64.b64encode(py.encode()).decode()
out = subprocess.check_output(["ssh", "-o", "ConnectTimeout=20", "panda", f"docker exec chatgpt2api-local uv run python3 -c \"import base64; exec(base64.b64decode('{b64}').decode())\""], text=True)
print(out)
