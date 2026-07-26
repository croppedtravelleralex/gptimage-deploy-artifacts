#!/usr/bin/env python3
"""Find Webshare nodes passing probe_proxy_cf (not CF403 on requirements)."""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts._tmp_probe_webshare_csrf import normalize_proxy, proxy_host


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--exclude-hosts", default="")
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    from services.proxy_cf_probe import probe_proxy_cf

    exclude = {h.strip().lower() for h in args.exclude_hosts.split(",") if h.strip()}
    candidates: list[str] = []
    for raw in Path(args.pool).read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            proxy = normalize_proxy(line)
        except Exception:
            continue
        if proxy_host(proxy).lower() in exclude:
            continue
        candidates.append(proxy)

    good: list[dict] = []

    def run_one(proxy: str) -> dict:
        row = probe_proxy_cf(proxy, timeout=35.0)
        row["proxy"] = proxy_host(proxy)
        row["endpoint"] = row.get("proxy_endpoint")
        return row

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = {ex.submit(run_one, p): p for p in candidates}
        for fut in as_completed(futures):
            row = fut.result()
            cf = str(row.get("cf_classification") or "")
            if row.get("ok") and cf not in {"cf403"}:
                good.append(row)
                if len(good) >= args.count:
                    break
    print(json.dumps({"scanned": len(candidates), "good": good[: args.count]}, ensure_ascii=False, indent=2))
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
