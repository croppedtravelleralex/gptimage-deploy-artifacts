#!/usr/bin/env python3
from pathlib import Path
from services.proxy_cf_failover import load_proxy_pool, _parse_pool_line
from services.proxy_quarantine import is_gpt_unavailable_proxy
from services.proxy_cf_failover import proxy_endpoint_key

paths = [
    "/app/data/runlogs/webshare_100_proxies.secret.txt",
    "/app/data/runlogs/webshare_good_csrf_200.secret.txt",
    "/app/data/runlogs/spa_repro/webshare_pool_100.txt",
]
for p in paths:
    path = Path(p)
    if not path.is_file():
        print(p, "MISSING")
        continue
    raw = 0
    clean = len(load_proxy_pool(path))
    quar = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        url = _parse_pool_line(line)
        if not url:
            continue
        raw += 1
        if is_gpt_unavailable_proxy(url):
            quar += 1
    print(p, "raw_lines", raw, "clean", clean, "quarantined", quar)
