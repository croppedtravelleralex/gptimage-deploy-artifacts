#!/usr/bin/env python3
"""Panda canary observation snapshot (T+0 / T+1h / ...).

Reads local SQLite + health; never prints secrets. Writes t{label}.json under --out-dir.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sha(value: object, n: int = 16) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:n]


def _token_hash(token: object) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16]


def _load_accounts(db: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("select data from accounts").fetchall()
    out: list[dict[str, Any]] = []
    for (raw,) in rows:
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _account_view(account: dict[str, Any]) -> dict[str, Any]:
    fp = account.get("fp")
    return {
        "token_hash": _token_hash(account.get("access_token")),
        "status": account.get("status"),
        "panda_receive_state": account.get("panda_receive_state"),
        "proxy_present": bool(str(account.get("proxy") or "").strip()),
        "proxy_binding_hash": account.get("proxy_binding_hash") or _sha(str(account.get("proxy") or ""), 16),
        "proxy_egress_hash": account.get("proxy_egress_hash"),
        "proxy_egress_ip": account.get("proxy_egress_ip"),
        "registration_proxy_hash": account.get("registration_proxy_hash"),
        "fp_hash": _sha(fp, 16) if fp else "",
        "fp_origin": account.get("fp_origin"),
        "traffic_total_bytes": account.get("traffic_total_bytes"),
        "success": int(account.get("success") or 0),
        "fail": int(account.get("fail") or 0),
        "invalid_count": int(account.get("invalid_count") or 0),
    }


def _health(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True, help="t0 / t1h / t6h / t24h / t72h")
    parser.add_argument("--db", default="/root/gptimage/data/accounts.db")
    parser.add_argument("--health-url", default="http://127.0.0.1:8012/health?format=json")
    parser.add_argument("--token-hash", default="", help="optional canary token hash filter")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    accounts = _load_accounts(Path(args.db))
    views = [_account_view(a) for a in accounts]
    if args.token_hash:
        views = [v for v in views if v["token_hash"] == args.token_hash]
    health = _health(args.health_url)
    payload = {
        "label": args.label,
        "ts": datetime.now(timezone.utc).isoformat(),
        "health": {
            "healthy": health.get("healthy"),
            "accounts": health.get("accounts"),
            "image_inflight_count": (health.get("accounts") or {}).get("image_inflight_count")
            if isinstance(health.get("accounts"), dict)
            else health.get("image_inflight_count"),
        },
        "accounts": views,
        "totals": {
            "n": len(views),
            "with_fp": sum(1 for v in views if v.get("fp_hash")),
            "with_egress": sum(1 for v in views if v.get("proxy_egress_hash")),
            "with_proxy": sum(1 for v in views if v.get("proxy_present")),
        },
    }
    path = out_dir / f"{args.label}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"wrote": str(path), "totals": payload["totals"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
