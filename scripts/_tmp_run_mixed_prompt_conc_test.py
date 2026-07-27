#!/usr/bin/env python3
"""Mixed long/short prompt conc test: half long, half short; short half uses pS, half not."""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

REMOTE = "panda"
BASE = "http://127.0.0.1:8012"
TIMEOUT_SECS = 540
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

SHORT_PROMPT = "ceramic mug on wooden table, soft morning daylight, no text"
LONG_PROMPT = (
    "A photorealistic still-life composition for a product catalog: a hand-thrown ceramic mug "
    "with a subtle speckled glaze sits on a weathered oak table beside folded linen, a sprig of "
    "dried eucalyptus, and scattered coffee beans. The scene is lit by soft north-facing window "
    "light with gentle falloff, shallow depth of field, neutral background, natural color grading, "
    "no logos, no readable text, no watermark, high detail in ceramic texture and wood grain."
)


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
    return round(ordered[idx], 3)


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


def build_cases(count: int) -> list[dict]:
    """Half long, half short; among short, half prompt_enhance (pS), half not."""
    cases: list[dict] = []
    long_n = count // 2
    short_n = count - long_n
    short_ps_n = short_n // 2
    short_no_ps_n = short_n - short_ps_n
    idx = 1
    for i in range(long_n):
        cases.append({
            "run": idx,
            "profile": "long",
            "prompt_enhance": False,
            "uses_ps": False,
            "prompt": LONG_PROMPT,
            "variant": i + 1,
        })
        idx += 1
    for i in range(short_ps_n):
        cases.append({
            "run": idx,
            "profile": "short_ps",
            "prompt_enhance": True,
            "uses_ps": True,
            "prompt": SHORT_PROMPT,
            "variant": i + 1,
        })
        idx += 1
    for i in range(short_no_ps_n):
        cases.append({
            "run": idx,
            "profile": "short_no_ps",
            "prompt_enhance": False,
            "uses_ps": False,
            "prompt": SHORT_PROMPT,
            "variant": i + 1,
        })
        idx += 1
    return cases


def _run_remote(auth: str, emails: list[str], cases: list[dict], tag: str, response_format: str) -> tuple[list[dict], int]:
    cases_json = json.dumps(cases, ensure_ascii=False)
    py = f"""
import json, time, urllib.request, urllib.error, concurrent.futures
auth = {json.dumps(auth)}
base = {json.dumps(BASE)}
tag = {json.dumps(tag)}
response_format = {json.dumps(response_format)}
cases = json.loads({json.dumps(cases_json)})
emails = {json.dumps(emails)}
timeout = {TIMEOUT_SECS}

def one(case):
    idx = int(case["run"])
    email = emails[(idx - 1) % len(emails)] if emails else ""
    profile = case["profile"]
    prompt = f"{{tag}}|r{{idx:02d}}|{{profile}}| {{case['prompt']}}"
    body = {{
        "model": "gpt-image-2",
        "prompt": prompt,
        "n": 1,
        "response_format": response_format,
        "prompt_enhance": bool(case.get("prompt_enhance")),
    }}
    raw_body = json.dumps(body).encode("utf-8")
    headers = {{
        "Authorization": f"Bearer {{auth}}",
        "Content-Type": "application/json",
    }}
    if email:
        headers["X-Preferred-Account-Email"] = email
    req = urllib.request.Request(base + "/v1/images/generations", data=raw_body, method="POST", headers=headers)
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
    resp_bytes = len(raw)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        data = {{"raw": raw[:500].decode("utf-8", "replace")}}
    images = data.get("data") if isinstance(data, dict) else None
    b64_len = 0
    url_len = 0
    if isinstance(images, list) and images and isinstance(images[0], dict):
        b64_len = len(str(images[0].get("b64_json") or ""))
        url_len = len(str(images[0].get("url") or ""))
    ok = code == 200 and (
        (response_format == "b64_json" and b64_len > 1000)
        or (response_format == "url" and url_len > 10)
    )
    return {{
        "run": idx,
        "profile": profile,
        "prompt_enhance": bool(case.get("prompt_enhance")),
        "uses_ps": bool(case.get("uses_ps")),
        "account_email": email,
        "http_code": code,
        "ok": ok,
        "elapsed_ms": elapsed_ms,
        "resp_bytes": resp_bytes,
        "b64_len": b64_len,
        "url_len": url_len,
        "response_format": response_format,
        "error": data.get("error") if isinstance(data, dict) else None,
    }}

wall0 = time.time()
with concurrent.futures.ThreadPoolExecutor(max_workers=len(cases)) as pool:
    results = list(pool.map(one, cases))
wall_ms = int((time.time() - wall0) * 1000)
print(json.dumps({{"wall_ms": wall_ms, "results": results}}, ensure_ascii=False))
"""
    b64 = base64.b64encode(py.encode()).decode()
    out = remote(
        f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"',
        timeout=TIMEOUT_SECS + 300,
    )
    payload = json.loads(out.strip().splitlines()[-1])
    return payload.get("results") or [], int(payload.get("wall_ms") or 0)


def _fetch_phase_rows(tag: str) -> list[dict]:
    py = f"""
import json, urllib.request, re
auth=json.load(open("/root/gptimage/config.json"))["auth-key"]
req=urllib.request.Request(
    "http://127.0.0.1:8012/api/logs?type=call&limit=300",
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
    m=re.search(r"\\|r(\\d{{2}})\\|(\\w+)\\|", blob)
    run_no=int(m.group(1)) if m else 0
    profile=m.group(2) if m else ""
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
        "run": run_no,
        "profile": profile,
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
rows.sort(key=lambda r: (r.get("run") or 0))
print(json.dumps(rows, ensure_ascii=False))
"""
    b64 = base64.b64encode(py.encode()).decode()
    out = remote(f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"', timeout=120)
    return json.loads(out.strip() or "[]")


def _ms_to_s(ms: float | int | None) -> float | None:
    if ms is None:
        return None
    return round(float(ms) / 1000.0, 2)


def _summarize_phases(rows: list[dict]) -> dict:
    out: dict = {}
    for key, _label in PHASE_KEYS:
        vals = [float(r[key]) for r in rows if int(r.get(key) or 0) >= 0]
        if not vals:
            out[key] = {"n": 0}
            continue
        out[key] = {
            "n": len(vals),
            "min_s": _ms_to_s(min(vals)),
            "p50_s": _ms_to_s(_pct(vals, 50)),
            "p95_s": _ms_to_s(_pct(vals, 95)),
            "max_s": _ms_to_s(max(vals)),
            "mean_s": _ms_to_s(sum(vals) / len(vals)),
        }
    return out


def _wall_share_pct(summary: dict) -> dict:
    wall_mean = (summary.get("wall_clock_ms") or {}).get("mean_s") or 0
    if not wall_mean:
        return {}
    shares = {}
    for key, _ in PHASE_KEYS:
        if key == "wall_clock_ms":
            continue
        mean = (summary.get(key) or {}).get("mean_s") or 0
        shares[key] = round(100.0 * mean / wall_mean, 1)
    return shares


def _merge_rows(http_results: list[dict], phase_rows: list[dict]) -> list[dict]:
    phase_by_run = {int(r.get("run") or 0): r for r in phase_rows if int(r.get("run") or 0)}
    merged = []
    for http in sorted(http_results, key=lambda r: r.get("run", 0)):
        run = int(http.get("run") or 0)
        phase = phase_by_run.get(run, {})
        row = {**http, **{k: phase.get(k, 0) for k, _ in PHASE_KEYS}}
        row["http_s"] = _ms_to_s(http.get("elapsed_ms"))
        row["wall_s"] = _ms_to_s(phase.get("wall_clock_ms") or http.get("elapsed_ms"))
        row["sync_wait_s"] = round(max(0.0, (row["http_s"] or 0) - (row["wall_s"] or 0)), 2)
        for key, _ in PHASE_KEYS:
            if key != "wall_clock_ms":
                row[f"{key.replace('_ms', '_s')}"] = _ms_to_s(row.get(key))
        shares = {}
        wall = row.get("wall_clock_ms") or 0
        if wall > 0:
            for key, _ in PHASE_KEYS:
                if key == "wall_clock_ms":
                    continue
                val = int(row.get(key) or 0)
                shares[key] = round(100.0 * val / wall, 1)
        row["phase_share_pct"] = shares
        merged.append(row)
    return merged


def _md_report(payload: dict) -> str:
    lines = [
        f"# 混合 Prompt 并发 — {payload['stamp']}",
        "",
        f"- 通过：**{payload['ok_count']}/{payload['runs_planned']}**",
        f"- 并发数：**{payload['runs_planned']}**",
        f"- 并发墙钟：**{payload['conc_wall_s']} s**",
        f"- 配比：长 prompt {payload['mix']['long']} / 短+pS {payload['mix']['short_ps']} / 短无pS {payload['mix']['short_no_ps']}",
        f"- Prompt 标签：`{payload['prompt_tag']}`",
        f"- response_format：**{payload.get('response_format', 'b64_json')}**",
        "",
        "## HTTP 响应体大小（字节）",
        "",
    ]
    rb = payload.get("resp_bytes") or {}
    if rb.get("n"):
        lines += [
            "| 指标 | bytes |",
            "|------|-------|",
            f"| mean | {rb.get('mean')} |",
            f"| p50 | {rb.get('p50')} |",
            f"| max | {rb.get('max')} |",
            "",
        ]
    lines += [
        "## HTTP 耗时汇总（秒）",
        "",
        "| 指标 | s |",
        "|------|---|",
    ]
    lat = payload.get("http_latency_s") or {}
    for k in ("min_s", "p50_s", "p95_s", "max_s", "mean_s"):
        if lat.get(k) is not None:
            lines.append(f"| {k} | {lat[k]} |")
    lines += [
        "",
        "## 服务端阶段汇总（秒，占墙钟%）",
        "",
        "| 阶段 | p50 | p95 | mean | 占墙钟% |",
        "|------|-----|-----|------|---------|",
    ]
    shares = payload.get("wall_share_pct") or {}
    labels = {k: v for k, v in PHASE_KEYS}
    for key, label in PHASE_KEYS:
        if key == "wall_clock_ms":
            continue
        s = (payload.get("phase_summary") or {}).get(key) or {}
        if not s.get("n"):
            continue
        lines.append(
            f"| {label} | {s.get('p50_s')} | {s.get('p95_s')} | {s.get('mean_s')} | {shares.get(key, '-')} |"
        )
    lines += [
        "",
        "## 逐请求明细（秒）",
        "",
        "| # | 类型 | pS | HTTP | 墙钟 | 同步等待 | 任务排队 | 准入 | pS排队 | 取号 | sS排队 | SSE | poll | 下载 | 墙钟占比 |",
        "|---|------|----|------|------|----------|----------|------|--------|------|--------|-----|------|------|----------|",
    ]
    for r in payload.get("rows") or []:
        sh = r.get("phase_share_pct") or {}
        wall_share = ", ".join(
            f"{labels.get(k, k)}:{sh.get(k, 0)}%"
            for k, _ in PHASE_KEYS
            if k != "wall_clock_ms" and sh.get(k, 0)
        ) or "-"
        lines.append(
            f"| {r.get('run')} | {r.get('profile')} | {'Y' if r.get('uses_ps') else 'N'} | "
            f"{r.get('http_s')} | {r.get('wall_s')} | {r.get('sync_wait_s')} | "
            f"{r.get('task_queue_s', 0)} | {r.get('admit_queue_s', 0)} | {r.get('ps_queue_s', 0)} | "
            f"{r.get('account_queue_s', 0)} | {r.get('ss_queue_s', 0)} | {r.get('sse_stream_s', 0)} | "
            f"{r.get('poll_resolve_s', 0)} | {r.get('download_s', 0)} | {wall_share} |"
        )
    lines.append("")
    return "\n".join(lines)


def _summarize_ints(values: list[int]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": ordered[0],
        "p50": ordered[len(ordered) // 2],
        "max": ordered[-1],
        "mean": int(sum(ordered) / len(ordered)),
    }


def _poll_health_during(seconds: float, interval: float = 5.0) -> list[dict]:
    """Poll Panda /health bandwidth during test (best-effort)."""
    py = """
import json, time, urllib.request
samples = []
deadline = time.time() + %s
while time.time() < deadline:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8012/health?format=json", timeout=10) as r:
            d = json.loads(r.read().decode())
        bw = d.get("bandwidth") or {}
        st = d.get("slot_topology") or {}
        samples.append({
            "ts": time.time(),
            "current_mbps": bw.get("current_mbps"),
            "last_5m_bytes": bw.get("last_5m_bytes"),
            "ss_inflight": st.get("ss_inflight"),
            "ss_queued": st.get("ss_queued"),
        })
    except Exception as exc:
        samples.append({"ts": time.time(), "error": str(exc)})
    time.sleep(%s)
print(json.dumps(samples))
""" % (seconds + 5, interval)
    b64 = base64.b64encode(py.encode()).decode()
    try:
        out = remote(
            f'python3 -c "import base64; exec(base64.b64decode(\'{b64}\').decode())"',
            timeout=seconds + 60,
        )
        return json.loads(out.strip().splitlines()[-1])
    except Exception as exc:
        return [{"error": str(exc)}]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--response-format", dest="response_format", default="b64_json", choices=["b64_json", "url"])
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rf = args.response_format
    tag = f"PROD-mixed-{rf}-conc{args.count}-{stamp}"
    cases = build_cases(args.count)

    auth = _auth()
    emails = _dispatch_emails()
    mix = {
        "long": sum(1 for c in cases if c["profile"] == "long"),
        "short_ps": sum(1 for c in cases if c["profile"] == "short_ps"),
        "short_no_ps": sum(1 for c in cases if c["profile"] == "short_no_ps"),
    }
    print(f"[mixed-{rf}-conc{args.count}] accounts={len(emails)} mix={mix}", flush=True)

    import threading
    health_samples: list[dict] = []
    health_thread = threading.Thread(
        target=lambda: health_samples.extend(_poll_health_during(120, 5.0)),
        daemon=True,
    )
    health_thread.start()

    http_results, conc_wall_ms = _run_remote(auth, emails, cases, tag, rf)
    health_thread.join(timeout=30)
    time.sleep(8)
    phase_rows = _fetch_phase_rows(tag)
    if len(phase_rows) < args.count:
        time.sleep(12)
        phase_rows = _fetch_phase_rows(tag)

    rows = _merge_rows(http_results, phase_rows)
    ok_n = sum(1 for r in rows if r.get("ok"))
    walls_http = [float(r["elapsed_ms"]) for r in rows if r.get("elapsed_ms") is not None]
    resp_bytes = [int(r.get("resp_bytes") or 0) for r in rows if r.get("resp_bytes")]
    phase_summary = _summarize_phases(phase_rows)
    wall_share = _wall_share_pct(phase_summary)
    phase_coverage = sum(
        1 for r in phase_rows if isinstance(r.get("task_queue_ms"), int) or r.get("sse_stream_ms")
    )

    payload = {
        "stamp": stamp,
        "prompt_tag": tag,
        "response_format": rf,
        "runs_planned": args.count,
        "ok_count": ok_n,
        "pass": ok_n == args.count,
        "conc_wall_ms": conc_wall_ms,
        "conc_wall_s": _ms_to_s(conc_wall_ms),
        "mix": mix,
        "resp_bytes": _summarize_ints(resp_bytes),
        "health_samples": health_samples,
        "phase_log_coverage": f"{len([r for r in phase_rows if r.get('sse_stream_ms') or r.get('wall_clock_ms')])}/{args.count}",
        "dispatch_emails": emails,
        "cases": cases,
        "rows": rows,
        "http_latency_s": {
            "n": len(walls_http),
            "min_s": _ms_to_s(min(walls_http)) if walls_http else None,
            "p50_s": _ms_to_s(_pct(walls_http, 50)),
            "p95_s": _ms_to_s(_pct(walls_http, 95)),
            "max_s": _ms_to_s(max(walls_http)) if walls_http else None,
            "mean_s": _ms_to_s(sum(walls_http) / len(walls_http)) if walls_http else None,
        },
        "phase_summary": phase_summary,
        "wall_share_pct": wall_share,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / f"PROD-mixed-{rf}-conc{args.count}-{stamp}.json"
    md_path = OUT_DIR / f"PROD-mixed-{rf}-conc{args.count}-{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_md_report(payload), encoding="utf-8")
    print(json.dumps({"pass": payload["pass"], "ok": ok_n, "json": str(json_path)}, ensure_ascii=False, indent=2))
    print(_md_report(payload))
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
