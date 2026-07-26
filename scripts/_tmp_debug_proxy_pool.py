#!/usr/bin/env python3
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.proxy_cf_failover import _parse_pool_line
from services.proxy_quarantine import is_gpt_unavailable_proxy, proxy_endpoint_key

pool = Path(sys.argv[1])
lines = [ln for ln in pool.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip() and not ln.strip().startswith("#")]
parsed = [_parse_pool_line(ln) for ln in lines]
ok_urls = [u for u in parsed if u]
blocked = [u for u in ok_urls if is_gpt_unavailable_proxy(u)]
print(json.dumps({
    "lines": len(lines),
    "parsed": len(ok_urls),
    "blocked": len(blocked),
    "sample_parsed": ok_urls[:2],
    "sample_blocked_keys": [proxy_endpoint_key(u) for u in blocked[:5]],
}, ensure_ascii=False))
