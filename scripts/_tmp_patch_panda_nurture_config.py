#!/usr/bin/env python3
"""Patch chatgpt2api config.json for text nurture rollout."""
from __future__ import annotations

import json
from pathlib import Path

CONFIG = Path("/app/config.json")


def main() -> None:
    c = json.loads(CONFIG.read_text(encoding="utf-8"))
    c["text_chat_persist_history"] = True
    c["text_chat_reuse_conversation"] = True
    nurture = dict(c.get("text_nurture") or {})
    nurture.update(
        {
            "enabled": True,
            "worker_enabled": True,
            "auto_enqueue": True,
            "auto_enqueue_every_sec": 120,
            "poll_interval_sec": 3,
            "max_per_hour": 70,
            "max_per_account_per_day": 8,
            "turns_per_session": 3,
            "turn_gap_sec": 8,
            "require_persist_history": True,
            "auto_enqueue_rotate_accounts": True,
        }
    )
    c["text_nurture"] = nurture
    CONFIG.write_text(json.dumps(c, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "text_nurture": nurture}, ensure_ascii=False))


if __name__ == "__main__":
    main()
