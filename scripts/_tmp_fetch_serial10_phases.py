#!/usr/bin/env python3
import json, subprocess, sys

auth = subprocess.check_output([
    "ssh", "-o", "ConnectTimeout=20", "panda",
    'python3 -c "import json; print(json.load(open(\'/root/gptimage/config.json\'))[\'auth-key\'])"',
], text=True).strip()

raw = subprocess.check_output([
    "ssh", "-o", "ConnectTimeout=20", "panda",
    f"curl -fsS -H 'Authorization: Bearer {auth}' 'http://127.0.0.1:8012/api/logs?type=call&limit=80'",
]).decode("utf-8", errors="replace")
data = json.loads(raw)
tag = sys.argv[1] if len(sys.argv) > 1 else "PROD-serial10-20260724T143921Z"
email = "qaflowakjewai6ps@proton.me"
rows = []
for i in data.get("items", []):
    d = i.get("detail") or {}
    blob = json.dumps(d, ensure_ascii=False)
    if tag not in blob and email not in blob:
        continue
    if "调用完成" not in str(i.get("summary") or "") and "完成" not in str(i.get("summary") or ""):
        continue
    pt = d.get("phase_timings_ms") if isinstance(d.get("phase_timings_ms"), dict) else {}
    aq = int(pt.get("account_queue_ms") or 0)
    sse = int(pt.get("sse_stream_ms") or 0)
    ss = int(pt.get("ss_ms") or 0)
    rows.append({
        "time": i.get("time"),
        "summary": i.get("summary"),
        "task_queue_ms": int(pt.get("task_queue_ms") or d.get("task_queue_ms") or 0),
        "admit_queue_ms": int(pt.get("admit_queue_ms") or 0),
        "ps_queue_ms": int(pt.get("ps_queue_ms") or 0),
        "account_queue_ms": aq,
        "ss_queue_ms": int(pt.get("ss_queue_ms") or 0),
        "sse_stream_ms": sse,
        "poll_resolve_ms": max(0, ss - aq - sse) if ss else int(pt.get("poll_resolve_ms") or 0),
        "download_ms": int(pt.get("download_ms") or 0),
        "wall_clock_ms": int(pt.get("wall_clock_ms") or d.get("duration_ms") or d.get("total_wall_ms") or 0),
        "has_phase": bool(pt),
    })
rows.sort(key=lambda r: r.get("time") or "")
print(json.dumps({"n": len(rows), "rows": rows}, ensure_ascii=False, indent=2))
