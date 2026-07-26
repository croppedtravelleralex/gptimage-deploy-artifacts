import json
import subprocess

raw = subprocess.check_output(
    ["ssh", "panda", "python3 /tmp/inspect_logs.py"],
    text=True,
    errors="replace",
)
