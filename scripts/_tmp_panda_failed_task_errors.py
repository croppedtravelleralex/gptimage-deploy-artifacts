#!/usr/bin/env python3
import json
import subprocess

REMOTE_PY = """
import json, sqlite3
from pathlib import Path
conn = sqlite3.connect('/root/gptimage/data/image_tasks.db')
rows = conn.execute("SELECT task_id, data FROM image_tasks WHERE status='error' ORDER BY updated_ts DESC LIMIT 8").fetchall()
for task_id, data in rows:
    t = json.loads(data or '{}')
    print('---', task_id)
    print('error:', t.get('error'))
    print('progress:', t.get('progress'))
conn.close()
"""

raw = subprocess.check_output(
    ["ssh", "-o", "ConnectTimeout=20", "panda", f"python3 -c {json.dumps(REMOTE_PY)}"],
    text=True,
    errors="replace",
)
print(raw)
