#!/usr/bin/env python3
import json
import sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv) > 1 else "/root/gptimage/data/runlogs/webshare_cf_scan_latest.json")
d = json.loads(p.read_text(encoding="utf-8"))
print(json.dumps({k: d.get(k) for k in ["finished_at", "good_count", "req_ok_count", "scanned", "good", "last_good"] if k in d}, ensure_ascii=False, indent=2)[:4000])
