"""T+0 live canary: /me + fp persist check + egress re-measure. No secrets in output."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from services.proxy_health import measure_proxy_egress_ip

TOKEN_HASH = "40de2f332c0d3fd4"
OUT = Path("/app/data/runlogs/account-identity-remediation-canary-40de2f332c0d3fd4")


def th(t: object) -> str:
    return hashlib.sha256(str(t or "").encode("utf-8")).hexdigest()[:16]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    target = None
    token = ""
    for a in account_service.list_accounts():
        if th(a.get("access_token")) == TOKEN_HASH:
            target = a
            token = str(a.get("access_token") or "")
            break
    if not target or not token:
        raise SystemExit("canary missing")

    before_fp = hashlib.sha256(
        json.dumps(target.get("fp") or {}, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    proxy = str(target.get("proxy") or "")
    egress_samples = []
    for _ in range(3):
        sample = measure_proxy_egress_ip(proxy, timeout=20.0)
        egress_samples.append(
            {
                "ok": sample.get("ok"),
                "egress_hash": sample.get("egress_hash"),
                "loc": sample.get("loc"),
                "elapsed_sec": sample.get("elapsed_sec"),
                "error": sample.get("error"),
            }
        )
        time.sleep(1.0)

    me_ok = False
    me_error = ""
    fp_after = ""
    try:
        backend = OpenAIBackendAPI(access_token=token)
        info = backend.get_user_info()
        me_ok = bool(info)
        after = account_service.get_account(token) or {}
        fp_after = hashlib.sha256(
            json.dumps(after.get("fp") or {}, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        backend.close()
    except Exception as exc:  # noqa: BLE001
        me_error = f"{type(exc).__name__}: {exc}"[:300]

    hashes = [s.get("egress_hash") for s in egress_samples if s.get("ok") and s.get("egress_hash")]
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "token_hash": TOKEN_HASH,
        "me_ok": me_ok,
        "me_error": me_error,
        "fp_before": before_fp,
        "fp_after": fp_after,
        "fp_stable": bool(fp_after) and fp_after == before_fp,
        "egress_samples": egress_samples,
        "egress_stable": len(set(hashes)) == 1 and len(hashes) == 3,
        "stored_egress_hash": target.get("proxy_egress_hash"),
        "egress_matches_stored": bool(hashes) and hashes[0] == target.get("proxy_egress_hash"),
        "schedulable": account_service._is_image_account_schedulable(
            account_service.get_account(token) or target
        ),
    }
    path = OUT / "t0-live.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in payload if k != "egress_samples"}, ensure_ascii=False))
    print("wrote", path)
    return 0 if payload["me_ok"] and payload["egress_stable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
