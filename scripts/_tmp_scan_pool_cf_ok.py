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
    host = parts[0].lower()
    if host in exclude:
        continue
    proxy = f"http://{parts[2]}:{':'.join(parts[3:])}@{parts[0]}:{parts[1]}"
    proxies.append(proxy)

good = []

def run(p: str) -> dict:
    row = probe_proxy_cf(p, timeout=35.0)
    row["proxy_url"] = p
    return row

with ThreadPoolExecutor(max_workers=12) as ex:
    futures = [ex.submit(run, p) for p in proxies]
    for fut in as_completed(futures):
        row = fut.result()
        if row.get("ok"):
            good.append(row)
            if len(good) >= 2:
                break

print(json.dumps({"scanned": len(proxies), "good": good}, ensure_ascii=False, indent=2))
