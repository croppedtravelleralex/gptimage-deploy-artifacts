#!/usr/bin/env python3
"""Canary /me + egress gate before workload live allowlist."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_HASH = "40de2f332c0d3fd4"


def _token_hash(token: object) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16]


def main() -> int:
    from services.account_service import account_service
    from services.register.proxy_health import measure_proxy_egress_ip

    token_hash = str(os.environ.get("CANARY_TOKEN_HASH") or (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HASH)).strip()
    out_dir = ROOT / "data" / "runlogs" / f"account-identity-remediation-canary-{token_hash}"
    out_dir.mkdir(parents=True, exist_ok=True)

    account = None
    for item in account_service.list_accounts():
        if _token_hash(item.get("access_token")) == token_hash:
            account = item
            break
    if account is None:
        payload = {"ok": False, "error": "canary_not_found", "token_hash": token_hash}
        (out_dir / "me-gate.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps(payload))
        return 1

    access_token = str(account.get("access_token") or "")
    proxy = str(account.get("proxy") or "").strip()
    me_ok = False
    me_error = ""
    me_status = None
    try:
        refreshed = account_service.fetch_remote_info(access_token, "canary_me_gate")
        me_ok = isinstance(refreshed, dict) and bool(refreshed.get("access_token") or refreshed.get("email"))
        if not me_ok:
            me_error = "fetch_remote_info_empty"
    except Exception as exc:
        me_error = f"{type(exc).__name__}: {exc}"
        text = str(exc).lower()
        if "403" in text:
            me_status = 403
        elif "401" in text:
            me_status = 401

    egress_runs = []
    egress_ok = True
    hashes: set[str] = set()
    for _ in range(3):
        if not proxy:
            egress_runs.append({"ok": False, "error": "missing_proxy"})
            egress_ok = False
            break
        result = measure_proxy_egress_ip(proxy, timeout=20.0)
        egress_runs.append(
            {
                "ok": bool(result.get("ok")),
                "egress_hash": result.get("egress_hash"),
                "error": result.get("error"),
            }
        )
        if not result.get("ok"):
            egress_ok = False
        else:
            hashes.add(str(result.get("egress_hash") or ""))
    if len(hashes) > 1:
        egress_ok = False

    live_eligible = bool(me_ok and egress_ok and me_status not in {401, 403})
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "token_hash": token_hash,
        "me_ok": me_ok,
        "me_status": me_status,
        "me_error": me_error[:300] if me_error else "",
        "egress_ok": egress_ok,
        "egress_runs": egress_runs,
        "live_eligible": live_eligible,
    }
    name = "me-gate-ok.json" if live_eligible else "me-gate-fail.json"
    (out_dir / name).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "me-gate.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if live_eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
