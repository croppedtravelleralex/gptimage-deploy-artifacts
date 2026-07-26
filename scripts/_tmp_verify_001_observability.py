#!/usr/bin/env python3
"""VERIFY-001: 2 concurrent gens → 1 call log/task + phase_timings + logs limit."""
from __future__ import annotations

import concurrent.futures
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REMOTE = "panda"
EMAIL = "qaflowakjewai6ps@proton.me"
BASE = "http://127.0.0.1:8012"
PROMPT = "VERIFY-001 observability: a blue square on white background, simple flat design, no text"
OUT = Path(__file__).resolve().parents[1] / "docs" / "captures" / "spa" / f"verify-001-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"


def remote(cmd: str, timeout: float = 300) -> str:
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
    return proc.stdout or ""


def _auth() -> str:
    raw = remote("python3 -c \"import json; print(json.load(open('/root/gptimage/config.json'))['auth-key'])\"")
    return raw.strip()


def _post_image(auth: str, tag: str) -> dict:
    body = json.dumps(
        {
            "model": "gpt-image-2",
            "prompt": f"{PROMPT} [{tag}]",
            "n": 1,
            "response_format": "b64_json",
        }
    ).encode("utf-8")
    # Run on Panda to avoid local network/auth issues
    script = f"""
import json, time, urllib.request, urllib.error
auth = {auth!r}
body = {body!r}
req = urllib.request.Request(
    '{BASE}/v1/images/generations',
    data=body,
    method='POST',
    headers={{
        'Authorization': f'Bearer {{auth}}',
        'Content-Type': 'application/json',
        'X-Preferred-Account-Email': '{EMAIL}',
    }},
)
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=240) as resp:
        raw = resp.read()
        code = resp.status
except urllib.error.HTTPError as exc:
    raw = exc.read()
    code = exc.code
elapsed = round(time.time() - t0, 2)
try:
    data = json.loads(raw.decode('utf-8', 'replace'))
except Exception:
    data = {{'raw': raw[:300].decode('utf-8', 'replace')}}
images = data.get('data') if isinstance(data, dict) else None
b64_len = 0
if isinstance(images, list) and images:
    b64_len = len(str(images[0].get('b64_json') or ''))
print(json.dumps({{
    'tag': {tag!r},
    'ok': code == 200 and b64_len > 1000,
    'http_code': code,
    'elapsed_secs': elapsed,
    'b64_len': b64_len,
    'error': (data.get('detail') if isinstance(data, dict) else None)
        or ((data.get('error') or {{}}).get('message') if isinstance(data, dict) and isinstance(data.get('error'), dict) else (data.get('error') if isinstance(data, dict) else None)),
}}, ensure_ascii=False))
"""
    out = remote(f"python3 - <<'PY'\n{script}\nPY", timeout=300)
    return json.loads(out.strip().splitlines()[-1])


def _inspect_logs(since_iso: str) -> dict:
    script = f"""
import json
from pathlib import Path
from datetime import datetime

since = {since_iso!r}
logs_path = Path('/root/gptimage/data/logs.jsonl')
rows = []
if logs_path.is_file():
    for raw in logs_path.read_text(encoding='utf-8', errors='replace').splitlines()[-8000:]:
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if item.get('type') != 'call':
            continue
        summary = str(item.get('summary') or '')
        if 'VERIFY-001' not in summary and '文生图' not in summary and '图生图' not in summary:
            # keep all recent call logs after since; filter by time below
            pass
        time_s = str(item.get('time') or '')
        if time_s < since:
            continue
        detail = item.get('detail') if isinstance(item.get('detail'), dict) else {{}}
        prompt = str(detail.get('prompt') or detail.get('request_text') or '')
        if 'VERIFY-001' not in prompt and 'VERIFY-001' not in summary:
            continue
        rows.append({{
            'time': time_s,
            'summary': summary,
            'task_id': detail.get('task_id'),
            'status': detail.get('status'),
            'total_wall_ms': detail.get('total_wall_ms') or detail.get('duration_ms'),
            'phase_timings_ms': detail.get('phase_timings_ms') if isinstance(detail.get('phase_timings_ms'), dict) else {{}},
            'completion_tokens': detail.get('completion_tokens'),
            'tokens_per_sec': detail.get('tokens_per_sec'),
            'upload_bytes': detail.get('upload_bytes'),
            'download_bytes': detail.get('download_bytes'),
        }})

# also check API limit
import urllib.request
auth = json.load(open('/root/gptimage/config.json'))['auth-key']
req = urllib.request.Request(
    'http://127.0.0.1:8012/api/logs?type=call&limit=200',
    headers={{'Authorization': f'Bearer {{auth}}'}},
)
with urllib.request.urlopen(req, timeout=60) as resp:
    api = json.loads(resp.read().decode('utf-8'))
items = api.get('items') if isinstance(api, dict) else []
print(json.dumps({{
    'verify_call_logs': rows,
    'api_logs_limit_200_count': len(items) if isinstance(items, list) else -1,
    'api_ok': isinstance(items, list),
}}, ensure_ascii=False))
"""
    out = remote(f"python3 - <<'PY'\n{script}\nPY", timeout=120)
    return json.loads(out.strip().splitlines()[-1])


def main() -> int:
    auth = _auth()
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # local clock may differ; use remote time
    since = remote("date -u +'%Y-%m-%d %H:%M:%S'").strip()

    print(f"[1/3] fire 2 concurrent /v1/images (email={EMAIL})")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(_post_image, auth, f"c{i+1}") for i in range(2)]
        results = [f.result() for f in futs]
    print(json.dumps(results, ensure_ascii=False, indent=2))

    print("[2/3] wait 3s then inspect call logs")
    time.sleep(3)
    inspect = _inspect_logs(since)
    logs = inspect.get("verify_call_logs") or []

    by_task: dict[str, list] = {}
    thin_without_task = 0
    rich_with_phases = 0
    for row in logs:
        tid = str(row.get("task_id") or "").strip()
        phases = row.get("phase_timings_ms") if isinstance(row.get("phase_timings_ms"), dict) else {}
        if tid:
            by_task.setdefault(tid, []).append(row)
            if phases:
                rich_with_phases += 1
        else:
            thin_without_task += 1

    checks = {
        "both_ok": all(r.get("ok") for r in results),
        "call_log_rows": len(logs),
        "unique_task_ids": len(by_task),
        "max_logs_per_task": max((len(v) for v in by_task.values()), default=0),
        "thin_without_task_id": thin_without_task,
        "all_tasks_single_log": bool(by_task) and all(len(v) == 1 for v in by_task.values()) and thin_without_task == 0,
        "all_have_phase_chips": bool(logs)
        and rich_with_phases == len(logs)
        and all(
            isinstance(row.get("phase_timings_ms"), dict) and bool(row.get("phase_timings_ms"))
            for row in logs
        ),
        "api_logs_limit_200_ok": bool(inspect.get("api_ok")) and int(inspect.get("api_logs_limit_200_count") or 0) >= 0,
        "api_logs_limit_200_count": inspect.get("api_logs_limit_200_count"),
    }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since": since,
        "email": EMAIL,
        "results": results,
        "checks": checks,
        "logs": logs,
        "pass": bool(
            checks["both_ok"]
            and checks["call_log_rows"] == 2
            and checks["unique_task_ids"] == 2
            and checks["all_tasks_single_log"]
            and checks["all_have_phase_chips"]
            and checks["api_logs_limit_200_ok"]
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"pass": report["pass"], "checks": checks, "evidence": str(OUT)}, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
