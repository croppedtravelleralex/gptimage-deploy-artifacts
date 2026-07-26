#!/usr/bin/env python3
"""Pull phase_timings from Panda call logs for a run prefix."""
from __future__ import annotations

import argparse
import base64
import json
import subprocess


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True, help="task_id prefix e.g. pipe-conc10-20260724T120352Z")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()

    py = f"""
import json, urllib.request
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
req=urllib.request.Request(
    "http://127.0.0.1:8012/api/logs?type=call&limit={args.limit}",
    headers={{"Authorization": f"Bearer {{auth}}"}},
)
items=json.load(urllib.request.urlopen(req, timeout=30)).get("items", [])
rows=[]
for i in items:
    d=i.get("detail") or {{}}
    tid=str(d.get("task_id") or d.get("client_task_id") or "")
    if not tid.startswith({json.dumps(args.prefix)}):
        continue
    pt=d.get("phase_timings_ms") if isinstance(d.get("phase_timings_ms"), dict) else {{}}
    aq=int(pt.get("account_queue_ms") or d.get("phase_account_queue_ms") or 0)
    sse=int(pt.get("sse_stream_ms") or d.get("phase_sse_stream_ms") or 0)
    ssq=int(pt.get("ss_queue_ms") or 0)
    ss=int(pt.get("ss_ms") or 0)
    wall=int(pt.get("wall_clock_ms") or d.get("total_wall_ms") or d.get("duration_ms") or 0)
    tq=int(d.get("task_queue_ms") or 0)
    poll=max(0, ss - aq - sse) if ss else 0
    rows.append({{
        "task_id": tid,
        "time": i.get("time"),
        "task_queue_ms": tq,
        "account_queue_ms": aq,
        "ss_queue_ms": ssq,
        "sse_stream_ms": sse,
        "poll_resolve_ms": poll,
        "download_ms": int(pt.get("download_ms") or 0),
        "wall_clock_ms": wall,
    }})
print(json.dumps(rows, ensure_ascii=False))
"""
    b64 = base64.b64encode(py.encode()).decode()
    out = subprocess.check_output(
        ["ssh", "-o", "ConnectTimeout=20", "panda", f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"'],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    rows = json.loads(out.strip() or "[]")
    if not rows:
        print(json.dumps({"error": "no rows", "prefix": args.prefix}, ensure_ascii=False, indent=2))
        return 1

    def col(key: str) -> list[float]:
        return [float(r[key]) for r in rows if r.get(key) is not None]

    summary = {
        "prefix": args.prefix,
        "n": len(rows),
        "task_queue_ms": {"p50": pct(col("task_queue_ms"), 50), "p95": pct(col("task_queue_ms"), 95)},
        "account_queue_ms": {"p50": pct(col("account_queue_ms"), 50), "p95": pct(col("account_queue_ms"), 95), "max": max(col("account_queue_ms"))},
        "ss_queue_ms": {"p50": pct(col("ss_queue_ms"), 50), "p95": pct(col("ss_queue_ms"), 95)},
        "sse_stream_ms": {"p50": pct(col("sse_stream_ms"), 50), "p95": pct(col("sse_stream_ms"), 95)},
        "poll_resolve_ms": {"p50": pct(col("poll_resolve_ms"), 50), "p95": pct(col("poll_resolve_ms"), 95)},
        "wall_clock_ms": {"p50": pct(col("wall_clock_ms"), 50), "p95": pct(col("wall_clock_ms"), 95)},
        "rows": rows,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
