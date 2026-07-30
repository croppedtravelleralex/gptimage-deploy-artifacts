#!/usr/bin/env python3
"""Diagnose conc10 observability: quota marks, phase_timings, schedule_trace, IMG-018 config."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def _load_call_logs(prefix: str, limit: int = 300) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        from services.log_service import log_service

        items = log_service.list_logs(log_type="call", limit=limit) or []
    except Exception:
        import urllib.request

        from services.config import config

        auth = str(config.data.get("auth-key") or config.data.get("auth_key") or "").strip()
        for base in ("http://127.0.0.1:80", "http://127.0.0.1:8012"):
            try:
                req = urllib.request.Request(
                    f"{base}/api/logs?type=call&limit={limit}",
                    headers={"Authorization": f"Bearer {auth}"},
                )
                payload = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))
                items = payload.get("items") if isinstance(payload.get("items"), list) else []
                if items:
                    break
            except Exception:
                continue
    rows: list[dict[str, Any]] = []
    for item in items:
        blob = json.dumps(item, ensure_ascii=False)
        if prefix not in blob:
            continue
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        rows.append(
            {
                "time": item.get("time"),
                "summary": item.get("summary"),
                "status": detail.get("status"),
                "account_email": detail.get("account_email"),
                "phase_timings_ms": detail.get("phase_timings_ms"),
                "schedule_trace_engine": (detail.get("schedule_trace") or {}).get("engine")
                if isinstance(detail.get("schedule_trace"), dict)
                else None,
                "schedule_trace_events": (detail.get("schedule_trace") or {}).get("event_count")
                if isinstance(detail.get("schedule_trace"), dict)
                else None,
                "failure_phase": detail.get("failure_phase"),
                "failure_reason": detail.get("failure_reason"),
                "task_key": detail.get("task_key"),
            }
        )
    return rows


def _tasks_from_db(db_path: Path, prefix: str) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {row[1] for row in conn.execute("pragma table_info(image_tasks)").fetchall()}
        if "task_key" in cols:
            where = "task_key like ?"
        elif "id" in cols:
            where = "id like ?"
        else:
            return []
        select_cols = [c for c in (
            "task_key", "id", "status", "duration_ms", "phase_timings_ms",
            "schedule_trace", "failure_phase", "failure_reason", "pipeline_phase", "payload",
        ) if c in cols]
        rows = conn.execute(
            f"select {', '.join(select_cols)} from image_tasks where {where} order by 1",
            (f"{prefix}%",),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("phase_timings_ms", "schedule_trace"):
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                try:
                    item[key] = json.loads(raw)
                except Exception:
                    pass
        out.append(item)
    return out


def _account_snapshot(emails: list[str]) -> list[dict[str, Any]]:
    from services.account_service import account_service

    email_set = set(emails)
    rows: list[dict[str, Any]] = []
    for _tok, acc in account_service._accounts.items():
        email = str(acc.get("email") or "").strip()
        if email not in email_set:
            continue
        rows.append(
            {
                "email": email,
                "quota": acc.get("quota"),
                "success": acc.get("success"),
                "fail": acc.get("fail"),
                "last_used_at": acc.get("last_used_at"),
                "image_quota_unknown": acc.get("image_quota_unknown"),
            }
        )
    rows.sort(key=lambda r: r["email"])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--report", default="")
    ap.add_argument("--db", default="/app/data/image_tasks.db")
    args = ap.parse_args()

    from services.config import config

    emails: list[str] = []
    if args.report:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        for item in report.get("submits") or []:
            email = str(item.get("preferred_account_email") or "").strip()
            if email:
                emails.append(email)

    cfg = {
        "newapi_image_attempt_budget_secs": config.data.get("newapi_image_attempt_budget_secs"),
        "ss_stage_wall_timeout_secs": config.data.get("ss_stage_wall_timeout_secs"),
        "generation_poll_timeout_secs": config.data.get("generation_poll_timeout_secs"),
        "timeout_pending_max_attempts": config.get_image_task_queue_settings().get("timeout_pending_max_attempts"),
        "schedule_trace_enabled": config.get_image_pipeline_settings().get("schedule_trace_enabled"),
        "image_release_account_after_sse": config.data.get("image_release_account_after_sse"),
    }
    try:
        from services.image_pipeline import schedule_trace

        cfg["schedule_trace_runtime_enabled"] = schedule_trace.enabled()
    except Exception as exc:
        cfg["schedule_trace_runtime_enabled"] = f"error:{exc}"

    call_logs = _load_call_logs(args.prefix)
    phase_logs = [r for r in call_logs if r.get("phase_timings_ms")]
    trace_logs = [r for r in call_logs if r.get("schedule_trace_engine")]

    out = {
        "prefix": args.prefix,
        "config": cfg,
        "status_api_note": "_status_task omits phase_timings_ms; /api/image-tasks/status will not show SSE/poll breakdown",
        "db_tasks": _tasks_from_db(Path(args.db), args.prefix),
        "call_log_summary": {
            "matched": len(call_logs),
            "with_phase_timings": len(phase_logs),
            "with_schedule_trace": len(trace_logs),
        },
        "call_logs": call_logs,
        "accounts": _account_snapshot(emails) if emails else [],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
