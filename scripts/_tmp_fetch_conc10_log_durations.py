#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import subprocess

RUN_ID = "pipe-conc10-20260724T120352Z"

REMOTE_PY = f"""
import json, urllib.request
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
req=urllib.request.Request(
    "http://127.0.0.1:8012/api/logs?type=call&limit=50",
    headers={{"Authorization": f"Bearer {{auth}}"}},
)
items=json.load(urllib.request.urlopen(req, timeout=30)).get("items", [])
rows = []
for i in items:
    d = i.get("detail") or {{}}
    tid = str(d.get("task_id") or d.get("client_task_id") or "")
    if {json.dumps(RUN_ID)} in tid:
        pt = d.get("phase_timings_ms") if isinstance(d.get("phase_timings_ms"), dict) else {{}}
        rows.append({{
            "task_id": tid,
            "duration_ms": d.get("duration_ms"),
            "wall_clock_ms": pt.get("wall_clock_ms"),
            "time": i.get("time"),
        }})
print(json.dumps(rows, ensure_ascii=False))
"""


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def main() -> None:
    b64 = base64.b64encode(REMOTE_PY.encode()).decode()
    out = subprocess.check_output(
        ["ssh", "-o", "ConnectTimeout=20", "panda", f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"'],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    rows = json.loads(out.strip())
    vals = [float(r["wall_clock_ms"] or r["duration_ms"]) for r in rows if r.get("wall_clock_ms") or r.get("duration_ms")]
    print(json.dumps({"rows": rows, "n": len(vals), "p50": pct(vals, 50) if vals else None, "p95": pct(vals, 95) if vals else None, "p99": pct(vals, 99) if vals else None}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
