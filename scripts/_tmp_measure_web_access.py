#!/usr/bin/env python3
"""Measure static asset sizes + optional production URL TTFB."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIST = ROOT / "web_dist"
REPORT = ROOT / "docs" / "captures" / "spa" / "FE-access-speed-report.json"


def static_report() -> dict:
    static = WEB_DIST / "_next" / "static"
    files = [p for p in static.rglob("*") if p.is_file()] if static.is_dir() else []
    total_kb = sum(p.stat().st_size for p in files) / 1024
    routes = {}
    for html in WEB_DIST.rglob("index.html"):
        route = "/" if html.parent == WEB_DIST else "/" + html.parent.relative_to(WEB_DIST).as_posix()
        routes[route] = {"html_kb": round(html.stat().st_size / 1024, 1)}
    return {
        "static_total_kb": round(total_kb, 1),
        "static_files": len(files),
        "routes": routes,
    }


def fetch_ttfb(url: str) -> dict:
    t0 = time.perf_counter()
    req = urllib.request.Request(url, method="GET", headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read(4096)
            code = resp.status
    except Exception as exc:
        return {"url": url, "ok": False, "error": str(exc)}
    ms = int((time.perf_counter() - t0) * 1000)
    return {"url": url, "ok": True, "status": code, "ttfb_ms": ms}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod", action="store_true", help="also probe gptimage.relai.asia routes")
    args = ap.parse_args()
    payload = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "local": static_report()}
    if args.prod:
        base = "https://gptimage.relai.asia"
        paths = ["/accounts/", "/logs/", "/chat/", "/ops/", "/image-manager/", "/_next/static/chunks/"]
        payload["production"] = [fetch_ttfb(base + p) for p in paths]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"WROTE {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
