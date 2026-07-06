#!/usr/bin/env python3
"""IMG-012 NewAPI sync-over-async 24 混合压测。

通过 NewAPI 标准 /v1/images/* 同步入口压测（非 /api/image-tasks）。
环境变量：
  NEWAPI_BASE_URL  默认 https://sub2api.closeapi.top
  NEWAPI_API_KEY   必填
  IMG012_ROUNDS    默认 1（验收可先单轮，通过后改 3）
  IMG012_STAGE     默认 24
"""
from __future__ import annotations

import base64
import concurrent.futures
import datetime as dt
import importlib.util
import json
import os
import random
import sys
import time
import traceback
import threading
import uuid
from pathlib import Path
from typing import Any

import httpx

STAGE = int(os.getenv("IMG012_STAGE", "24"))
ROUNDS = int(os.getenv("IMG012_ROUNDS", "1"))
NEWAPI_BASE = os.getenv("NEWAPI_BASE_URL", "https://sub2api.closeapi.top").rstrip("/")
NEWAPI_KEY = os.getenv("NEWAPI_API_KEY", "").strip()
TARGET = os.getenv("IMG012_TARGET", "newapi").strip().lower()  # newapi | panda
PANDA_PUBLIC = os.getenv("PANDA_PUBLIC_BASE", "https://gptimage.relai.asia").rstrip("/")
PANDA_KEY = os.getenv("PANDA_AUTH_KEY", "").strip()
READ_TIMEOUT = float(os.getenv("IMG012_READ_TIMEOUT", "600"))
CONNECT_TIMEOUT = float(os.getenv("IMG012_CONNECT_TIMEOUT", "30"))
SUBMIT_WINDOW = max(1, int(os.getenv("IMG012_SUBMIT_WINDOW", "24")))
USE_PANDA_ASSETS = os.getenv("IMG012_USE_PANDA_ASSETS", "1").strip().lower() in {"1", "true", "yes", "on"}
ASSET_UPLOAD_WINDOW = max(1, int(os.getenv("IMG012_ASSET_UPLOAD_WINDOW", "6")))
HTTP2_ENABLED = os.getenv("IMG012_HTTP2", "1").strip().lower() in {"1", "true", "yes", "on"}
_ASSET_UPLOAD_SEM = threading.BoundedSemaphore(ASSET_UPLOAD_WINDOW)

if STAGE not in {6, 12, 18, 24, 30}:
    raise SystemExit("IMG012_STAGE must be 6, 12, 18, 24, or 30")


def resolve_api_target() -> tuple[str, str, str]:
    if TARGET == "panda":
        key = PANDA_KEY
        if not key:
            import subprocess
            proc = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=15", "panda",
                 "python3 -c \"import json;print(json.load(open('/root/gptimage/config.json')).get('auth-key',''))\""],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if proc.returncode != 0:
                raise SystemExit(f"failed to fetch Panda auth key: {proc.stderr}")
            key = (proc.stdout or "").strip()
        if not key:
            raise SystemExit("PANDA_AUTH_KEY is required for IMG012_TARGET=panda")
        return PANDA_PUBLIC, key, "panda"
    if not NEWAPI_KEY:
        raise SystemExit("NEWAPI_API_KEY is required for IMG012_TARGET=newapi")
    return NEWAPI_BASE, NEWAPI_KEY, "newapi"


API_BASE, API_KEY, API_TARGET = resolve_api_target()


def panda_auth_key() -> str:
    key = PANDA_KEY
    if key:
        return key
    import subprocess
    proc = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=15", "panda",
         "python3 -c \"import json;print(json.load(open('/root/gptimage/config.json')).get('auth-key',''))\""],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to fetch Panda auth key: {proc.stderr}")
    key = (proc.stdout or "").strip()
    if not key:
        raise RuntimeError("PANDA_AUTH_KEY is required for Panda asset upload")
    return key


PANDA_ASSET_KEY = ""
if USE_PANDA_ASSETS:
    PANDA_ASSET_KEY = panda_auth_key()

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "scripts" / "r56_stage18_local_loadgen.py"
spec = importlib.util.spec_from_file_location("r56", LEGACY)
legacy = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(legacy)

STAMP = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
SUITE_ID = f"img012-newapi-sync-stage{STAGE}-{ROUNDS}rounds-{STAMP}"
REPORT = ROOT / "reports" / SUITE_ID
REPORT.mkdir(parents=True, exist_ok=True)

_CLIENT = httpx.Client(
    http2=HTTP2_ENABLED,
    limits=httpx.Limits(max_connections=SUBMIT_WINDOW, max_keepalive_connections=SUBMIT_WINDOW),
    timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=60.0),
)


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def data_url_to_file(data_url: str, filename: str) -> tuple[str, bytes, str]:
    header, _, payload = data_url.partition(",")
    mime = header.split(";", 1)[0].removeprefix("data:") or "image/png"
    return filename, base64.b64decode(payload), mime


def build_sync_tasks(round_no: int, run_id: str) -> list[dict[str, Any]]:
    legacy_stage = STAGE if STAGE in {18, 24, 30} else 18
    legacy.STAGE = legacy_stage
    legacy.RUN_ID = run_id
    legacy.LOCAL_REPORT = REPORT
    legacy.random.seed(2026070612 + round_no)
    async_tasks = legacy.build_tasks()
    if STAGE < legacy_stage:
        gen_target = {6: 2, 12: 4}.get(STAGE, max(1, STAGE // 3))
        edit_target = STAGE - gen_target
        generations = [task for task in async_tasks if task.get("kind") == "generation"][:gen_target]
        edits = [task for task in async_tasks if task.get("kind") == "edit"][:edit_target]
        async_tasks = generations + edits
    sync_tasks: list[dict[str, Any]] = []
    for task in async_tasks:
        kind = task["kind"]
        body = dict(task["body"])
        body.pop("client_task_id", None)
        body["response_format"] = "url"
        if kind == "generation":
            sync_tasks.append({
                "kind": "generation",
                "endpoint": "/v1/images/generations",
                "json_body": body,
                "reference_count": 0,
                "reference_total_bytes": 0,
                "prompt_family": task.get("prompt_family"),
            })
        else:
            images = body.pop("images", [])
            ref_names = task.get("reference_names") or [f"reference_{i + 1}.png" for i in range(len(images))]
            files = [data_url_to_file(images[i], ref_names[i] if i < len(ref_names) else f"reference_{i + 1}.png") for i in range(len(images))]
            sync_tasks.append({
                "kind": "edit",
                "endpoint": "/v1/images/edits",
                "form_fields": {
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
            })
    return sync_tasks


def response_failure(resp: httpx.Response, started: float, task: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "status": resp.status_code,
        "elapsed_ms": round((time.time() - started) * 1000, 2),
        "kind": task.get("kind"),
        "endpoint": task.get("endpoint"),
        "error": repr(exc),
        "content_type": resp.headers.get("content-type", ""),
        "body_preview": (resp.text or "")[:2000],
        "busy_6": "global concurrency limit 6" in (resp.text or "").lower(),
    }


def upload_reference_assets(files: list[tuple[str, bytes, str]]) -> list[str]:
    with _ASSET_UPLOAD_SEM:
        multipart = [("image", (filename, data, mime)) for filename, data, mime in files]
        resp = _CLIENT.post(
            PANDA_PUBLIC + "/api/image-assets/references",
            headers={"Authorization": f"Bearer {PANDA_ASSET_KEY}"},
            files=multipart,
        )
        resp.raise_for_status()
        body = resp.json()
        items = body.get("items") if isinstance(body, dict) else None
        asset_ids = [str(item.get("asset_id") or "").strip() for item in (items or []) if isinstance(item, dict)]
        asset_ids = [item for item in asset_ids if item]
        if not asset_ids:
            raise RuntimeError(f"Panda asset upload returned no asset_id: status={resp.status_code}, body={(resp.text or '')[:500]}")
        return asset_ids


def submit_generation(task: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    try:
        resp = _CLIENT.post(
            API_BASE + task["endpoint"],
            headers={"Authorization": f"Bearer {API_KEY}"},
            json=task["json_body"],
        )
        try:
            body = resp.json() if resp.content else {}
        except Exception as exc:
            return response_failure(resp, started, task, exc)
        data = body.get("data") if isinstance(body, dict) else None
        ok = 200 <= resp.status_code < 300 and isinstance(data, list) and len(data) > 0
        err_text = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                err_text = str(err.get("message") or err)
            elif err:
                err_text = str(err)
        return {
            "ok": ok,
            "status": resp.status_code,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "kind": task["kind"],
            "endpoint": task["endpoint"],
            "error": err_text,
            "busy_6": "global concurrency limit 6" in err_text.lower(),
            "data_count": len(data) if isinstance(data, list) else 0,
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "kind": task["kind"],
            "endpoint": task["endpoint"],
            "error": repr(exc),
        }


def submit_edit(task: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    try:
        files = []
        data = dict(task["form_fields"])
        if USE_PANDA_ASSETS and task.get("files"):
            asset_ids = upload_reference_assets(task["files"])
            data["asset_ids"] = ",".join(asset_ids)
            for index, asset_id in enumerate(asset_ids, start=1):
                files.append((
                    "image",
                    (
                        f"panda-asset-{index}.txt",
                        f"panda-asset://{asset_id}".encode("utf-8"),
                        "text/plain",
                    ),
                ))
        else:
            for filename, file_data, mime in task["files"]:
                files.append(("image", (filename, file_data, mime)))
        resp = _CLIENT.post(
            API_BASE + task["endpoint"],
            headers={"Authorization": f"Bearer {API_KEY}"},
            data=data,
            files=files or None,
        )
        try:
            body = resp.json() if resp.content else {}
        except Exception as exc:
            return response_failure(resp, started, task, exc)
        payload_data = body.get("data") if isinstance(body, dict) else None
        ok = 200 <= resp.status_code < 300 and isinstance(payload_data, list) and len(payload_data) > 0
        err_text = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                err_text = str(err.get("message") or err)
            elif err:
                err_text = str(err)
        return {
            "ok": ok,
            "status": resp.status_code,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "kind": task["kind"],
            "endpoint": task["endpoint"],
            "error": err_text,
            "busy_6": "global concurrency limit 6" in err_text.lower(),
            "data_count": len(payload_data) if isinstance(payload_data, list) else 0,
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "kind": task["kind"],
            "endpoint": task["endpoint"],
            "error": repr(exc),
        }


def submit_one(task: dict[str, Any]) -> dict[str, Any]:
    if task["kind"] == "generation":
        return submit_generation(task)
    return submit_edit(task)


def panda_health() -> dict[str, Any]:
    try:
        resp = httpx.get(f"{PANDA_PUBLIC}/health?format=json", timeout=20.0)
        return resp.json() if resp.status_code == 200 else {"error": resp.status_code}
    except Exception as exc:
        return {"error": repr(exc)}


def run_round(round_no: int) -> dict[str, Any]:
    run_id = f"{SUITE_ID}-r{round_no}"
    tasks = build_sync_tasks(round_no, run_id)
    pre_health = panda_health()
    started = time.time()
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=SUBMIT_WINDOW) as pool:
        futures = [pool.submit(submit_one, task) for task in tasks]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
    post_health = panda_health()
    ok_count = sum(1 for item in results if item.get("ok"))
    busy_count = sum(1 for item in results if item.get("busy_6"))
    elapsed = [float(item.get("elapsed_ms") or 0) for item in results if item.get("elapsed_ms")]
    elapsed.sort()
    summary = {
        "round": round_no,
        "run_id": run_id,
        "requested": len(tasks),
        "success": ok_count,
        "failed": len(results) - ok_count,
        "busy_6_count": busy_count,
        "submit_p50_ms": elapsed[len(elapsed) // 2] if elapsed else None,
        "submit_p95_ms": elapsed[int(len(elapsed) * 0.95)] if elapsed else None,
        "submit_max_ms": max(elapsed) if elapsed else None,
        "duration_sec": round(time.time() - started, 2),
        "pre_health": pre_health,
        "post_health": post_health,
        "results": results,
    }
    write_json(REPORT / f"round-{round_no}.json", summary)
    return summary


def aggregate(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    total_req = sum(int(r.get("requested") or 0) for r in rounds)
    total_ok = sum(int(r.get("success") or 0) for r in rounds)
    total_busy = sum(int(r.get("busy_6_count") or 0) for r in rounds)
    p95s = [float(r.get("submit_p95_ms") or 0) for r in rounds if r.get("submit_p95_ms")]
    agg = {
        "suite_id": SUITE_ID,
        "newapi_base": API_BASE,
        "api_target": API_TARGET,
        "use_panda_assets": USE_PANDA_ASSETS,
        "asset_upload_window": ASSET_UPLOAD_WINDOW,
        "http2_enabled": HTTP2_ENABLED,
        "stage": STAGE,
        "rounds": len(rounds),
        "requested_total": total_req,
        "success_total": total_ok,
        "failed_total": total_req - total_ok,
        "busy_6_total": total_busy,
        "submit_p95_ms_max": max(p95s) if p95s else None,
        "pass_24_single_round": STAGE == 24 and total_req >= 24 and total_ok >= 23 and total_busy == 0,
        "pass_single_round": total_req >= STAGE and total_ok >= STAGE - 1 and total_busy == 0,
        "pass_70_72_three_rounds": total_req >= 72 and total_ok >= 70 and total_busy == 0,
        "rounds_summary": [{
            "round": r.get("round"),
            "success": r.get("success"),
            "failed": r.get("failed"),
            "busy_6_count": r.get("busy_6_count"),
            "submit_p95_ms": r.get("submit_p95_ms"),
        } for r in rounds],
    }
    write_json(REPORT / "aggregate-summary.json", agg)
    return agg


def main() -> int:
    print(json.dumps({"event": "start", "suite_id": SUITE_ID, "api_target": API_TARGET, "api_base": API_BASE, "stage": STAGE, "rounds": ROUNDS, "use_panda_assets": USE_PANDA_ASSETS, "http2_enabled": HTTP2_ENABLED, "submit_window": SUBMIT_WINDOW}, ensure_ascii=False))
    round_summaries = []
    for round_no in range(1, ROUNDS + 1):
        print(json.dumps({"event": "round_start", "round": round_no}, ensure_ascii=False))
        try:
            summary = run_round(round_no)
        except Exception:
            summary = {"round": round_no, "error": traceback.format_exc()}
            write_json(REPORT / f"round-{round_no}-error.json", summary)
        round_summaries.append(summary)
        print(json.dumps({
            "event": "round_done",
            "round": round_no,
            "success": summary.get("success"),
            "failed": summary.get("failed"),
            "busy_6_count": summary.get("busy_6_count"),
        }, ensure_ascii=False))
        if round_no < ROUNDS:
            time.sleep(60)
    agg = aggregate(round_summaries)
    print(json.dumps({"event": "done", "report": str(REPORT), **{k: agg[k] for k in ("success_total", "failed_total", "busy_6_total", "pass_24_single_round")}}, ensure_ascii=False))
    return 0 if agg.get("pass_single_round") or (ROUNDS >= 3 and agg.get("pass_70_72_three_rounds")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
