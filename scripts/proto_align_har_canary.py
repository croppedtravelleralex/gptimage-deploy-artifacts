#!/usr/bin/env python3
"""PROTO-ALIGN HAR canary: compare local chat body shape vs optional Web HAR.

Usage:
  python scripts/proto_align_har_canary.py
  python scripts/proto_align_har_canary.py --har path/to/chatgpt.har
  python scripts/proto_align_har_canary.py --email a@b.com --enable-persist

Does NOT call upstream by default. Enabling persist only updates local account flags.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.protocol.chatgpt_web_request import (  # noqa: E402
    build_chat_body,
    oai_language_for_timezone,
    timezone_offset_min,
)


EXPECTED_CHAT_KEYS = {
    "action",
    "messages",
    "model",
    "parent_message_id",
    "conversation_mode",
    "history_and_training_disabled",
    "timezone",
    "timezone_offset_min",
    "client_contextual_info",
    "force_use_sse",
}


def _extract_har_conversation_bodies(har_path: Path) -> list[dict[str, Any]]:
    data = json.loads(har_path.read_text(encoding="utf-8"))
    entries = (((data.get("log") or {}).get("entries")) or [])
    bodies: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        req = entry.get("request") or {}
        url = str(req.get("url") or "")
        if "/backend-api/conversation" not in url and "/backend-api/f/conversation" not in url:
            continue
        if "prepare" in url:
            continue
        post = req.get("postData") or {}
        text = str(post.get("text") or "")
        if not text:
            continue
        try:
            body = json.loads(text)
        except Exception:
            continue
        if isinstance(body, dict) and body.get("action") == "next":
            bodies.append({"url": url, "body": body})
    return bodies


def _diff_keys(local: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
    lk, rk = set(local), set(remote)
    return {
        "only_local": sorted(lk - rk),
        "only_remote": sorted(rk - lk),
        "shared": sorted(lk & rk),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="PROTO-ALIGN HAR canary")
    parser.add_argument("--har", type=str, default="", help="Optional ChatGPT Web HAR path")
    parser.add_argument("--timezone", type=str, default="Asia/Singapore")
    parser.add_argument("--persist", action="store_true", help="Build with history enabled")
    parser.add_argument("--email", type=str, default="", help="Account email for persist flag update")
    parser.add_argument("--enable-persist", action="store_true", help="Set chat_persist_history on account")
    parser.add_argument("--out", type=str, default="", help="Write JSON report path")
    args = parser.parse_args()

    tz = str(args.timezone or "Asia/Singapore")
    local = build_chat_body(
        [{"role": "user", "content": "canary ping"}],
        "auto",
        timezone=tz,
        history_and_training_disabled=not bool(args.persist),
        conversation_id="conv-canary" if args.persist else "",
        parent_message_id="parent-canary" if args.persist else "",
        contextual_jitter=False,
    )
    report: dict[str, Any] = {
        "local_shape": {
            "keys": sorted(local.keys()),
            "timezone": local.get("timezone"),
            "timezone_offset_min": local.get("timezone_offset_min"),
            "history_and_training_disabled": local.get("history_and_training_disabled"),
            "has_conversation_id": "conversation_id" in local,
            "oai_language_hint": oai_language_for_timezone(tz),
            "offset_expected": timezone_offset_min(tz),
            "missing_expected_keys": sorted(EXPECTED_CHAT_KEYS - set(local.keys())),
        },
        "har": None,
        "account_update": None,
        "verdict": "local_ok",
    }

    if args.har:
        har_path = Path(args.har)
        bodies = _extract_har_conversation_bodies(har_path)
        if not bodies:
            report["har"] = {"error": "no conversation POST bodies found"}
            report["verdict"] = "har_empty"
        else:
            remote = bodies[0]["body"]
            report["har"] = {
                "count": len(bodies),
                "first_url": bodies[0]["url"],
                "remote_history_disabled": remote.get("history_and_training_disabled"),
                "remote_timezone": remote.get("timezone"),
                "diff": _diff_keys(local, remote),
            }
            missing_remote = EXPECTED_CHAT_KEYS - set(remote.keys())
            report["har"]["missing_expected_on_remote"] = sorted(missing_remote)
            report["verdict"] = "compared"

    if args.enable_persist and args.email:
        from services.account_service import account_service

        updated = None
        for item in account_service.list_accounts():
            if str(item.get("email") or "").strip().lower() == str(args.email).strip().lower():
                token = str(item.get("access_token") or "")
                updated = account_service.update_account(
                    token,
                    {
                        "chat_persist_history": True,
                        "chat_reuse_conversation": True,
                    },
                    quiet=True,
                )
                break
        report["account_update"] = {
            "email": args.email,
            "ok": bool(updated),
            "chat_persist_history": bool((updated or {}).get("chat_persist_history")),
            "chat_reuse_conversation": bool((updated or {}).get("chat_reuse_conversation")),
        }
        if not updated:
            report["verdict"] = "account_not_found"

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report.get("verdict") in {"local_ok", "compared"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
