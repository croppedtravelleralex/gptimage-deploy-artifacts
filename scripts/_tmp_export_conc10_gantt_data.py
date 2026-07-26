#!/usr/bin/env python3
"""Export conc10 per-task phase segments for Gantt chart (Panda image_tasks.db)."""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.image_gantt_segments import build_image_task_gantt_segments

REMOTE_PY = r'''
import json, sqlite3, statistics
rounds = [
    "pipe-conc10-20260723T071912Z",
    "pipe-conc10-20260723T072133Z",
    "pipe-conc10-20260723T072340Z",
]
conn = sqlite3.connect("/app/data/image_tasks.db")
out = {"rounds": [], "source": "image_tasks.db on Panda", "suite": "acceptance-90s-picture_v2-20260723"}
for run_id in rounds:
    cur = conn.cursor()
    cur.execute(
        "SELECT task_id, data FROM image_tasks WHERE task_id LIKE ? ORDER BY task_id",
        (run_id + "%",),
    )
    tasks = []
    t0 = None
    for tid, raw in cur.fetchall():
        d = json.loads(raw)
        pt = d.get("phase_timings_ms") or {}
        created = d.get("created_ts")
        started = d.get("started_ts")
        updated = d.get("updated_ts")
        if created and (t0 is None or created < t0):
            t0 = created
        tasks.append(
            {
                "task_id": tid.split("-")[-1],
                "created_ts": created,
                "started_ts": started,
                "updated_ts": updated,
                "phase": pt,
                "wall_clock_ms": pt.get("wall_clock_ms"),
                "resume_access_token": (d.get("resume_access_token") or "")[:24],
            }
        )
    out["rounds"].append({"run_id": run_id, "label": run_id.replace("pipe-conc10-", ""), "tasks": tasks, "t0": t0})
print(json.dumps(out, ensure_ascii=False))
'''

OUT = ROOT / "docs" / "captures" / "spa" / "conc10-gantt-data.json"


def main() -> int:
    proc = subprocess.run(
        ["ssh", "panda", "docker", "exec", "-i", "chatgpt2api-local", "python3"],
        input=REMOTE_PY,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return proc.returncode
    raw = json.loads(proc.stdout)
    out = {
        "rounds": [],
        "source": raw.get("source"),
        "suite": raw.get("suite"),
        "segment_schema": "queue_wait+sse_active+poll_resolve+download_ms",
    }
    for round_row in raw.get("rounds") or []:
        t0 = round_row.get("t0") or 0
        tasks = []
        for t in round_row.get("tasks") or []:
            created = t.get("created_ts")
            started = t.get("started_ts")
            updated = t.get("updated_ts")
            start_ms = round((float(created) - float(t0)) * 1000, 1) if created and t0 else 0.0
            end_ms = round((float(updated) - float(t0)) * 1000, 1) if updated and t0 else 0.0
            segments = build_image_task_gantt_segments(
                t.get("phase") or {},
                created_ts=created,
                started_ts=started,
            )
            tasks.append(
                {
                    "task_id": t.get("task_id"),
                    "wall_clock_ms": t.get("wall_clock_ms"),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "segments": segments,
                    "account_token_prefix": t.get("resume_access_token") or "",
                }
            )
        walls = [t["wall_clock_ms"] for t in tasks if t.get("wall_clock_ms")]
        ordered = sorted(walls)
        p95 = ordered[int(round(0.95 * (len(ordered) - 1)))] if ordered else None
        out["rounds"].append(
            {
                "run_id": round_row.get("run_id"),
                "label": round_row.get("label"),
                "span_ms": max((t["end_ms"] for t in tasks), default=0),
                "tasks": tasks,
                "wall_p50": round(statistics.median(walls), 1) if walls else None,
                "wall_p95": round(p95, 1) if p95 is not None else None,
            }
        )
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
