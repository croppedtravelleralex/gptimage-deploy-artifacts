#!/usr/bin/env python3
"""Pick first N fresh Webshare lines not in exclude hosts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.outlook_camoufox_stable_register import normalize_proxy, proxy_host


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--exclude-hosts", required=True)
    ap.add_argument("--count", type=int, default=2)
    args = ap.parse_args()
    exclude = {h.strip().lower() for h in args.exclude_hosts.split(",") if h.strip()}
    picked = []
    for raw in Path(args.pool).read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            proxy = normalize_proxy(line)
        except Exception:
            continue
        host = proxy_host(proxy).lower()
        if host in exclude:
            continue
        picked.append({"host": host, "proxy": proxy})
        exclude.add(host)
        if len(picked) >= args.count:
            break
    print(json.dumps({"picked": picked}, ensure_ascii=False, indent=2))
    return 0 if picked else 1


if __name__ == "__main__":
    raise SystemExit(main())
