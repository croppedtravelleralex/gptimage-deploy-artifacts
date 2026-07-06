#!/usr/bin/env python3
import json
import subprocess
import time
import sys

for i in range(36):
    p = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=15", "panda", "curl -sS http://127.0.0.1:8012/health?format=json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    h = json.loads(p.stdout)
    a = h.get("accounts") or {}
    inflight = int(a.get("image_inflight_count") or 0)
    dispatchable = int(a.get("dispatchable_candidate_count") or 0)
    print(f"poll {i}: inflight={inflight} dispatchable={dispatchable}", flush=True)
    if inflight == 0 and dispatchable >= 80:
        sys.exit(0)
    time.sleep(5)
sys.exit(1)
