#!/usr/bin/env python3
import json
import subprocess
import traceback

REMOTE_PY = r"""
import json
import sqlite3
import traceback
from pathlib import Path

# recent failed tasks
db = Path('/root/gptimage/data/image_tasks.db')
if db.is_file():
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT task_id, status, data FROM image_tasks WHERE status='error' ORDER BY updated_ts DESC LIMIT 5"
    ).fetchall()
    for task_id, status, data in rows:
        task = json.loads(data or '{}')
        print('TASK', task_id, task.get('error'), task.get('duration_ms'))
    conn.close()

# reproduce get_available_access_token
try:
    import sys
    sys.path.insert(0, '/app')
    from services.account_service import account_service
    t = account_service.get_available_access_token()
    print('ACQUIRED', t[:20])
    account_service.release_image_slot(t)
except Exception as exc:
    print('ACQUIRE_FAIL', exc)
    traceback.print_exc()

# grep deployed source for suspicious token refs near our change
src = Path('/root/gptimage/services/account_service.py').read_text(encoding='utf-8')
for i, line in enumerate(src.splitlines(), 1):
    if 'token' in line and 'def _image_slot_available_locked' in '\n'.join(src.splitlines()[max(0,i-5):i+15]):
        pass
start = src.find('def _image_slot_available_locked')
print('SNIPPET_START')
print(src[start:start+1200])
print('SNIPPET_END')
"""


def main() -> None:
    raw = subprocess.check_output(
        ["ssh", "-o", "ConnectTimeout=20", "panda", f"docker exec chatgpt2api-local /app/.venv/bin/python3 -c {json.dumps(REMOTE_PY)}"],
        text=True,
        errors="replace",
    )
    print(raw)


if __name__ == "__main__":
    main()
