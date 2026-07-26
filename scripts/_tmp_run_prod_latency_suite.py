#!/usr/bin/env python3
"""Production latency suite: serial5 + conc10 with p50/p95/p99, fail-fast on any failure."""
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
GAP_SECS = 60
PROMPT = (
    "PROD-LAT fair serial: a rainy Tokyo side street at dusk, neon reflections, "
    "cinematic, no text, no watermark"
)
OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "captures" / "spa"


def remote(cmd: str, timeout: float = 900, *, allow_fail: bool = False) -> str:
    proc = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", REMOTE, cmd],
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0 and not allow_fail:
        raise RuntimeError(f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return (proc.stdout or "") + (proc.stderr or "")


def _auth() -> str:
    return remote(
        "python3 -c \"import json; print(json.load(open('/root/gptimage/config.json'))['auth-key'])\""
    ).strip()


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def _percentile_block(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50_ms": _pct(values, 50),
        "p95_ms": _pct(values, 95),
        "p99_ms": _pct(values, 99),
        "min_ms": round(min(values), 2) if values else None,
        "max_ms": round(max(values), 2) if values else None,
        "mean_ms": round(sum(values) / len(values), 2) if values else None,
    }


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


def run_serial5(*, runs: int, gap: float, fail_fast: bool) -> dict:
    auth = _auth()
    results: list[dict] = []
    stopped_early = False
    for i in range(1, runs + 1):
        print(f"[serial5] run {i}/{runs} ...", flush=True)
        row = _post_one(auth, i)
        results.append(row)
        if not row.get("ok"):
            print(f"[serial5] FAIL run={i} http={row.get('http_code')} err={row.get('error')}", flush=True)
            if fail_fast:
                stopped_early = True
                break
        elif i < runs and gap > 0:
            time.sleep(gap)
    ok_n = sum(1 for r in results if r.get("ok"))
    walls = [float(r["elapsed_ms"]) for r in results if r.get("elapsed_ms") is not None]
    summary = {
        "phase": "PROD-serial5",
        "account_email": EMAIL,
        "runs_planned": runs,
        "runs_executed": len(results),
        "ok_count": ok_n,
        "pass": ok_n == runs and not stopped_early,
        "stopped_early": stopped_early,
        "timeout_secs": TIMEOUT_SECS,
        "gap_secs": gap,
        "latency_ms": _percentile_block(walls),
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return summary


def _fetch_conc10_durations_from_logs(run_id: str) -> dict:
    py = f"""
import json, urllib.request
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
req=urllib.request.Request(
    "http://127.0.0.1:8012/api/logs?type=call&limit=80",
    headers={{"Authorization": f"Bearer {{auth}}"}},
)
items=json.load(urllib.request.urlopen(req, timeout=30)).get("items", [])
vals = []
for i in items:
    d = i.get("detail") or {{}}
    tid = str(d.get("task_id") or d.get("client_task_id") or "")
    if {json.dumps(run_id)} not in tid:
        continue
    pt = d.get("phase_timings_ms") if isinstance(d.get("phase_timings_ms"), dict) else {{}}
    wc = pt.get("wall_clock_ms") or d.get("duration_ms")
    if wc is not None:
        vals.append(float(wc))
print(json.dumps(vals))
"""
    b64 = base64.b64encode(py.encode()).decode()
    out = remote(
        f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"',
        timeout=60,
    )
    vals = json.loads(out.strip() or "[]")
    block = _percentile_block(vals)
    block["source"] = "call_logs" if vals else "status_api"
    return block


def run_conc10(*, count: int, max_wait: float, fail_fast: bool) -> dict:
    script_local = Path(__file__).resolve().parent / "_tmp_pipeline_conc10_acceptance.py"
    remote_path = "/root/gptimage/_tmp_pipeline_conc10_acceptance.py"
    proc = subprocess.run(
        ["scp", str(script_local), f"{REMOTE}:{remote_path}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = f"/root/gptimage/data/runlogs/prod-latency-conc10-{stamp}"
    remote(f"mkdir -p {out_dir}")
    cmd = (
        f"cd /root/gptimage && python3 {remote_path} "
        f"--base {BASE} --config-root /root/gptimage "
        f"--count {count} --max-wait-secs {max_wait} --out-dir {out_dir}"
    )
    raw = remote(cmd, timeout=max_wait + 180, allow_fail=True)
    report_path = ""
    report: dict = {}
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("report"):
            report_path = str(payload["report"])
            break
    if report_path:
        report_raw = remote(f"cat {report_path}", timeout=60)
        report = json.loads(report_raw)
    completed = int(
        (report.get("final_status_counts") or {}).get("success", 0)
        + (report.get("final_status_counts") or {}).get("completed", 0)
    )
    failed = int(
        (report.get("final_status_counts") or {}).get("failed", 0)
        + (report.get("final_status_counts") or {}).get("error", 0)
    )
    wall_clocks = []
    for item in report.get("final_items") or []:
        wc = item.get("wall_clock_ms")
        if wc is not None:
            wall_clocks.append(float(wc))
    run_id = ""
    if report_path:
        run_id = Path(report_path).stem
    task_wall = _percentile_block(wall_clocks)
    if not wall_clocks and run_id:
        task_wall = _fetch_conc10_durations_from_logs(run_id)
    ss_queue = []
    for item in report.get("final_items") or []:
        timings = item.get("phase_timings_ms") if isinstance(item.get("phase_timings_ms"), dict) else {}
        if isinstance(timings, dict) and timings.get("ss_queue_ms") is not None:
            ss_queue.append(float(timings["ss_queue_ms"]))
    pass_ok = completed >= count and failed == 0
    if fail_fast and failed > 0:
        pass_ok = False
    summary = {
        "phase": "PROD-conc10",
        "count": count,
        "completed": completed,
        "failed": failed,
        "pass": pass_ok,
        "wall_clock_ms": report.get("wall_clock_ms"),
        "task_wall_clock_ms": task_wall,
        "ss_queue_ms": _percentile_block(ss_queue),
        "final_status_counts": report.get("final_status_counts"),
        "report": report_path,
        "raw_tail": raw[-2000:],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return summary


def _write_artifacts(stamp: str, payload: dict) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / f"PROD-latency-{stamp}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    s5 = payload.get("serial5") or {}
    c10 = payload.get("conc10") or {}
    lat5 = s5.get("latency_ms") or {}
    lat10 = c10.get("task_wall_clock_ms") or {}
    md_lines = [
        f"# PROD latency — {stamp}",
        "",
        "## serial5（公平 API `/v1/images`）",
        "",
        f"| 项 | 值 |",
        f"|----|-----|",
        f"| 账号 | `{s5.get('account_email', '')}` |",
        f"| 成功 | **{s5.get('ok_count')}/{s5.get('runs_planned')}** |",
        f"| pass | **{s5.get('pass')}** |",
        f"| p50 | {lat5.get('p50_ms')} ms |",
        f"| p95 | {lat5.get('p95_ms')} ms |",
        f"| p99 | {lat5.get('p99_ms')} ms |",
        "",
        "## conc10（image-tasks pipeline）",
        "",
        f"| 项 | 值 |",
        f"|----|-----|",
        f"| 成功 | **{c10.get('completed')}/{c10.get('count')}** |",
        f"| pass | **{c10.get('pass')}** |",
        f"| 总 wall | {c10.get('wall_clock_ms')} ms |",
        f"| task p50 | {lat10.get('p50_ms')} ms |",
        f"| task p95 | {lat10.get('p95_ms')} ms |",
        f"| task p99 | {lat10.get('p99_ms')} ms |",
        "",
        f"JSON: `{json_path.name}`",
    ]
    md_path = OUT_DIR / f"PROD-latency-{stamp}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial-runs", type=int, default=5)
    ap.add_argument("--serial-gap", type=float, default=GAP_SECS)
    ap.add_argument("--conc-count", type=int, default=10)
    ap.add_argument("--conc-max-wait", type=float, default=900.0)
    ap.add_argument("--no-fail-fast", action="store_true")
    ap.add_argument("--serial-only", action="store_true")
    ap.add_argument("--conc-only", action="store_true")
    args = ap.parse_args()
    fail_fast = not args.no_fail_fast
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload: dict = {"stamp": stamp, "fail_fast": fail_fast}

    if not args.conc_only:
        s5 = run_serial5(runs=args.serial_runs, gap=args.serial_gap, fail_fast=fail_fast)
        payload["serial5"] = s5
        print(json.dumps(s5, ensure_ascii=False, indent=2))
        if fail_fast and not s5.get("pass"):
            payload["overall_pass"] = False
            _write_artifacts(stamp, payload)
            print("SUITE_STOPPED: serial5 failed", file=sys.stderr)
            return 1

    if not args.serial_only:
        c10 = run_conc10(count=args.conc_count, max_wait=args.conc_max_wait, fail_fast=fail_fast)
        payload["conc10"] = c10
        print(json.dumps(c10, ensure_ascii=False, indent=2))
        if fail_fast and not c10.get("pass"):
            payload["overall_pass"] = False
            _write_artifacts(stamp, payload)
            print("SUITE_STOPPED: conc10 failed", file=sys.stderr)
            return 1

    payload["overall_pass"] = True
    if not args.conc_only:
        payload["overall_pass"] = payload.get("overall_pass", True) and bool(payload.get("serial5", {}).get("pass"))
    if not args.serial_only:
        payload["overall_pass"] = payload.get("overall_pass", True) and bool(payload.get("conc10", {}).get("pass"))
    json_path, md_path = _write_artifacts(stamp, payload)
    print(f"WROTE {json_path}")
    print(f"WROTE {md_path}")
    return 0 if payload["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
