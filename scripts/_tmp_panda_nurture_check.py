#!/usr/bin/env python3
"""One-shot nurture / humanlike diagnostics (run inside chatgpt2api container)."""
from __future__ import annotations

import glob
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

ROOT = os.environ.get("GPTIMAGE_ROOT", "/app")
CONFIG = os.path.join(ROOT, "config.json")
ACCOUNTS = os.path.join(ROOT, "data", "accounts.json")
LOG_DIR = os.path.join(ROOT, "data", "logs")


def main() -> None:
    c = json.load(open(CONFIG, encoding="utf-8"))
    tn = c.get("text_nurture") or {}
    print("=== text_nurture ===")
    print(json.dumps(tn, ensure_ascii=False, indent=2))
    print("text_chat_persist_history", c.get("text_chat_persist_history"))
    print("text_chat_reuse_conversation", c.get("text_chat_reuse_conversation"))

    for key in (
        "text_chat_persist_history",
        "text_chat_reuse_conversation",
        "account_warmup",
        "scheduler",
        "workload_policy",
        "proactive_refresh",
    ):
        if key in c:
            print(f"=== {key} ===")
            print(json.dumps(c[key], ensure_ascii=False, indent=2)[:2500])

    if os.path.exists(ACCOUNTS):
        raw = json.load(open(ACCOUNTS, encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("accounts", [])
        total = len(items)
        persist = sum(1 for a in items if a.get("chat_persist_history"))
        reuse = sum(1 for a in items if a.get("chat_reuse_conversation"))
        text_cid = sum(1 for a in items if a.get("text_conversation_id"))
        sched = sum(
            1
            for a in items
            if str(a.get("panda_receive_state") or "")
            in {"verified_ready", "verified", "local_verified"}
        )
        ready_text = sum(
            1
            for a in items
            if a.get("chat_persist_history")
            and str(a.get("status") or "") not in {"禁用", "异常"}
        )
        print(
            f"=== accounts ===\n"
            f"total={total} schedulable={sched} persist={persist} reuse={reuse} "
            f"text_cid={text_cid} nurture_ready={ready_text}"
        )
        status_ctr = Counter(str(a.get("status") or "?") for a in items)
        receive_ctr = Counter(str(a.get("panda_receive_state") or "?") for a in items)
        print("status", dict(status_ctr.most_common(8)))
        print("receive_state", dict(receive_ctr.most_common(8)))
        # traffic dialogues_nurture last 7 days
        nurture_days = Counter()
        for a in items:
            traffic = a.get("traffic") or {}
            if not isinstance(traffic, dict):
                continue
            for day, blob in traffic.items():
                if isinstance(blob, dict):
                    n = int(blob.get("dialogues_nurture") or 0)
                    if n:
                        nurture_days[day] += n
        if nurture_days:
            print("dialogues_nurture by day", dict(sorted(nurture_days.items())[-14:]))
        else:
            print("dialogues_nurture by day: NONE")

    # llm_ops nurture events (last 48h)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    outcomes = Counter()
    per_day = Counter()
    scanned = 0
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "**", "*.log"), recursive=True)):
        try:
            for line in open(path, encoding="utf-8", errors="ignore"):
                if "llm_ops" not in line or "nurture" not in line:
                    continue
                scanned += 1
                if '"outcome": "ok"' in line or '"outcome":"ok"' in line:
                    outcomes["ok"] += 1
                elif '"outcome": "error"' in line or '"outcome":"error"' in line:
                    outcomes["error"] += 1
                else:
                    outcomes["other"] += 1
                # crude day bucket from log prefix if present
                if len(line) > 10 and line[0:4].isdigit():
                    per_day[line[0:10]] += 1
        except OSError:
            pass
    print("=== nurture llm_ops (all logs) ===")
    print(f"lines={scanned} outcomes={dict(outcomes)} per_day_sample={dict(list(per_day.items())[-7:])}")

    tick_errors = 0
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "**", "*.log"), recursive=True)):
        try:
            for line in open(path, encoding="utf-8", errors="ignore"):
                if "text_nurture_tick_error" in line:
                    tick_errors += 1
                    if tick_errors <= 5:
                        print("tick_error:", line.strip()[:300])
        except OSError:
            pass
    print(f"text_nurture_tick_error count={tick_errors}")

    # try live service status via import
    try:
        import sys

        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from services.text_nurture_service import text_nurture_service
        from services.config import config as cfg

        print("=== runtime status ===")
        print(json.dumps(text_nurture_service.status(), ensure_ascii=False, indent=2))
        print("text_chat_persist_history", getattr(cfg, "text_chat_persist_history", None))
        print("scheduler enabled", cfg.get_scheduler_settings().get("enabled"))
        print("text_min_interval_sec", cfg.get_scheduler_settings().get("text_min_interval_sec"))
    except Exception as exc:
        print("=== runtime import failed ===", exc)


if __name__ == "__main__":
    main()
