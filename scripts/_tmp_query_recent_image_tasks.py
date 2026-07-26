#!/usr/bin/env python3
"""Query recent image tasks + call logs on Panda for 2-concurrency investigation."""
from __future__ import annotations

import json
import subprocess
import textwrap
from datetime import datetime, timezone

REMOTE = "panda"
REMOTE_DIR = "/root/gptimage"


def remote(cmd: str, timeout: float = 120) -> str:
    proc = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd],
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout or ""


REMOTE_PY = """
import json
import sqlite3
from pathlib import Path

root = Path("/root/gptimage")
config = json.loads((root / "config.json").read_text(encoding="utf-8"))
print("CONFIG", json.dumps({
    "image_account_concurrency": config.get("image_account_concurrency"),
    "image_binding_inflight_max": config.get("image_binding_inflight_max"),
    "submit_start_min_interval_ms": (config.get("image_task_queue") or {}).get("submit_start_min_interval_ms"),
    "per_user_running_base": (config.get("image_task_queue") or {}).get("per_user_running_base"),
    "pipeline_enabled": (config.get("image_pipeline") or {}).get("enabled"),
}, ensure_ascii=False))

db = root / "data" / "image_tasks.db"
out = {"tasks": [], "db_exists": db.is_file()}
if db.is_file():
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT key, task_id, status, updated_ts, data FROM image_tasks ORDER BY updated_ts DESC LIMIT 40'
    ).fetchall()
    for row in rows:
        task = {}
        try:
            task = json.loads(row["data"] or "{}")
        except Exception:
            task = {}
        phase = task.get("phase_timings_ms") if isinstance(task.get("phase_timings_ms"), dict) else {}
        data = task.get("data") if isinstance(task.get("data"), list) else []
        first = data[0] if data else {}
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        prompt = str(payload.get("prompt") or "")
        out["tasks"].append({
            "id": row["task_id"] or task.get("id"),
            "status": row["status"] or task.get("status"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "updated_ts": row["updated_ts"],
            "duration_ms": task.get("duration_ms"),
            "total_wall_ms": task.get("total_wall_ms"),
            "task_queue_ms": task.get("task_queue_ms"),
            "worker_duration_ms": task.get("worker_duration_ms"),
            "wall_clock_ms": phase.get("wall_clock_ms"),
            "phase_timings_ms": phase,
            "progress": task.get("progress"),
            "error": task.get("error"),
            "has_image_url": bool(first.get("url")),
            "has_b64": bool(first.get("b64_json")),
            "prompt_preview": prompt[:100],
        })
    conn.close()

logs_path = root / "data" / "logs.jsonl"
out["call_logs"] = []
if logs_path.is_file():
    lines = logs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for raw in reversed(lines[-5000:]):
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if item.get("type") != "call":
            continue
        summary = str(item.get("summary") or "")
        if "文生图" not in summary and "图生图" not in summary:
            continue
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        out["call_logs"].append({
            "time": item.get("time"),
            "summary": summary,
            "task_id": detail.get("task_id"),
            "duration_ms": detail.get("duration_ms"),
            "total_wall_ms": detail.get("total_wall_ms"),
            "task_queue_ms": detail.get("task_queue_ms"),
            "phase_timings_ms": detail.get("phase_timings_ms"),
            "status": detail.get("status"),
            "error": detail.get("error"),
            "request_text": (detail.get("request_text") or "")[:80],
        })
        if len(out["call_logs"]) >= 40:
            break

print("RESULT_JSON_START")
print(json.dumps(out, ensure_ascii=False, indent=2))
print("RESULT_JSON_END")
"""


def main() -> None:
    raw = remote(f"python3 - <<'PY'\n{REMOTE_PY}\nPY", timeout=180)
    start = raw.find("RESULT_JSON_START")
    end = raw.find("RESULT_JSON_END")
    if start < 0 or end < 0:
        print(raw)
        return
    payload = json.loads(raw[start + len("RESULT_JSON_START") : end].strip())

    print("\n=== CONFIG ===")
    print(raw.split("CONFIG", 1)[1].split("\n", 1)[0] if "CONFIG" in raw else "")

    tasks = payload.get("tasks") or []
    print(f"\n=== RECENT TASKS ({len(tasks)}) ===")
    # group pairs with similar prompt within 5 min
    for t in tasks[:15]:
        print(
            f"- {t.get('id')} | {t.get('status')} | created={t.get('created_at')} | "
            f"dur={t.get('duration_ms')} total_wall={t.get('total_wall_ms')} "
            f"queue={t.get('task_queue_ms')} wall_clock={t.get('wall_clock_ms')} | "
            f"img={'url' if t.get('has_image_url') else ('b64' if t.get('has_b64') else 'NONE')} | "
            f"{t.get('prompt_preview')}"
        )
        phase = t.get("phase_timings_ms") or {}
        if phase:
            print(f"  phases: {json.dumps(phase, ensure_ascii=False)}")

    logs = payload.get("call_logs") or []
    print(f"\n=== RECENT CALL LOGS ({len(logs)}) ===")
    for row in logs[:20]:
        print(
            f"- {row.get('time')} | {row.get('summary')} | task={row.get('task_id')} | "
            f"dur={row.get('duration_ms')} total={row.get('total_wall_ms')} queue={row.get('task_queue_ms')} | "
            f"status={row.get('status')}"
        )
        if row.get("phase_timings_ms"):
            print(f"  phases: {json.dumps(row.get('phase_timings_ms'), ensure_ascii=False)}")


if __name__ == "__main__":
    main()
