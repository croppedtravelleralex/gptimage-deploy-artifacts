#!/usr/bin/env python3
"""Run 10 serial /v1/images/generations on Panda and report phase timings from call logs."""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REMOTE = "panda"
EMAIL = "qaflowakjewai6ps@proton.me"
BASE = "http://127.0.0.1:8012"
TIMEOUT_SECS = 540
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
PROMPT_TAG = f"PROD-serial10-{STAMP}"
PROMPT = (
    f"{PROMPT_TAG}: rainy Tokyo side street at dusk, neon reflections, "
    "cinematic, no text, no watermark"
)
OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "captures" / "spa"


def remote(cmd: str, timeout: float = 900) -> str:
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
    return (proc.stdout or "") + (proc.stderr or "")


def _auth() -> str:
    return remote(
        'python3 -c "import json; print(json.load(open(\'/root/gptimage/config.json\'))[\'auth-key\'])"'
    ).strip()


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def _post_one(auth: str, run_idx: int) -> dict:
    body = json.dumps(
        {
            "model": "gpt-image-2",
            "prompt": f"{PROMPT} [run={run_idx}]",
            "n": 1,
            "response_format": "b64_json",
        },
        ensure_ascii=False,
    )
    py = f"""
import json, time, urllib.request, urllib.error
auth = {json.dumps(auth)}
body = {json.dumps(body)}.encode("utf-8")
req = urllib.request.Request(
    {json.dumps(BASE + "/v1/images/generations")},
    data=body,
    method="POST",
    headers={{
        "Authorization": f"Bearer {{auth}}",
        "Content-Type": "application/json",
        "X-Preferred-Account-Email": {json.dumps(EMAIL)},
    }},
)
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout={TIMEOUT_SECS}) as resp:
        raw = resp.read()
        code = resp.status
except urllib.error.HTTPError as exc:
    raw = exc.read()
    code = exc.code
except Exception as exc:
    raw = str(exc).encode("utf-8", "replace")
    code = 0
elapsed_ms = int((time.time() - t0) * 1000)
try:
    data = json.loads(raw.decode("utf-8", "replace"))
except Exception:
    data = {{"raw": raw[:500].decode("utf-8", "replace")}}
images = data.get("data") if isinstance(data, dict) else None
b64_len = 0
if isinstance(images, list) and images and isinstance(images[0], dict):
    b64_len = len(str(images[0].get("b64_json") or ""))
print(json.dumps({{
    "run": {run_idx},
    "http_code": code,
    "ok": code == 200 and b64_len > 1000,
    "elapsed_ms": elapsed_ms,
    "b64_len": b64_len,
    "error": data.get("error") if isinstance(data, dict) else None,
}}))
"""
    b64 = base64.b64encode(py.encode("utf-8")).decode("ascii")
    out = remote(
        f"python3 -c \"import base64; exec(base64.b64decode('{b64}').decode())\"",
        timeout=TIMEOUT_SECS + 120,
    )
    return json.loads(out.strip().splitlines()[-1])


def _fetch_phase_rows(tag: str) -> list[dict]:
    py = f"""
import json, urllib.request
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
req=urllib.request.Request(
    "http://127.0.0.1:8012/api/logs?type=call&limit=200",
    headers={{"Authorization": f"Bearer {{auth}}"}},
)
items=json.load(urllib.request.urlopen(req, timeout=60)).get("items", [])
tag={json.dumps(tag)}
rows=[]
for i in items:
    d=i.get("detail") or {{}}
    prompt=str(d.get("request_text") or d.get("prompt") or d.get("request_prompt") or i.get("summary") or "")
    if tag not in prompt and tag not in str(i.get("summary") or ""):
        continue
    if "调用完成" not in str(i.get("summary") or "") and "调用失败" not in str(i.get("summary") or ""):
        continue
    pt=d.get("phase_timings_ms") if isinstance(d.get("phase_timings_ms"), dict) else {{}}
    st=d.get("schedule_trace") if isinstance(d.get("schedule_trace"), dict) else {{}}
    st_ph=st.get("phases_ms") if isinstance(st.get("phases_ms"), dict) else {{}}
    def g(k):
        v=pt.get(k) or st_ph.get(k) or d.get(f"phase_{{k}}") or d.get(k)
        try: return int(v or 0)
        except: return 0
    aq=g("account_queue_ms")
    sse=g("sse_stream_ms")
    ss=g("ss_ms")
    rows.append({{
        "time": i.get("time"),
        "status": d.get("status") or ("success" if "完成" in str(i.get("summary")) else "failed"),
        "task_queue_ms": g("task_queue_ms"),
        "admit_queue_ms": g("admit_queue_ms"),
        "ps_queue_ms": g("ps_queue_ms"),
        "account_queue_ms": aq,
        "ss_queue_ms": g("ss_queue_ms"),
        "sse_stream_ms": sse,
        "poll_resolve_ms": max(0, ss - aq - sse) if ss else g("poll_resolve_ms"),
        "download_ms": g("download_ms"),
        "wall_clock_ms": g("wall_clock_ms") or int(d.get("duration_ms") or d.get("total_wall_ms") or 0),
        "trace_engine": st.get("engine") or "",
        "trace_events": int(st.get("event_count") or 0),
        "prompt_tail": prompt[-80:],
    }})
rows.sort(key=lambda r: r.get("time") or "")
print(json.dumps(rows, ensure_ascii=False))
"""
    b64 = base64.b64encode(py.encode()).decode()
    out = remote(
        f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"',
        timeout=120,
    )
    return json.loads(out.strip() or "[]")


PHASE_KEYS = [
    ("task_queue_ms", "任务排队"),
    ("admit_queue_ms", "准入排队"),
    ("ps_queue_ms", "pS排队"),
    ("account_queue_ms", "取号"),
    ("ss_queue_ms", "sS排队"),
    ("sse_stream_ms", "开票+SSE"),
    ("poll_resolve_ms", "轮询收图"),
    ("download_ms", "下载"),
    ("wall_clock_ms", "墙钟"),
]


def _summarize_phases(rows: list[dict]) -> dict:
    out: dict = {}
    for key, _label in PHASE_KEYS:
        vals = [float(r[key]) for r in rows if int(r.get(key) or 0) > 0]
        if not vals:
            out[key] = {"n": 0}
            continue
        out[key] = {
            "n": len(vals),
            "min": round(min(vals), 1),
            "p50": _pct(vals, 50),
            "p95": _pct(vals, 95),
            "max": round(max(vals), 1),
            "mean": round(sum(vals) / len(vals), 1),
        }
    return out


def _md_report(payload: dict) -> str:
    lines = [
        f"# PROD serial10 阶段分解 — {payload['stamp']}",
        "",
        f"- 账号：`{payload['account_email']}`",
        f"- 通过：**{payload['ok_count']}/{payload['runs_planned']}**",
        f"- Prompt 标签：`{payload['prompt_tag']}`",
        f"- schedule_trace 引擎：**{payload.get('trace_engine', 'unknown')}**（{payload.get('trace_event_count_mean', '-')} events/任务均值）",
        "",
        "## HTTP 墙钟（客户端）",
        "",
        f"| 指标 | ms |",
        f"|------|-----|",
    ]
    lat = payload.get("http_latency_ms") or {}
    for k in ("min_ms", "p50_ms", "p95_ms", "max_ms", "mean_ms"):
        if lat.get(k) is not None:
            lines.append(f"| {k} | {lat[k]} |")
    lines += ["", "## Pipeline 阶段 p50 / p95（服务端 call log）", "", "| 阶段 | p50 | p95 | max | n |", "|------|-----|-----|-----|---|"]
    for key, label in PHASE_KEYS:
        s = (payload.get("phase_summary") or {}).get(key) or {}
        if not s.get("n"):
            continue
        lines.append(f"| {label} | {s.get('p50')} | {s.get('p95')} | {s.get('max')} | {s.get('n')} |")
    lines += ["", "## 逐轮明细", "", "| # | HTTP ms | 墙钟 | 取号 | sS排队 | 开票+SSE | 轮询 | trace | 状态 |", "|---|---------|------|------|--------|----------|------|-------|------|"]
    for i, r in enumerate(payload.get("phase_rows") or [], 1):
        lines.append(
            f"| {i} | "
            f"{(payload.get('http_results') or [{}])[i-1].get('elapsed_ms', '-')} | "
            f"{r.get('wall_clock_ms', '-')} | "
            f"{r.get('account_queue_ms', '-')} | "
            f"{r.get('ss_queue_ms', '-')} | "
            f"{r.get('sse_stream_ms', '-')} | "
            f"{r.get('poll_resolve_ms', '-')} | "
            f"{r.get('trace_engine', '-')}/{r.get('trace_events', '-')} | "
            f"{r.get('status', '-')} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--gap", type=float, default=30.0)
    ap.add_argument("--no-fail-fast", action="store_true")
    args = ap.parse_args()

    auth = _auth()
    http_results: list[dict] = []
    for i in range(1, args.runs + 1):
        print(f"[serial10] run {i}/{args.runs} ...", flush=True)
        row = _post_one(auth, i)
        http_results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if not row.get("ok") and not args.no_fail_fast:
            break
        if i < args.runs and args.gap > 0:
            time.sleep(args.gap)

    time.sleep(3)
    phase_rows = _fetch_phase_rows(PROMPT_TAG)
    ok_n = sum(1 for r in http_results if r.get("ok"))
    walls = [float(r["elapsed_ms"]) for r in http_results if r.get("elapsed_ms") is not None]
    trace_engines = sorted({str(r.get("trace_engine") or "") for r in phase_rows if r.get("trace_engine")})
    trace_events = [int(r.get("trace_events") or 0) for r in phase_rows if int(r.get("trace_events") or 0) > 0]

    payload = {
        "stamp": STAMP,
        "prompt_tag": PROMPT_TAG,
        "account_email": EMAIL,
        "runs_planned": args.runs,
        "runs_executed": len(http_results),
        "ok_count": ok_n,
        "pass": ok_n == args.runs,
        "trace_engine": trace_engines[0] if len(trace_engines) == 1 else trace_engines,
        "trace_event_count_mean": round(sum(trace_events) / len(trace_events), 1) if trace_events else None,
        "http_results": http_results,
        "http_latency_ms": {
            "n": len(walls),
            "min_ms": round(min(walls), 2) if walls else None,
            "p50_ms": _pct(walls, 50),
            "p95_ms": _pct(walls, 95),
            "max_ms": round(max(walls), 2) if walls else None,
            "mean_ms": round(sum(walls) / len(walls), 2) if walls else None,
        },
        "phase_rows": phase_rows,
        "phase_summary": _summarize_phases(phase_rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / f"PROD-serial10-{STAMP}.json"
    md_path = OUT_DIR / f"PROD-serial10-{STAMP}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_md_report(payload), encoding="utf-8")
    print(json.dumps({"pass": payload["pass"], "ok": ok_n, "phase_n": len(phase_rows), "json": str(json_path), "md": str(md_path)}, ensure_ascii=False, indent=2))
    print(_md_report(payload))
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
