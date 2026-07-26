#!/usr/bin/env python3
"""STAB suite runner — fair serial5 (A1) and warmup status (B5) on Panda via SSH."""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

REMOTE = "panda"
EMAIL = "qaflowakjewai6ps@proton.me"
BASE = "http://127.0.0.1:8012"
TIMEOUT_SECS = 540
GAP_SECS = 60
PROMPT = (
    "STAB-A1 fair serial: a rainy Tokyo side street at dusk, neon reflections, "
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


def phase_a1(runs: int, gap: float) -> dict:
    auth = _auth()
    results: list[dict] = []
    for i in range(1, runs + 1):
        print(f"[STAB-A1] run {i}/{runs} ...", flush=True)
        results.append(_post_one(auth, i))
        if i < runs and gap > 0:
            time.sleep(gap)
    ok_n = sum(1 for r in results if r.get("ok"))
    summary = {
        "phase": "STAB-A1",
        "account_email": EMAIL,
        "runs": runs,
        "ok_count": ok_n,
        "pass": ok_n == runs,
        "timeout_secs": TIMEOUT_SECS,
        "gap_secs": gap,
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"STAB-serial5-{stamp}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = OUT_DIR / f"STAB-serial5-{stamp}.md"
    md_path.write_text(
        "\n".join(
            [
                f"# STAB-A1 fair serial5 — {stamp}",
                "",
                f"| 项 | 值 |",
                f"|----|-----|",
                f"| 账号 | `{EMAIL}` |",
                f"| 结果 | **{ok_n}/{runs}** |",
                f"| pass | **{summary['pass']}** |",
                f"| timeout | {TIMEOUT_SECS}s |",
                "",
                "## 各轮",
                "",
            ]
            + [
                f"- run {r['run']}: {'OK' if r.get('ok') else 'FAIL'} "
                f"http={r.get('http_code')} wall={r.get('elapsed_ms')}ms b64={r.get('b64_len')}"
                for r in results
            ]
            + ["", f"JSON: `{out_path.name}`"],
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def _remote_python(script: str, *, timeout: float = 60) -> str:
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return remote(
        f"python3 -c \"import base64; exec(base64.b64decode('{b64}').decode())\"",
        timeout=timeout,
    )


def phase_c1_verify() -> dict:
    out = _remote_python(
        """
import json
d=json.load(open('/root/gptimage/config.json'))
print(d.get('newapi_image_sync_wait_timeout_secs', 540))
""".strip()
    ).strip()
    val = float(out.splitlines()[-1])
    summary = {
        "phase": "STAB-C1",
        "newapi_image_sync_wait_timeout_secs": val,
        "pass": 540.0 <= val <= 900.0,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def phase_b4_verify() -> dict:
    out = _remote_python(
        """
import json
d=json.load(open('/root/gptimage/config.json'))
print(d.get('image_binding_inflight_max', 1))
""".strip()
    ).strip()
    binding = int(out.splitlines()[-1] or 0)
    summary = {"phase": "STAB-B4", "image_binding_inflight_max": binding, "pass": binding <= 1}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def phase_conc10(count: int = 10, max_wait: float = 900.0) -> dict:
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
    out_dir = f"/root/gptimage/data/runlogs/stab-conc10-{stamp}"
    remote(f"mkdir -p {out_dir}")
    cmd = (
        f"cd /root/gptimage && python3 {remote_path} "
        f"--base http://127.0.0.1:8012 --config-root /root/gptimage "
        f"--count {count} --max-wait-secs {max_wait} --out-dir {out_dir}"
    )
    raw = remote(cmd, timeout=max_wait + 180, allow_fail=True)
    summary: dict = {"phase": "STAB-conc10", "raw_tail": raw[-3000:]}
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "summary" in payload and isinstance(payload["summary"], dict):
            summary = {**payload["summary"], "report": payload.get("report"), "phase": "STAB-conc10"}
            break
    ok = int(summary.get("completed") or 0)
    failed = int(summary.get("failed") or 0)
    summary["pass"] = ok >= 8 and failed <= count - 8
    summary["ok_count"] = ok
    summary["failed_count"] = failed
    summary["target"] = f">=8/{count}"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"STAB-conc10-{stamp}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md = OUT_DIR / f"STAB-conc10-{stamp}.md"
    md.write_text(
        f"# STAB multiacct conc10 — {stamp}\n\n"
        f"| 成功 | **{ok}/{count}** |\n"
        f"| pass | **{summary['pass']}** |\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def phase_b5() -> dict:
    auth = _auth()
    warmup = json.loads(
        remote(
            f"curl -fsS --max-time 25 -H 'Authorization: Bearer {auth}' "
            f"'{BASE}/api/ops/warmup/status'"
        )
    )
    health = json.loads(remote(f"curl -fsS --max-time 20 '{BASE}/health?format=json'"))
    summary = {
        "phase": "STAB-B5",
        "warmup": warmup,
        "health": {
            "healthy": health.get("healthy"),
            "schedulable": health.get("schedulable"),
            "inflight": health.get("inflight"),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--phase",
        choices=("a1", "b4", "b5", "c1", "conc10", "verify", "all"),
        default="a1",
    )
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--gap-secs", type=float, default=GAP_SECS)
    ap.add_argument("--conc10-count", type=int, default=10)
    args = ap.parse_args()
    if args.phase in ("a1", "all"):
        s = phase_a1(args.runs, args.gap_secs)
        if not s.get("pass"):
            return 1
    if args.phase in ("b4", "verify", "all"):
        if not phase_b4_verify().get("pass"):
            return 1
    if args.phase in ("c1", "verify", "all"):
        if not phase_c1_verify().get("pass"):
            return 1
    if args.phase in ("b5", "verify", "all"):
        phase_b5()
    if args.phase in ("conc10", "all"):
        s = phase_conc10(count=args.conc10_count)
        if not s.get("pass"):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
