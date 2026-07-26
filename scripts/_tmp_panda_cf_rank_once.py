#!/usr/bin/env python3
"""One-shot CF rank scan (run inside Panda container)."""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, "/app")
from services.proxy_cf_probe import probe_proxy_cf  # noqa: E402


def main() -> int:
    exclude = {h.strip().lower() for h in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if h.strip()}
    pool = Path("/app/data/runlogs/webshare_100_proxies.secret.txt")
    proxies: list[str] = []
    for line in pool.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if parts[0].lower() in exclude:
            continue
        proxies.append(f"http://{parts[2]}:{':'.join(parts[3:])}@{parts[0]}:{parts[1]}")

    rows: list[dict] = []

    def run(proxy: str) -> dict:
        row = probe_proxy_cf(proxy, timeout=30.0)
        row["host"] = proxy.split("@")[-1].split(":")[0]
        return row

    with ThreadPoolExecutor(max_workers=12) as ex:
        for fut in as_completed([ex.submit(run, p) for p in proxies]):
            rows.append(fut.result())

    rows.sort(key=lambda r: (not r.get("requirements_ok"), r.get("cf_classification") or "z"))
    good = [r for r in rows if r.get("ok") and r.get("cf_classification") != "cf403"]
    print(
        json.dumps(
            {
                "scanned": len(proxies),
                "good_count": len(good),
                "good": good[:5],
                "best_non_ok": [
                    {
                        "host": r.get("host"),
                        "home": r.get("home_status"),
                        "req": r.get("requirements_status"),
                        "cf": r.get("cf_classification"),
                    }
                    for r in rows[:10]
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
