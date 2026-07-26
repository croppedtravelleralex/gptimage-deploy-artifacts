#!/usr/bin/env python3
"""Run 10 concurrent /v1/images/generations on Panda (multi-account) and report phase timings."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

REMOTE = "panda"
BASE = "http://127.0.0.1:8012"
TIMEOUT_SECS = 540
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
PROMPT_TAG = f"PROD-conc10-{STAMP}"
OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "captures" / "spa"

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


def _dispatch_emails() -> list[str]:
    py = """
import json, urllib.request
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
req=urllib.request.Request(
    "http://127.0.0.1:8012/api/accounts?limit=500",
    headers={"Authorization": f"Bearer {auth}"},
)
items=json.load(urllib.request.urlopen(req, timeout=60)).get("items", [])
emails=sorted({
    str(i.get("email") or "").strip()
    for i in items
    if isinstance(i, dict) and i.get("image_schedulable") and str(i.get("email") or "").strip()
})
print(json.dumps(emails))
"""
    b64 = base64.b64encode(py.encode()).decode()
    out = remote(f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"')
    return json.loads(out.strip())


def _run_conc10_remote(auth: str, emails: list[str], count: int, tag: str) -> list[dict]:
    py = f"""
import json, time, urllib.request, urllib.error, concurrent.futures
auth = {json.dumps(auth)}
base = {json.dumps(BASE)}
tag = {json.dumps(tag)}
count = {count}
emails = {json.dumps(emails)}
timeout = {TIMEOUT_SECS}

def one(idx):
    email = emails[idx % len(emails)] if emails else ""
    body = json.dumps({{
        "model": "gpt-image-2",
        "prompt": f"{{tag}}: ceramic mug on wood table, soft daylight, no text, variant {{idx}}",
        "n": 1,
        "response_format": "b64_json",
    }}).encode("utf-8")
    headers = {{
        "Authorization": f"Bearer {{auth}}",
        "Content-Type": "application/json",
    }}
    if email:
        headers["X-Preferred-Account-Email"] = email
    req = urllib.request.Request(base + "/v1/images/generations", data=body, method="POST", headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    return {{
        "run": idx,
        "account_email": email,
        "http_code": code,
        "ok": code == 200 and b64_len > 1000,
        "elapsed_ms": elapsed_ms,
        "b64_len": b64_len,
        "error": data.get("error") if isinstance(data, dict) else None,
    }}

wall0 = time.time()
with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
    results = list(pool.map(one, range(1, count + 1)))
wall_ms = int((time.time() - wall0) * 1000)
print(json.dumps({{"wall_ms": wall_ms, "results": results}}, ensure_ascii=False))
"""
    b64 = base64.b64encode(py.encode()).decode()
    out = remote(
        f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"',
        timeout=TIMEOUT_SECS + 180,
    )
    payload = json.loads(out.strip().splitlines()[-1])
    return payload.get("results") or [], int(payload.get("wall_ms") or 0)


def _fetch_phase_rows(tag: str, since_minutes: int = 30) -> list[dict]:
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
    blob=json.dumps(d, ensure_ascii=False)
    if tag not in blob and tag not in str(i.get("summary") or ""):
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
        "account_email": d.get("account_email") or d.get("email") or "",
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


def _summarize_phases(rows: list[dict]) -> dict:
    out: dict = {}
    for key, _label in PHASE_KEYS:
        vals = [float(r[key]) for r in rows if int(r.get(key) or 0) >= 0]
        if not vals:
            out[key] = {"n": 0}
            continue
        out[key] = {
            "n": len(vals),
            "min_ms": round(min(vals), 1),
            "p50_ms": _pct(vals, 50),
            "p95_ms": _pct(vals, 95),
            "max_ms": round(max(vals), 1),
            "mean_ms": round(sum(vals) / len(vals), 1),
        }
    return out


def _wall_share_pct(summary: dict) -> dict:
    wall_mean = (summary.get("wall_clock_ms") or {}).get("mean_ms") or 0
    if not wall_mean:
        return {}
    shares = {}
    for key, _ in PHASE_KEYS:
        if key == "wall_clock_ms":
            continue
        mean = (summary.get(key) or {}).get("mean_ms") or 0
        shares[key] = round(100.0 * mean / wall_mean, 1)
    return shares


def _md_report(payload: dict) -> str:
    lines = [
        f"# PROD conc10 阶段分解 — {payload['stamp']}",
        "",
        f"- 通过：**{payload['ok_count']}/{payload['runs_planned']}**",
        f"- 并发墙钟：**{payload.get('conc_wall_ms')} ms**",
        f"- 调度账号数：**{len(payload.get('dispatch_emails') or [])}**",
        f"- Prompt 标签：`{payload['prompt_tag']}`",
        f"- schedule_trace 引擎：**{payload.get('trace_engine', 'unknown')}**（{payload.get('trace_event_count_mean', '-')} events/任务均值）",
        "",
        "## HTTP 墙钟（单任务客户端）",
        "",
        "| 指标 | ms |",
        "|------|-----|",
    ]
    lat = payload.get("http_latency_ms") or {}
    for k in ("min_ms", "p50_ms", "p95_ms", "max_ms", "mean_ms"):
        if lat.get(k) is not None:
            lines.append(f"| {k} | {lat[k]} |")
    lines += ["", "## Pipeline 阶段（服务端 call log）", "", "| 阶段 | p50 | p95 | mean | 占墙钟% |", "|------|-----|-----|------|---------|"]
    shares = payload.get("wall_share_pct") or {}
    for key, label in PHASE_KEYS:
        if key == "wall_clock_ms":
            continue
        s = (payload.get("phase_summary") or {}).get(key) or {}
        if not s.get("n"):
            continue
        lines.append(
            f"| {label} | {s.get('p50_ms')} | {s.get('p95_ms')} | {s.get('mean_ms')} | {shares.get(key, '-')} |"
        )
    lines += ["", "## 逐轮明细", "", "| # | 账号 | HTTP ms | 墙钟 | 取号 | sS排队 | SSE | poll | trace |", "|---|------|---------|------|------|--------|-----|------|-------|"]
    for i, r in enumerate(payload.get("phase_rows") or [], 1):
        http = (payload.get("http_results") or [{}])[i - 1] if i <= len(payload.get("http_results") or []) else {}
        lines.append(
            f"| {i} | {(r.get('account_email') or http.get('account_email') or '-')[:24]} | "
            f"{http.get('elapsed_ms', '-')} | {r.get('wall_clock_ms', '-')} | "
            f"{r.get('account_queue_ms', '-')} | {r.get('ss_queue_ms', '-')} | "
            f"{r.get('sse_stream_ms', '-')} | {r.get('poll_resolve_ms', '-')} | "
            f"{r.get('trace_engine', '-')}/{r.get('trace_events', '-')} |"
        )
    if payload.get("serial_compare"):
        sc = payload["serial_compare"]
        lines += [
            "",
            "## 与 serial10 占墙钟比对比",
            "",
            "| 阶段 | serial10 % | conc10 % | Δ |",
            "|------|------------|----------|---|",
        ]
        for key, label in PHASE_KEYS:
            if key == "wall_clock_ms":
                continue
            s10 = (sc.get("serial10") or {}).get(key)
            c10 = (sc.get("conc10") or {}).get(key)
            if s10 is None and c10 is None:
                continue
            delta = round((c10 or 0) - (s10 or 0), 1) if s10 is not None and c10 is not None else "-"
            lines.append(f"| {label} | {s10} | {c10} | {delta} |")
    lines.append("")
    return "\n".join(lines)


def _load_serial_shares() -> dict | None:
    for name in (
        "PROD-serial10-20260724T165739Z.json",
        "PROD-serial10-20260724T143921Z.json",
    ):
        path = OUT_DIR / name
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        summary = data.get("phase_summary") or {}
        wall_mean = (summary.get("wall_clock_ms") or {}).get("mean_ms") or 0
        if not wall_mean:
            continue
        shares = {}
        for key, _ in PHASE_KEYS:
            if key == "wall_clock_ms":
                continue
            mean = (summary.get(key) or {}).get("mean_ms") or 0
            shares[key] = round(100.0 * mean / wall_mean, 1)
        return shares, name
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    args = ap.parse_args()

    auth = _auth()
    emails = _dispatch_emails()
    print(f"[conc10] dispatchable accounts: {len(emails)}", flush=True)
    if len(emails) < 2:
        print("warning: few dispatchable accounts", flush=True)

    http_results, conc_wall_ms = _run_conc10_remote(auth, emails, args.count, PROMPT_TAG)
    http_results.sort(key=lambda r: r.get("run", 0))
    time.sleep(5)
    phase_rows = _fetch_phase_rows(PROMPT_TAG)
    if len(phase_rows) < args.count:
        time.sleep(10)
        phase_rows = _fetch_phase_rows(PROMPT_TAG)

    ok_n = sum(1 for r in http_results if r.get("ok"))
    walls = [float(r["elapsed_ms"]) for r in http_results if r.get("elapsed_ms") is not None]
    phase_summary = _summarize_phases(phase_rows)
    wall_share = _wall_share_pct(phase_summary)
    serial_shares, serial_ref = _load_serial_shares()
    trace_engines = sorted({str(r.get("trace_engine") or "") for r in phase_rows if r.get("trace_engine")})
    trace_events = [int(r.get("trace_events") or 0) for r in phase_rows if int(r.get("trace_events") or 0) > 0]

    payload = {
        "stamp": STAMP,
        "prompt_tag": PROMPT_TAG,
        "runs_planned": args.count,
        "runs_executed": len(http_results),
        "ok_count": ok_n,
        "pass": ok_n == args.count,
        "trace_engine": trace_engines[0] if len(trace_engines) == 1 else trace_engines,
        "trace_event_count_mean": round(sum(trace_events) / len(trace_events), 1) if trace_events else None,
        "conc_wall_ms": conc_wall_ms,
        "dispatch_emails": emails,
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
        "phase_summary": phase_summary,
        "wall_share_pct": wall_share,
        "serial_compare": {
            "serial10_ref": serial_ref,
            "serial10": serial_shares,
            "conc10": wall_share,
        }
        if serial_shares
        else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / f"PROD-conc10-{STAMP}.json"
    md_path = OUT_DIR / f"PROD-conc10-{STAMP}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_md_report(payload), encoding="utf-8")
    print(json.dumps({"pass": payload["pass"], "ok": ok_n, "phase_n": len(phase_rows), "json": str(json_path)}, ensure_ascii=False, indent=2))
    print(_md_report(payload))
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
