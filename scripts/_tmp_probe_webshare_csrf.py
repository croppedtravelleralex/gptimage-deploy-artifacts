#!/usr/bin/env python3
"""Probe Webshare pool for ChatGPT CSRF 200 (registration-grade)."""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from urllib.parse import urlparse


def normalize_proxy(raw: str) -> str:
    line = str(raw or "").strip()
    if not line:
        raise ValueError("empty proxy")
    if "://" in line:
        return line
    parts = line.split(":")
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        return f"http://{user}:{password}@{host}:{port}"
    raise ValueError(f"bad proxy line: {line[:40]}")


def proxy_host(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return str(parsed.hostname or "")


def proxy_endpoint(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = str(parsed.hostname or "")
    return f"{host}:{parsed.port}" if parsed.port else host


def probe_csrf(proxy: str) -> dict:
    from curl_cffi import requests as crequests

    out = {"proxy": proxy_endpoint(proxy), "host": proxy_host(proxy), "ok": False}
    try:
        session = crequests.Session(impersonate="chrome", proxies={"http": proxy, "https": proxy}, timeout=25)
        r = session.get("https://chatgpt.com/api/auth/csrf")
        data = r.json() if r.text else {}
        token = str(data.get("csrfToken") or "").strip() if isinstance(data, dict) else ""
        out["status"] = r.status_code
        out["ok"] = r.status_code == 200 and bool(token)
        if not out["ok"]:
            out["error"] = f"csrf_{r.status_code}"
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:120]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--exclude-hosts", default="")
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
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
        host = proxy_host(proxy).lower()
        if host in exclude:
            continue
        candidates.append(proxy)
    good: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = {ex.submit(probe_csrf, p): p for p in candidates}
        for fut in as_completed(futures):
            row = fut.result()
            if row.get("ok"):
                good.append(row)
                if len(good) >= args.count:
                    for pending in futures:
                        pending.cancel()
                    break
    print(json.dumps({"scanned": len(candidates), "good": good[: args.count]}, ensure_ascii=False, indent=2))
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
