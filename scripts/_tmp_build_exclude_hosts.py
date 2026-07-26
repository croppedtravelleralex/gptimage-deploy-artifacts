#!/usr/bin/env python3
"""Build exclude-hosts list from Panda pool + quarantine."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.proxy_quarantine import list_gpt_unavailable_endpoints


def main() -> int:
    pool = json.loads(Path("data/runlogs/panda_pool_snapshot.json").read_text(encoding="utf-8"))
    hosts = set(pool.get("hosts") or [])
    quarantine_path = Path("data/runlogs/gpt_unavailable_proxies.json")
    if quarantine_path.is_file():
        hosts |= list_gpt_unavailable_endpoints(quarantine_path)
    emails = set(e.lower() for e in pool.get("emails") or [])
    print(json.dumps({"exclude_hosts": sorted(hosts), "used_emails": sorted(emails)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
