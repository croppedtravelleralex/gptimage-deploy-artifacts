#!/usr/bin/env python3
"""IMG-013 NewAPI async-tunnel loadgen.

通过 NewAPI baseurl/key 走 `/v1/images/*`：

1. submit: `panda_async=true`，Panda 立即返回 `image.task`。
2. poll: `/v1/images/generations` + `panda_task_id`，穿过 NewAPI 查询并在成功时返回 OpenAI image response。

环境变量：
  NEWAPI_BASE_URL=https://sub2api.closeapi.top
  NEWAPI_API_KEY=<required>
  IMG013_STAGE=24|30|36
  IMG013_ROUNDS=1
  IMG013_USE_PANDA_ASSETS=1  # 先直传 Panda asset，再用 pointer 小文件穿 NewAPI
"""
from __future__ import annotations

import base64
import concurrent.futures
import datetime as dt
import importlib.util
import json
import math
import os
import statistics
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "scripts" / "r56_stage18_local_loadgen.py"
spec = importlib.util.spec_from_file_location("r56", LEGACY)
legacy = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(legacy)

STAGE = int(os.getenv("IMG013_STAGE", "24"))
ROUNDS = int(os.getenv("IMG013_ROUNDS", "1"))
NEWAPI_BASE = os.getenv("NEWAPI_BASE_URL", "https://sub2api.closeapi.top").rstrip("/")
NEWAPI_KEY = os.getenv("NEWAPI_API_KEY", "").strip()
PANDA_PUBLIC = os.getenv("PANDA_PUBLIC_BASE", "https://gptimage.relai.asia").rstrip("/")
PANDA_KEY = os.getenv("PANDA_AUTH_KEY", "").strip()
USE_PANDA_ASSETS = os.getenv("IMG013_USE_PANDA_ASSETS", "1").strip().lower() in {"1", "true", "yes", "on"}
HTTP2_ENABLED = os.getenv("IMG013_HTTP2", "1").strip().lower() in {"1", "true", "yes", "on"}
SUBMIT_WINDOW = max(1, int(os.getenv("IMG013_SUBMIT_WINDOW", str(STAGE))))
ASSET_UPLOAD_WINDOW = max(1, int(os.getenv("IMG013_ASSET_UPLOAD_WINDOW", "6")))
POLL_INTERVAL = max(1.0, float(os.getenv("IMG013_POLL_INTERVAL", "3")))
POLL_INTERVAL_MAX = max(POLL_INTERVAL, float(os.getenv("IMG013_POLL_INTERVAL_MAX", "18")))
POLL_INTERVAL_FACTOR = max(1.0, float(os.getenv("IMG013_POLL_INTERVAL_FACTOR", "1.35")))
POLL_TIMEOUT = max(60.0, float(os.getenv("IMG013_POLL_TIMEOUT", "900")))
CONNECT_TIMEOUT = float(os.getenv("IMG013_CONNECT_TIMEOUT", "30"))
READ_TIMEOUT = float(os.getenv("IMG013_READ_TIMEOUT", "120"))

if STAGE not in {6, 12, 18, 24, 30, 36}:
    raise SystemExit("IMG013_STAGE must be 6, 12, 18, 24, 30, or 36")
if not NEWAPI_KEY:
    raise SystemExit("NEWAPI_API_KEY is required")

STAMP = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
SUITE_ID = f"img013-newapi-async-stage{STAGE}-{ROUNDS}rounds-{STAMP}"
REPORT = ROOT / "reports" / SUITE_ID
REPORT.mkdir(parents=True, exist_ok=True)

CLIENT = httpx.Client(
    http2=HTTP2_ENABLED,
    limits=httpx.Limits(max_connections=max(SUBMIT_WINDOW, 36), max_keepalive_connections=max(SUBMIT_WINDOW, 36)),
    timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=60.0),
)
ASSET_SEM = threading.BoundedSemaphore(ASSET_UPLOAD_WINDOW)
NEWAPI_HEADERS = {
    "Authorization": f"Bearer {NEWAPI_KEY}",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept": "application/json",
}


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * p) - 1))]


def panda_auth_key() -> str:
    if PANDA_KEY:
        return PANDA_KEY
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
        raise RuntimeError(f"failed to fetch Panda auth key: {proc.stderr}")
    key = (proc.stdout or "").strip()
    if not key:
        raise RuntimeError("PANDA_AUTH_KEY is required")
    return key


PANDA_ASSET_KEY = panda_auth_key() if USE_PANDA_ASSETS else ""


def data_url_to_file(data_url: str, filename: str) -> tuple[str, bytes, str]:
    header, _, payload = data_url.partition(",")
    mime = header.split(";", 1)[0].removeprefix("data:") or "image/png"
    return filename, base64.b64decode(payload), mime


def build_tasks(round_no: int, run_id: str) -> list[dict[str, Any]]:
    legacy_stage = STAGE if STAGE in {18, 24, 30} else 18
    legacy.STAGE = legacy_stage
    legacy.RUN_ID = run_id
    legacy.LOCAL_REPORT = REPORT
    legacy.random.seed(2026070613 + round_no)
    raw_tasks = legacy.build_tasks()
    if STAGE < legacy_stage:
        gen_target = {6: 2, 12: 4}.get(STAGE, max(1, STAGE // 3))
        raw_tasks = [t for t in raw_tasks if t.get("kind") == "generation"][:gen_target] + [
            t for t in raw_tasks if t.get("kind") == "edit"
        ][: STAGE - gen_target]
    elif STAGE == 36:
        # 30 档模板外再补 6 个文生图，避免额外参考图上传把验收污染成纯上传测试。
        legacy.STAGE = 30
        raw_tasks = legacy.build_tasks()
        for idx in range(6):
            raw_tasks.append(
                {
                    "kind": "generation",
                    "body": {
                        "client_task_id": f"{run_id}-extra-gen-{idx+1}",
                        "prompt": f"高质量电影感产品海报，复杂光影，细节丰富，编号 {idx+1}",
                        "model": "gpt-image-2",
                        "size": "1024x1024",
                        "quality": "auto",
                    },
                    "prompt_family": "extra_generation",
                }
            )
    tasks: list[dict[str, Any]] = []
    for index, task in enumerate(raw_tasks[:STAGE], start=1):
        body = dict(task["body"])
        client_task_id = body.get("client_task_id") or f"{run_id}-task-{index:02d}"
        body["client_task_id"] = client_task_id
        body["response_format"] = "url"
        if task["kind"] == "generation":
            body.pop("panda_async", None)
            body["prompt"] = "panda-async: " + str(body.get("prompt") or "")
            tasks.append(
                {
                    "kind": "generation",
                    "client_task_id": client_task_id,
                    "endpoint": "/v1/images/generations",
                    "json_body": body,
                    "reference_count": 0,
                    "reference_total_bytes": 0,
                    "prompt_family": task.get("prompt_family"),
                }
            )
            continue
        images = body.pop("images", [])
        ref_names = task.get("reference_names") or [f"reference_{i + 1}.png" for i in range(len(images))]
        files = [
            data_url_to_file(images[i], ref_names[i] if i < len(ref_names) else f"reference_{i + 1}.png")
            for i in range(len(images))
        ]
        tasks.append(
            {
                "kind": "edit",
                "client_task_id": client_task_id,
                "endpoint": "/v1/images/edits",
                "form_fields": {
                    "client_task_id": client_task_id,
                    "panda_async": "true",
                    "prompt": body.get("prompt", ""),
                    "model": body.get("model", "gpt-image-2"),
                    "size": body.get("size", "1024x1024"),
                    "quality": body.get("quality", "auto"),
                    "response_format": "url",
                },
                "files": files,
                "reference_count": len(files),
                "reference_total_bytes": sum(len(item[1]) for item in files),
                "prompt_family": task.get("prompt_family"),
            }
        )
    return tasks


def upload_reference_assets(files: list[tuple[str, bytes, str]]) -> list[str]:
    with ASSET_SEM:
        resp = CLIENT.post(
            PANDA_PUBLIC + "/api/image-assets/references",
            headers={"Authorization": f"Bearer {PANDA_ASSET_KEY}"},
            files=[("image", (filename, data, mime)) for filename, data, mime in files],
        )
        resp.raise_for_status()
        body = resp.json()
        items = body.get("items") if isinstance(body, dict) else None
        asset_ids = [str(item.get("asset_id") or "").strip() for item in (items or []) if isinstance(item, dict)]
        asset_ids = [item for item in asset_ids if item]
        if not asset_ids:
            raise RuntimeError(f"asset upload returned no ids: {(resp.text or '')[:300]}")
        return asset_ids


def submit_task(task: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    asset_upload_ms = 0.0
    newapi_submit_ms = 0.0
    submit_phase = "prepare"
    try:
        if task["kind"] == "generation":
            submit_phase = "newapi_submit"
            newapi_started = time.time()
            resp = CLIENT.post(
                NEWAPI_BASE + task["endpoint"],
                headers=NEWAPI_HEADERS,
                json=task["json_body"],
            )
            newapi_submit_ms = round((time.time() - newapi_started) * 1000, 2)
        else:
            data = dict(task["form_fields"])
            files = []
            if USE_PANDA_ASSETS and task.get("files"):
                submit_phase = "asset_upload"
                asset_started = time.time()
                asset_ids = upload_reference_assets(task["files"])
                asset_upload_ms = round((time.time() - asset_started) * 1000, 2)
                data["asset_ids"] = ",".join(asset_ids)
                for idx, asset_id in enumerate(asset_ids, start=1):
                    files.append(
                        (
                            "image",
                            (
                                f"panda-asset-{idx}.txt",
                                f"panda-asset://{asset_id}".encode("utf-8"),
                                "text/plain",
                            ),
                        )
                    )
            else:
                files = [("image", (filename, content, mime)) for filename, content, mime in task.get("files") or []]
            submit_phase = "newapi_submit"
            newapi_started = time.time()
            resp = CLIENT.post(
                NEWAPI_BASE + task["endpoint"],
                headers=NEWAPI_HEADERS,
                data=data,
                files=files or None,
            )
            newapi_submit_ms = round((time.time() - newapi_started) * 1000, 2)
        elapsed_ms = round((time.time() - started) * 1000, 2)
        body = resp.json() if resp.content else {}
        task_id = str(body.get("task_id") or body.get("id") or "").strip() if isinstance(body, dict) else ""
        ok = 200 <= resp.status_code < 300 and task_id and isinstance(body, dict) and body.get("object") == "image.task"
        return {
            "ok": bool(ok),
            "status_code": resp.status_code,
            "elapsed_ms": elapsed_ms,
            "asset_upload_ms": asset_upload_ms,
            "newapi_submit_ms": newapi_submit_ms,
            "kind": task["kind"],
            "client_task_id": task["client_task_id"],
            "task_id": task_id,
            "task_status": body.get("status") if isinstance(body, dict) else "",
            "error": "" if ok else (resp.text or "")[:1000],
            "reference_count": task.get("reference_count", 0),
            "reference_total_bytes": task.get("reference_total_bytes", 0),
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "asset_upload_ms": asset_upload_ms,
            "newapi_submit_ms": newapi_submit_ms,
            "failed_phase": submit_phase,
            "kind": task.get("kind"),
            "client_task_id": task.get("client_task_id"),
            "error": repr(exc),
            "reference_count": task.get("reference_count", 0),
            "reference_total_bytes": task.get("reference_total_bytes", 0),
        }


def poll_task(task_id: str) -> dict[str, Any]:
    started = time.time()
    attempts = 0
    next_interval = POLL_INTERVAL
    last: dict[str, Any] = {}
    while time.time() - started < POLL_TIMEOUT:
        attempts += 1
        try:
            resp = CLIENT.post(
                NEWAPI_BASE + "/v1/images/generations",
                headers=NEWAPI_HEADERS,
                json={
                    "model": "gpt-image-2",
                    "prompt": f"panda status {task_id}",
                    "response_format": "url",
                },
            )
            body = resp.json() if resp.content else {}
            last = body if isinstance(body, dict) else {"raw": str(body)[:500]}
            if 200 <= resp.status_code < 300 and isinstance(body, dict):
                if isinstance(body.get("data"), list) and body.get("data"):
                    return {
                        "ok": True,
                        "task_id": task_id,
                        "status": "success",
                        "elapsed_ms": round((time.time() - started) * 1000, 2),
                        "attempts": attempts,
                        "poll_interval_initial_sec": POLL_INTERVAL,
                        "poll_interval_max_sec": POLL_INTERVAL_MAX,
                        "data_count": len(body.get("data") or []),
                    }
                panda_task = body.get("panda_task") if isinstance(body.get("panda_task"), dict) else {}
                status = str(body.get("status") or panda_task.get("status") or "")
                if status == "error":
                    return {
                        "ok": False,
                        "task_id": task_id,
                        "status": "error",
                        "elapsed_ms": round((time.time() - started) * 1000, 2),
                        "attempts": attempts,
                        "poll_interval_initial_sec": POLL_INTERVAL,
                        "poll_interval_max_sec": POLL_INTERVAL_MAX,
                        "error": str(body.get("error") or panda_task.get("error") or "")[:1000],
                    }
        except Exception as exc:
            last = {"exception": repr(exc)}
        time.sleep(next_interval)
        next_interval = min(POLL_INTERVAL_MAX, max(POLL_INTERVAL, next_interval * POLL_INTERVAL_FACTOR))
    return {
        "ok": False,
        "task_id": task_id,
        "status": "poll_timeout",
        "elapsed_ms": round((time.time() - started) * 1000, 2),
        "attempts": attempts,
        "poll_interval_initial_sec": POLL_INTERVAL,
        "poll_interval_max_sec": POLL_INTERVAL_MAX,
        "last": last,
    }


def panda_health() -> dict[str, Any]:
    try:
        resp = httpx.get(PANDA_PUBLIC + "/health?format=json", timeout=20)
        return resp.json() if resp.status_code == 200 else {"status_code": resp.status_code}
    except Exception as exc:
        return {"error": repr(exc)}


def run_round(round_no: int) -> dict[str, Any]:
    run_id = f"{SUITE_ID}-r{round_no}"
    tasks = build_tasks(round_no, run_id)
    pre_health = panda_health()
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=SUBMIT_WINDOW) as pool:
        submit_results = list(pool.map(submit_task, tasks))
    accepted_ids = [item["task_id"] for item in submit_results if item.get("ok") and item.get("task_id")]
    submit_done_ts = time.time()
    poll_results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max(1, len(accepted_ids)), 36)) as pool:
        futures = [pool.submit(poll_task, task_id) for task_id in accepted_ids]
        for fut in concurrent.futures.as_completed(futures):
            poll_results.append(fut.result())
    post_health = panda_health()
    submit_elapsed = [float(item.get("elapsed_ms") or 0) for item in submit_results if item.get("elapsed_ms")]
    asset_upload_elapsed = [
        float(item.get("asset_upload_ms") or 0)
        for item in submit_results
        if float(item.get("asset_upload_ms") or 0) > 0
    ]
    newapi_submit_elapsed = [
        float(item.get("newapi_submit_ms") or 0)
        for item in submit_results
        if float(item.get("newapi_submit_ms") or 0) > 0
    ]
    final_elapsed = [float(item.get("elapsed_ms") or 0) for item in poll_results if item.get("elapsed_ms")]
    poll_attempts = [int(item.get("attempts") or 0) for item in poll_results]
    summary = {
        "round": round_no,
        "run_id": run_id,
        "requested": len(tasks),
        "submit_ok": sum(1 for item in submit_results if item.get("ok")),
        "submit_failed": sum(1 for item in submit_results if not item.get("ok")),
        "final_success": sum(1 for item in poll_results if item.get("ok")),
        "final_failed": sum(1 for item in poll_results if not item.get("ok")),
        "duration_sec": round(time.time() - started, 2),
        "submit_phase_sec": round(submit_done_ts - started, 2),
        "submit_p50_ms": statistics.median(submit_elapsed) if submit_elapsed else None,
        "submit_p95_ms": percentile(submit_elapsed, 0.95),
        "submit_max_ms": max(submit_elapsed) if submit_elapsed else None,
        "asset_upload_p50_ms": statistics.median(asset_upload_elapsed) if asset_upload_elapsed else None,
        "asset_upload_p95_ms": percentile(asset_upload_elapsed, 0.95),
        "asset_upload_max_ms": max(asset_upload_elapsed) if asset_upload_elapsed else None,
        "newapi_submit_p50_ms": statistics.median(newapi_submit_elapsed) if newapi_submit_elapsed else None,
        "newapi_submit_p95_ms": percentile(newapi_submit_elapsed, 0.95),
        "newapi_submit_max_ms": max(newapi_submit_elapsed) if newapi_submit_elapsed else None,
        "final_p50_ms": statistics.median(final_elapsed) if final_elapsed else None,
        "final_p95_ms": percentile(final_elapsed, 0.95),
        "final_max_ms": max(final_elapsed) if final_elapsed else None,
        "poll_attempts_total": sum(poll_attempts),
        "poll_attempts_p50": statistics.median(poll_attempts) if poll_attempts else None,
        "poll_attempts_p95": percentile([float(item) for item in poll_attempts], 0.95),
        "pre_health": pre_health,
        "post_health": post_health,
        "submit_results": submit_results,
        "poll_results": poll_results,
    }
    write_json(REPORT / f"round-{round_no}.json", summary)
    return summary


def aggregate(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    requested = sum(int(r.get("requested") or 0) for r in rounds)
    submit_ok = sum(int(r.get("submit_ok") or 0) for r in rounds)
    final_success = sum(int(r.get("final_success") or 0) for r in rounds)
    final_failed = sum(int(r.get("final_failed") or 0) for r in rounds)
    agg = {
        "suite_id": SUITE_ID,
        "newapi_base": NEWAPI_BASE,
        "stage": STAGE,
        "rounds": len(rounds),
        "use_panda_assets": USE_PANDA_ASSETS,
        "http2_enabled": HTTP2_ENABLED,
        "requested_total": requested,
        "submit_ok_total": submit_ok,
        "submit_failed_total": requested - submit_ok,
        "final_success_total": final_success,
        "final_failed_total": final_failed,
        "submit_p95_ms_max": max([float(r.get("submit_p95_ms") or 0) for r in rounds] or [0]),
        "asset_upload_p95_ms_max": max([float(r.get("asset_upload_p95_ms") or 0) for r in rounds] or [0]),
        "newapi_submit_p95_ms_max": max([float(r.get("newapi_submit_p95_ms") or 0) for r in rounds] or [0]),
        "final_p95_ms_max": max([float(r.get("final_p95_ms") or 0) for r in rounds] or [0]),
        "poll_attempts_total": sum(int(r.get("poll_attempts_total") or 0) for r in rounds),
        "duration_sec_max": max([float(r.get("duration_sec") or 0) for r in rounds] or [0]),
        "pass_single_round": requested >= STAGE and submit_ok >= STAGE and final_success >= STAGE - 1,
        "pass_70_72_three_rounds": requested >= 72 and submit_ok >= 72 and final_success >= 70,
        "rounds_summary": [
            {
                "round": r.get("round"),
                "submit_ok": r.get("submit_ok"),
                "submit_failed": r.get("submit_failed"),
                "final_success": r.get("final_success"),
                "final_failed": r.get("final_failed"),
                "duration_sec": r.get("duration_sec"),
                "submit_p95_ms": r.get("submit_p95_ms"),
                "asset_upload_p95_ms": r.get("asset_upload_p95_ms"),
                "newapi_submit_p95_ms": r.get("newapi_submit_p95_ms"),
                "final_p95_ms": r.get("final_p95_ms"),
                "poll_attempts_total": r.get("poll_attempts_total"),
            }
            for r in rounds
        ],
    }
    write_json(REPORT / "aggregate-summary.json", agg)
    return agg


def main() -> int:
    print(
        json.dumps(
            {
                "event": "start",
                "suite_id": SUITE_ID,
                "base": NEWAPI_BASE,
                "stage": STAGE,
                "rounds": ROUNDS,
                "use_panda_assets": USE_PANDA_ASSETS,
                "http2": HTTP2_ENABLED,
            },
            ensure_ascii=False,
        )
    )
    rounds = []
    for round_no in range(1, ROUNDS + 1):
        print(json.dumps({"event": "round_start", "round": round_no}, ensure_ascii=False))
        try:
            summary = run_round(round_no)
        except Exception:
            summary = {"round": round_no, "error": traceback.format_exc()}
            write_json(REPORT / f"round-{round_no}-error.json", summary)
        rounds.append(summary)
        print(
            json.dumps(
                {
                    "event": "round_done",
                    "round": round_no,
                    "submit_ok": summary.get("submit_ok"),
                    "final_success": summary.get("final_success"),
                    "duration_sec": summary.get("duration_sec"),
                },
                ensure_ascii=False,
            )
        )
        if round_no < ROUNDS:
            time.sleep(60)
    agg = aggregate(rounds)
    print(json.dumps({"event": "done", "report": str(REPORT), **agg}, ensure_ascii=False))
    return 0 if (agg.get("pass_single_round") or agg.get("pass_70_72_three_rounds")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
