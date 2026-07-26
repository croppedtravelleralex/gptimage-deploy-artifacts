#!/usr/bin/env python3
"""Image pipeline conc10 acceptance via /api/image-tasks/generations (Panda localhost)."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_auth_key(root: Path) -> str:
    for candidate in (root / "config.json", Path("/root/gptimage/config.json")):
        if candidate.is_file():
            cfg = json.loads(candidate.read_text(encoding="utf-8"))
            key = str(cfg.get("auth-key") or cfg.get("auth_key") or "").strip()
            if key:
                return key
    raise RuntimeError("auth key not found in config.json")


def _http(
    base: str,
    path: str,
    *,
    auth_key: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 120,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {auth_key}"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = raw[:800].decode("utf-8", "replace")
            return {"ok": True, "status": resp.status, "body": parsed, "elapsed_ms": round((time.time() - started) * 1000, 2)}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = raw[:800].decode("utf-8", "replace")
        return {"ok": False, "status": exc.code, "body": parsed, "elapsed_ms": round((time.time() - started) * 1000, 2)}
    except Exception as exc:
        return {"ok": False, "status": 0, "body": str(exc), "elapsed_ms": round((time.time() - started) * 1000, 2)}


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def _dispatchable_image_emails(base: str, auth_key: str) -> list[str]:
    res = _http(base, "/api/accounts?limit=500", auth_key=auth_key, timeout=60)
    body = res.get("body") if isinstance(res.get("body"), dict) else {}
    items = body.get("items") if isinstance(body.get("items"), list) else []
    emails = sorted(
        {
            str(item.get("email") or "").strip()
            for item in items
            if isinstance(item, dict)
            and item.get("image_schedulable")
            and str(item.get("email") or "").strip()
        }
    )
    return emails


def _submit_one(
    base: str,
    auth_key: str,
    idx: int,
    run_id: str,
    *,
    preferred_account_email: str = "",
) -> dict[str, Any]:
    body = {
        "client_task_id": f"{run_id}-conc10-{idx:02d}",
        "prompt": f"A minimal product photo of a ceramic mug on wood table, soft daylight, no text, variant {idx}",
        "model": "gpt-image-2",
        "size": "1024x1024",
        "quality": "auto",
        "prompt_enhance": False,
    }
    if preferred_account_email:
        body["preferred_account_email"] = preferred_account_email
    started = time.time()
    res = _http(base, "/api/image-tasks/generations", auth_key=auth_key, method="POST", body=body, timeout=180)
    out: dict[str, Any] = {
        "idx": idx,
        "client_task_id": body["client_task_id"],
        "preferred_account_email": preferred_account_email,
        "submit_ok": res.get("ok"),
        "submit_status": res.get("status"),
        "submit_ms": round((time.time() - started) * 1000, 2),
    }
    if isinstance(res.get("body"), dict):
        out["task_id"] = res["body"].get("id") or body["client_task_id"]
        out["body_status"] = res["body"].get("status")
    else:
        out["error"] = res.get("body")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("PIPELINE_ACCEPT_BASE", "http://127.0.0.1:80"))
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument("--max-wait-secs", type=float, default=900.0)
    ap.add_argument("--out-dir", default="/app/data/runlogs/spa_repro/pipeline-conc10")
    ap.add_argument("--config-root", default="/app")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"pipe-conc10-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    auth_key = _load_auth_key(Path(args.config_root))
    dispatch_emails = _dispatchable_image_emails(args.base, auth_key)
    if not dispatch_emails:
        print(json.dumps({"warning": "no image_schedulable accounts; falling back to pool auto-pick"}))

    snap_before = _http(args.base, "/api/ops/image-pipeline/snapshot", auth_key=auth_key, timeout=30)
    wall_start = time.time()
    submits: list[dict[str, Any]] = []

    def _pick_email(idx: int) -> str:
        if not dispatch_emails:
            return ""
        return dispatch_emails[idx % len(dispatch_emails)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.count) as pool:
        futures = [
            pool.submit(_submit_one, args.base, auth_key, i, run_id, preferred_account_email=_pick_email(i))
            for i in range(args.count)
        ]
        for fut in concurrent.futures.as_completed(futures):
            submits.append(fut.result())
    submits.sort(key=lambda x: x.get("idx", 0))
    task_ids = [str(s.get("task_id") or s.get("client_task_id")) for s in submits if s.get("submit_ok")]

    poll_log: list[dict[str, Any]] = []
    final_items: list[dict[str, Any]] = []
    deadline = time.time() + args.max_wait_secs
    while time.time() < deadline and task_ids:
        ids = urllib.parse.quote(",".join(task_ids))
        res = _http(args.base, f"/api/image-tasks/status?ids={ids}", auth_key=auth_key, timeout=60)
        items = []
        if isinstance(res.get("body"), dict) and isinstance(res["body"].get("items"), list):
            items = res["body"]["items"]
        counts: dict[str, int] = {}
        for item in items:
            st = str(item.get("status") or "unknown")
            counts[st] = counts.get(st, 0) + 1
        snap = _http(args.base, "/api/ops/image-pipeline/snapshot", auth_key=auth_key, timeout=30)
        snap_body = snap.get("body") if snap.get("ok") and isinstance(snap.get("body"), dict) else {}
        poll_log.append(
            {
                "t_secs": round(time.time() - wall_start, 2),
                "counts": counts,
                "pipeline_in_flight": snap_body.get("in_flight"),
                "ss_active": (snap_body.get("ss") or {}).get("active"),
                "ss_queued": (snap_body.get("ss") or {}).get("queued"),
            }
        )
        terminal = {"success", "error", "cancelled", "completed", "failed"}
        if items and all(str(i.get("status") or "") in terminal for i in items):
            final_items = items
            break
        time.sleep(args.poll_interval)

    wall_ms = round((time.time() - wall_start) * 1000, 2)
    snap_after = _http(args.base, "/api/ops/image-pipeline/snapshot", auth_key=auth_key, timeout=30)

    ss_queue: list[float] = []
    wall_clocks: list[float] = []
    for item in final_items:
        timings = item.get("phase_timings_ms") if isinstance(item.get("phase_timings_ms"), dict) else {}
        if isinstance(timings, dict):
            if timings.get("ss_queue_ms") is not None:
                ss_queue.append(float(timings["ss_queue_ms"]))
            wc = timings.get("wall_clock_ms") or item.get("wall_clock_ms")
            if wc is not None:
                wall_clocks.append(float(wc))

    report = {
        "run_id": run_id,
        "count": args.count,
        "dispatch_accounts": dispatch_emails,
        "submitted": len(submits),
        "submit_ok": sum(1 for s in submits if s.get("submit_ok")),
        "wall_clock_ms": wall_ms,
        "final_status_counts": {},
        "ss_queue_ms": {
            "n": len(ss_queue),
            "p50": _pct(ss_queue, 50),
            "p95": _pct(ss_queue, 95),
            "max": round(max(ss_queue), 2) if ss_queue else None,
        },
        "task_wall_clock_ms": {
            "n": len(wall_clocks),
            "p50": _pct(wall_clocks, 50),
            "p95": _pct(wall_clocks, 95),
            "max": round(max(wall_clocks), 2) if wall_clocks else None,
        },
        "submits": submits,
        "final_items": [
            {
                "id": i.get("id"),
                "status": i.get("status"),
                "wall_clock_ms": i.get("wall_clock_ms"),
                "phase_timings_ms": i.get("phase_timings_ms"),
                "error": i.get("error"),
            }
            for i in final_items
        ],
        "pipeline_snapshot_before": snap_before.get("body"),
        "pipeline_snapshot_after": snap_after.get("body"),
        "poll_log": poll_log,
    }
    for item in final_items:
        st = str(item.get("status") or "unknown")
        report["final_status_counts"][st] = report["final_status_counts"].get(st, 0) + 1

    out_path = out_dir / f"{run_id}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "report": str(out_path), "summary": {
        "wall_clock_ms": wall_ms,
        "completed": report["final_status_counts"].get("success", 0) + report["final_status_counts"].get("completed", 0),
        "failed": report["final_status_counts"].get("failed", 0) + report["final_status_counts"].get("error", 0),
        "ss_queue_p50": report["ss_queue_ms"]["p50"],
        "ss_queue_p95": report["ss_queue_ms"]["p95"],
        "task_wall_p50": report["task_wall_clock_ms"]["p50"],
    }}, ensure_ascii=False))
    failed = report["final_status_counts"].get("failed", 0) + report["final_status_counts"].get("error", 0)
    completed = report["final_status_counts"].get("success", 0) + report["final_status_counts"].get("completed", 0)
    return 0 if completed >= args.count and failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
