#!/usr/bin/env python3
"""IMG-018 sync admission / ETA / pacing acceptance harness.

Runs low-risk canary ladders against Panda (default) or NewAPI.

Environment:
  IMG018_TARGET=panda|newapi          default panda
  PANDA_PUBLIC_BASE                   default https://gptimage.relai.asia
  PANDA_AUTH_KEY                      optional; ssh panda fetch if empty
  NEWAPI_BASE_URL / NEWAPI_API_KEY    required when target=newapi
  IMG018_CASES=C1,C2,C3               default C1,C2,C3
  IMG018_MODEL                        default gpt-image-2
  IMG018_READ_TIMEOUT                 default 600
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

TARGET = os.getenv("IMG018_TARGET", "panda").strip().lower()
PANDA_PUBLIC = os.getenv("PANDA_PUBLIC_BASE", "https://gptimage.relai.asia").rstrip("/")
PANDA_KEY = os.getenv("PANDA_AUTH_KEY", "").strip()
NEWAPI_BASE = os.getenv("NEWAPI_BASE_URL", "").rstrip("/")
NEWAPI_KEY = os.getenv("NEWAPI_API_KEY", "").strip()
MODEL = os.getenv("IMG018_MODEL", "gpt-image-2").strip() or "gpt-image-2"
READ_TIMEOUT = float(os.getenv("IMG018_READ_TIMEOUT", "600"))
CASES = [item.strip().upper() for item in os.getenv("IMG018_CASES", "C1,C2,C3").split(",") if item.strip()]
PROMPT = os.getenv("IMG018_PROMPT", "a cute red panda, simple illustration").strip()


def fetch_panda_auth_key() -> str:
    proc = subprocess.run(
        [
            "ssh",
            "-o",
            "ConnectTimeout=15",
            "panda",
            "python3 -c \"import json;print(json.load(open('/root/gptimage/config.json')).get('auth-key',''))\"",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if proc.returncode != 0:
        raise SystemExit(f"failed to fetch Panda auth key: {proc.stderr}")
    return proc.stdout.strip()


def resolve_target() -> tuple[str, str, str]:
    if TARGET == "newapi":
        if not NEWAPI_BASE or not NEWAPI_KEY:
            raise SystemExit("NEWAPI_BASE_URL and NEWAPI_API_KEY are required for target=newapi")
        return NEWAPI_BASE, NEWAPI_KEY, "newapi"
    key = PANDA_KEY or fetch_panda_auth_key()
    if not key:
        raise SystemExit("empty Panda auth key")
    return PANDA_PUBLIC, key, "panda"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = int(round((len(ordered) - 1) * p))
    return ordered[max(0, min(len(ordered) - 1, idx))]


def post_generation(client: httpx.Client, base: str, key: str, *, prompt: str, panda_async: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": MODEL,
        "prompt": f"panda-async: {prompt}" if panda_async else prompt,
        "n": 1,
        "response_format": "b64_json",
        "size": "1024x1024",
        "quality": "low",
    }
    started = time.perf_counter()
    try:
        response = client.post(
            f"{base}/v1/images/generations",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=httpx.Timeout(READ_TIMEOUT, connect=30.0),
        )
        elapsed = time.perf_counter() - started
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text[:500]}
        data = payload.get("data") if isinstance(payload, dict) else None
        has_image = False
        if isinstance(data, list) and data:
            first = data[0] if isinstance(data[0], dict) else {}
            has_image = bool(first.get("b64_json") or first.get("url"))
        empty_success = response.status_code == 200 and isinstance(data, list) and not has_image and payload.get("object") == "image.task"
        return {
            "ok": response.status_code == 200 and has_image,
            "status_code": response.status_code,
            "elapsed": elapsed,
            "has_image": has_image,
            "empty_task_200": empty_success,
            "busy_429": response.status_code == 429,
            "task_id": (payload.get("task_id") if isinstance(payload, dict) else None),
            "object": (payload.get("object") if isinstance(payload, dict) else None),
            "error": ((payload.get("error") or {}) if isinstance(payload, dict) else {}),
            "payload": payload,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "elapsed": time.perf_counter() - started,
            "has_image": False,
            "empty_task_200": False,
            "busy_429": False,
            "error": {"message": str(exc)},
            "payload": None,
        }


def poll_task(client: httpx.Client, base: str, key: str, task_id: str, *, timeout_secs: float = 540.0) -> dict[str, Any]:
    deadline = time.time() + timeout_secs
    last: dict[str, Any] = {}
    while time.time() < deadline:
        response = client.post(
            f"{base}/v1/images/generations",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": MODEL, "prompt": f"panda status {task_id}"},
            timeout=httpx.Timeout(60.0, connect=30.0),
        )
        payload = response.json()
        last = {"status_code": response.status_code, "payload": payload}
        if response.status_code == 200 and isinstance(payload, dict):
            data = payload.get("data") or []
            if isinstance(data, list) and data and (data[0].get("b64_json") or data[0].get("url")):
                return {"ok": True, "payload": payload}
            if payload.get("object") == "image.task" and payload.get("status") in {"error", "success"}:
                return {"ok": payload.get("status") == "success", "payload": payload}
        time.sleep(2.0)
    return {"ok": False, "payload": last}


def run_case(client: httpx.Client, base: str, key: str, case: str) -> dict[str, Any]:
    if case == "C1":
        rows = [post_generation(client, base, key, prompt=PROMPT) for _ in range(3)]
        success = [row for row in rows if row["ok"]]
        elapsed = [row["elapsed"] for row in success]
        passed = len(success) == 3 and not any(row["empty_task_200"] for row in rows) and percentile(elapsed, 0.5) < 90
        return {"case": case, "passed": passed, "rows": rows, "p50": percentile(elapsed, 0.5), "p95": percentile(elapsed, 0.95)}
    if case == "C2":
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: post_generation(client, base, key, prompt=PROMPT), range(2)))
        passed = all(row["ok"] for row in rows) and not any(row["empty_task_200"] for row in rows)
        return {"case": case, "passed": passed, "rows": rows}
    if case == "C3":
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: post_generation(client, base, key, prompt=PROMPT), range(4)))
        success = [row for row in rows if row["ok"]]
        failures = [row for row in rows if not row["ok"]]
        bad = [row for row in failures if not row["busy_429"]]
        elapsed = [row["elapsed"] for row in success]
        passed = (
            len(success) >= 3
            and not any(row["empty_task_200"] for row in rows)
            and not bad
            and percentile(elapsed, 0.95) <= 180
        )
        return {"case": case, "passed": passed, "rows": rows, "success": len(success), "p95": percentile(elapsed, 0.95)}
    if case == "C4":
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(lambda _: post_generation(client, base, key, prompt=PROMPT), range(6)))
        covered = all(row["ok"] or row["busy_429"] for row in rows)
        passed = covered and not any(row["empty_task_200"] for row in rows) and all(row["has_image"] for row in rows if row["ok"])
        return {"case": case, "passed": passed, "rows": rows}
    if case == "C5":
        rows = []
        for index in range(8):
            rows.append(post_generation(client, base, key, prompt=PROMPT))
            if index < 7:
                time.sleep(2.0)
        success = [row for row in rows if row["ok"]]
        elapsed = [row["elapsed"] for row in success]
        passed = not any(row["empty_task_200"] for row in rows) and len(success) >= 4
        return {"case": case, "passed": passed, "rows": rows, "success": len(success), "p95": percentile(elapsed, 0.95)}
    if case == "C6":
        submits = [post_generation(client, base, key, prompt=PROMPT, panda_async=True) for _ in range(4)]
        finals = []
        for row in submits:
            task_id = row.get("task_id") or ((row.get("payload") or {}).get("task_id") if isinstance(row.get("payload"), dict) else None)
            if not task_id:
                finals.append({"ok": False, "reason": "missing_task_id", "submit": row})
                continue
            finals.append(poll_task(client, base, key, str(task_id)))
        passed = all(item.get("ok") for item in finals)
        return {"case": case, "passed": passed, "submits": submits, "finals": finals}
    raise SystemExit(f"unknown case: {case}")


def main() -> int:
    base, key, label = resolve_target()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path("reports") / f"img018-admission-{label}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"target": label, "base": base, "cases": {}, "started_at": stamp}
    print(f"IMG-018 target={label} base={base} cases={CASES}")
    with httpx.Client(http2=False) as client:
        for case in CASES:
            print(f"--- running {case} ---")
            result = run_case(client, base, key, case)
            summary["cases"][case] = {
                "passed": bool(result.get("passed")),
                "success": result.get("success"),
                "p50": result.get("p50"),
                "p95": result.get("p95"),
                "row_count": len(result.get("rows") or result.get("finals") or []),
            }
            (out_dir / f"{case}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{case} passed={result.get('passed')} detail={summary['cases'][case]}")
    summary["all_passed"] = all(item["passed"] for item in summary["cases"].values())
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
