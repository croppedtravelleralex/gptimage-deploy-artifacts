#!/usr/bin/env python3
"""Verify schedule trace: engine + optional single Panda image generation."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.image_pipeline import schedule_trace


def _local_smoke() -> dict:
    run = schedule_trace.begin("verify-local-001", "trace@local.test")
    token = schedule_trace.bind(run)
    try:
        run.emit("task_worker_start")
        run.emit("pipeline_admit")
        run.emit("account_wait_start")
        time.sleep(0.01)
        run.emit("account_acquired")
        run.emit("ss_queue_enter", schedule_trace.pack_pool_aux(active=3, queued=1))
        time.sleep(0.02)
        run.emit("ss_slot_acquired", schedule_trace.pack_pool_aux(active=4, queued=0, slot=2))
        run.emit("sse_stream_end")
        run.emit("poll_resolve_end")
        run.emit("download_start")
        time.sleep(0.01)
        run.emit("download_end")
        run.emit("pipeline_finish")
        run.emit("task_terminal")
        return run.finish()
    finally:
        schedule_trace.unbind(token)


def _panda_one(auth: str, email: str) -> dict:
    tag = f"TRACE-VERIFY-{int(time.time())}"
    py = f"""
import json, time, urllib.request, urllib.error
auth = {json.dumps(auth)}
email = {json.dumps(email)}
tag = {json.dumps(tag)}
body = json.dumps({{
    "model": "gpt-image-2",
    "prompt": f"{{tag}}: minimal ceramic mug, soft light, no text",
    "n": 1,
    "response_format": "b64_json",
}}).encode()
headers = {{
    "Authorization": f"Bearer {{auth}}",
    "Content-Type": "application/json",
    "X-Preferred-Account-Email": email,
}}
req = urllib.request.Request("http://127.0.0.1:8012/v1/images/generations", data=body, method="POST", headers=headers)
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=540) as resp:
        raw = resp.read()
        code = resp.status
except urllib.error.HTTPError as exc:
    raw = exc.read()
    code = exc.code
elapsed_ms = int((time.time()-t0)*1000)
print(json.dumps({{"http_code": code, "elapsed_ms": elapsed_ms, "tag": tag, "body_head": raw[:200].decode('utf-8','replace')}}))
"""
    import base64

    b64 = base64.b64encode(py.encode()).decode()
    out = subprocess.check_output(
        ["ssh", "-o", "ConnectTimeout=20", "panda", f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"'],
        text=True,
        timeout=600,
    )
    http = json.loads(out.strip().splitlines()[-1])
    time.sleep(3)
    fetch = f"""
import json, urllib.request
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
req=urllib.request.Request(
    "http://127.0.0.1:8012/api/logs?type=call&limit=30",
    headers={{"Authorization": f"Bearer {{auth}}"}},
)
items=json.load(urllib.request.urlopen(req, timeout=60)).get("items", [])
tag={json.dumps(tag)}
row=None
for i in items:
    d=i.get("detail") or {{}}
    blob=json.dumps(d, ensure_ascii=False)
    if tag in blob:
        row=d
        break
print(json.dumps({{
    "found": row is not None,
    "schedule_trace": (row or {{}}).get("schedule_trace"),
    "phase_timings_ms": (row or {{}}).get("phase_timings_ms"),
    "account_email": (row or {{}}).get("account_email"),
}}, ensure_ascii=False))
"""
    b642 = base64.b64encode(fetch.encode()).decode()
    log_out = subprocess.check_output(
        ["ssh", "-o", "ConnectTimeout=20", "panda", f'python3 -c "import base64; exec(base64.b64decode(\'{b642}\').decode())"'],
        text=True,
        timeout=60,
    )
    log = json.loads(log_out.strip())
    return {"http": http, "call_log": log}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panda", action="store_true")
    ap.add_argument("--email", default="qaflowakjewai6ps@proton.me")
    args = ap.parse_args()

    info = schedule_trace.engine_info()
    print(json.dumps({"engine_info": info}, indent=2))

    local = _local_smoke()
    out_dir = ROOT / "docs" / "captures" / "spa"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "schedule-trace-verify-local.json"
    path.write_text(json.dumps(local, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    print(json.dumps({"local_event_count": local.get("event_count"), "phases_ms": local.get("phases_ms")}, indent=2))

    ok = bool(local.get("events")) and bool(local.get("phases_ms"))
    if args.panda:
        auth = subprocess.check_output(
            ["ssh", "-o", "ConnectTimeout=20", "panda", 'python3 -c "import json; print(json.load(open(\'/root/gptimage/config.json\'))[\'auth-key\'])"'],
            text=True,
        ).strip()
        prod = _panda_one(auth, args.email)
        prod_path = out_dir / "schedule-trace-verify-panda.json"
        prod_path.write_text(json.dumps(prod, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {prod_path}")
        ok = ok and bool(prod.get("call_log", {}).get("found"))
        st = (prod.get("call_log") or {}).get("schedule_trace") or {}
        if st:
            print(json.dumps({"panda_events": st.get("event_count"), "panda_phases": st.get("phases_ms")}, indent=2))
        else:
            print("panda schedule_trace not in call log yet — deploy updated code to Panda first", file=sys.stderr)
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
