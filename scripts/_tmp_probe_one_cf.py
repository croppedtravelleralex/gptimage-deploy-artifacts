#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")
from services.proxy_cf_probe import probe_proxy_cf

pool = Path("/app/data/runlogs/webshare_100_proxies.secret.txt")
lines = [l.strip() for l in pool.read_text().splitlines() if l.strip()]
idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
line = lines[idx % len(lines)]
parts = line.split(":")
proxy = f"http://{parts[2]}:{':'.join(parts[3:])}@{parts[0]}:{parts[1]}"
print(json.dumps({"line": line.split(':')[0], **probe_proxy_cf(proxy, timeout=45)}, ensure_ascii=False, indent=2))
