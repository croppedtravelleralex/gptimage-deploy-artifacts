#!/usr/bin/env python3
"""Pull full phase_timings_ms from Panda call logs and print mean wall shares."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

TAGS = [
    "PROD-serial10-20260724T165739Z",
    "PROD-conc10-20260725T005309Z",
]
PHASES = [
    "task_queue_ms",
    "admit_queue_ms",
    "upload_queue_ms",
    "ps_queue_ms",
    "account_queue_ms",
    "ss_queue_ms",
    "download_queue_ms",
    "upload_ms",
    "ps_ms",
    "ss_ms",
    "sse_stream_ms",
    "poll_resolve_ms",
    "download_ms",
    "wall_clock_ms",
]
LABELS = {
    "task_queue_ms": "任务排队",
    "admit_queue_ms": "准入排队",
    "upload_queue_ms": "上传排队",
    "ps_queue_ms": "pS排队",
    "account_queue_ms": "取号",
    "ss_queue_ms": "sS排队",
    "download_queue_ms": "下载排队",
    "upload_ms": "上传",
    "ps_ms": "pS执行",
    "ss_ms": "sS总段",
    "sse_stream_ms": "开票+SSE",
    "poll_resolve_ms": "轮询收图",
    "download_ms": "下载",
    "wall_clock_ms": "墙钟",
}


def fetch_rows(tag: str) -> list[dict]:
    py = f"""
import json, urllib.request
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
req=urllib.request.Request(
    "http://127.0.0.1:8012/api/logs?type=call&limit=300",
    headers={{"Authorization": f"Bearer {{auth}}"}},
)
items=json.load(urllib.request.urlopen(req, timeout=60)).get("items", [])
tag={json.dumps(tag)}
phases={json.dumps(PHASES)}
rows=[]
for i in items:
    d=i.get("detail") or {{}}
    blob=json.dumps(d, ensure_ascii=False)
    if tag not in blob:
        continue
    if "调用完成" not in str(i.get("summary") or ""):
        continue
    pt=d.get("phase_timings_ms") if isinstance(d.get("phase_timings_ms"), dict) else {{}}
    st=d.get("schedule_trace") if isinstance(d.get("schedule_trace"), dict) else {{}}
    st_ph=st.get("phases_ms") if isinstance(st.get("phases_ms"), dict) else {{}}
    row={{}}
    for k in phases:
        v=pt.get(k)
        if v is None:
            v=st_ph.get(k)
        if v is None:
            v=d.get(f"phase_{{k}}")
        if v is None and k=="task_queue_ms":
            v=d.get("task_queue_ms")
        if v is None and k=="wall_clock_ms":
            v=d.get("total_wall_ms") or d.get("duration_ms")
        try:
            row[k]=int(v or 0)
        except Exception:
            row[k]=0
    if not row.get("poll_resolve_ms"):
        row["poll_resolve_ms"]=max(
            0,
            row.get("ss_ms", 0) - row.get("account_queue_ms", 0) - row.get("sse_stream_ms", 0),
        )
    rows.append(row)
print(json.dumps(rows))
"""
    b64 = base64.b64encode(py.encode()).decode()
    out = subprocess.check_output(
        ["ssh", "-o", "ConnectTimeout=20", "panda", f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"'],
        text=True,
        timeout=120,
    )
    return json.loads(out.strip())


def report(tag: str, rows: list[dict]) -> dict:
    walls = [int(r.get("wall_clock_ms") or 0) for r in rows if int(r.get("wall_clock_ms") or 0) > 0]
    wall_mean = sum(walls) / len(walls) if walls else 0.0
    shares = []
    total_pct = 0.0
    for key in PHASES:
        if key == "wall_clock_ms":
            continue
        mean = sum(int(r.get(key) or 0) for r in rows) / len(rows) if rows else 0.0
        pct = round(100.0 * mean / wall_mean, 2) if wall_mean else 0.0
        if mean > 0:
            total_pct += pct
        shares.append({"key": key, "label": LABELS[key], "mean_ms": round(mean, 1), "pct": pct})
    return {
        "tag": tag,
        "n": len(rows),
        "wall_mean_ms": round(wall_mean, 1),
        "shares": shares,
        "accounted_pct": round(total_pct, 2),
        "unaccounted_pct": round(100.0 - total_pct, 2),
    }


def main() -> None:
    reports = []
    for tag in TAGS:
        rows = fetch_rows(tag)
        reports.append(report(tag, rows))
    out_path = Path(__file__).resolve().parents[1] / "docs" / "captures" / "spa" / "PROD-phase-wall-share-mean.json"
    out_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
