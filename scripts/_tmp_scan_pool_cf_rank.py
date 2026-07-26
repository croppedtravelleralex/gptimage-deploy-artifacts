#!/usr/bin/env python3
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, "/app")
from services.proxy_cf_probe import probe_proxy_cf

exclude = {h.strip().lower() for h in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if h.strip()}
pool = Path("/app/data/runlogs/webshare_100_proxies.secret.txt")
proxies = []
for line in pool.read_text().splitlines():
    line = line.strip()
    if not line:
        continue
    parts = line.split(":")
    if parts[0].lower() in exclude:
        continue
    proxies.append(f"http://{parts[2]}:{':'.join(parts[3:])}@{parts[0]}:{parts[1]}")

rows = []

def run(p: str) -> dict:
    row = probe_proxy_cf(p, timeout=30.0)
    row["proxy_url"] = p
    return row

with ThreadPoolExecutor(max_workers=12) as ex:
    for fut in as_completed([ex.submit(run, p) for p in proxies]):
        rows.append(fut.result())

rows.sort(key=lambda r: (not r.get("requirements_ok"), r.get("cf_classification") or "z"))
print(json.dumps({
    "scanned": len(proxies),
    "ok_count": sum(1 for r in rows if r.get("ok")),
    "req_ok_count": sum(1 for r in rows if r.get("requirements_ok")),
    "best": rows[:8],
}, ensure_ascii=False, indent=2))
