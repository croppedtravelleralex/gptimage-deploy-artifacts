#!/usr/bin/env python3
"""Isolate non-canary accounts that share proxy_binding_hash (canary window)."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.account_identity import proxy_binding_hash
from services.account_service import account_service


def _token_hash(token: object) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-hash", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="exit non-zero if any peer shares binding and is not identity_isolated",
    )
    parser.add_argument("--restore", action="store_true", help="restore from rollback.json in --out")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    if args.restore:
        rollback = json.loads((args.out / "peer-isolation-rollback.json").read_text(encoding="utf-8"))
        restored = 0
        for item in rollback.get("peers", []):
            token = ""
            for acc in account_service.list_accounts():
                if _token_hash(acc.get("access_token")) == item["token_hash"]:
                    token = str(acc.get("access_token") or "")
                    break
            if not token:
                continue
            account_service.update_account(
                token,
                {"panda_receive_state": item.get("before_receive_state")},
                quiet=True,
            )
            restored += 1
        print(json.dumps({"ok": True, "restored": restored}))
        return 0

    canary = None
    for acc in account_service.list_accounts():
        if _token_hash(acc.get("access_token")) == args.token_hash:
            canary = acc
            break
    if not canary:
        raise SystemExit("canary not found")
    binding = str(canary.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(canary.get("proxy"))
    peers = []
    for acc in account_service.list_accounts():
        th = _token_hash(acc.get("access_token"))
        if th == args.token_hash:
            continue
        other = str(acc.get("proxy_binding_hash") or "").strip() or proxy_binding_hash(acc.get("proxy"))
        if other and other == binding and str(acc.get("status") or "") != "禁用":
            peers.append(
                {
                    "token_hash": th,
                    "before_receive_state": acc.get("panda_receive_state"),
                    "status": acc.get("status"),
                }
            )
    doc = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "canary_token_hash": args.token_hash,
        "binding": binding,
        "peers": peers,
    }
    (args.out / "peer-isolation-plan.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    if args.preflight:
        bad = [
            item
            for item in peers
            if str(item.get("before_receive_state") or "").strip().lower()
            not in {"identity_isolated", "rejected"}
        ]
        result = {
            "ok": len(bad) == 0,
            "preflight": True,
            "peer_count": len(peers),
            "not_isolated": len(bad),
            "peers": bad,
            "out": str(args.out),
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if not bad else 2
    if not args.apply:
        print(json.dumps({"ok": True, "dry_run": True, "peer_count": len(peers), "out": str(args.out)}))
        return 0
    (args.out / "peer-isolation-rollback.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    changed = 0
    for item in peers:
        token = ""
        for acc in account_service.list_accounts():
            if _token_hash(acc.get("access_token")) == item["token_hash"]:
                token = str(acc.get("access_token") or "")
                break
        if not token:
            continue
        account_service.update_account(
            token,
            {"panda_receive_state": "identity_isolated"},
            quiet=True,
        )
        changed += 1
    print(json.dumps({"ok": True, "isolated": changed, "out": str(args.out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
