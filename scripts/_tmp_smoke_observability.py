#!/usr/bin/env python3
import subprocess
import sys

cmd = [
    "ssh",
    "-o",
    "ConnectTimeout=15",
    "panda",
    "docker exec chatgpt2api-local /app/.venv/bin/python3 -c "
    "\"import services.image_task_service as its; "
    "from pathlib import Path; "
    "assert hasattr(its, '_emit_pending_call_log'); "
    "assert (Path('/app/web_dist/logs/index.html')).is_file(); "
    "print('smoke_ok')\"",
]
proc = subprocess.run(cmd, text=True, capture_output=True, encoding="utf-8", errors="replace")
sys.stdout.write(proc.stdout or "")
sys.stderr.write(proc.stderr or "")
raise SystemExit(proc.returncode)
