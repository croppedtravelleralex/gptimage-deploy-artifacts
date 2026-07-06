#!/usr/bin/env python3
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
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import httpx


STAGE = int(sys.argv[1]) if len(sys.argv) > 1 else 24
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
if STAGE not in {18, 24, 30}:
    raise SystemExit("stage must be one of: 18, 24, 30")
if ROUNDS < 1:
    raise SystemExit("rounds must be >= 1")

ROOT = Path(__file__).resolve().parents[1]
LEGACY_PATH = ROOT / "scripts" / "r56_stage18_local_loadgen.py"
spec = importlib.util.spec_from_file_location("r56_stage_loadgen", LEGACY_PATH)
legacy = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(legacy)

PUBLIC_BASE = legacy.PUBLIC_BASE
SSH_ATTEMPTS = max(1, int(os.getenv("IMG005_SSH_ATTEMPTS", "4")))
SSH_RETRY_DELAY_SEC = max(0.0, float(os.getenv("IMG005_SSH_RETRY_DELAY_SEC", "5.0")))
POLL_INTERVAL = 15.0
MAX_WAIT_SECONDS = 45 * 60
ROUND_COOLDOWN_SECONDS = 60
SUBMIT_WINDOW = max(1, int(os.getenv("IMG005_SUBMIT_WINDOW", "8")))
SUBMIT_MAX_ATTEMPTS = max(1, int(os.getenv("IMG005_SUBMIT_MAX_ATTEMPTS", "3")))
SUBMIT_CONNECT_TIMEOUT = max(1.0, float(os.getenv("IMG005_SUBMIT_CONNECT_TIMEOUT", "15")))
SUBMIT_READ_TIMEOUT = max(5.0, float(os.getenv("IMG005_SUBMIT_READ_TIMEOUT", "120")))
SUBMIT_JITTER_MS = max(0, int(os.getenv("IMG005_SUBMIT_JITTER_MS", "150")))
SUBMIT_RETRY_BASE_DELAY_SEC = max(0.0, float(os.getenv("IMG005_SUBMIT_RETRY_BASE_DELAY_SEC", "1.0")))
ASSET_UPLOAD_WINDOW = max(1, int(os.getenv("IMG005_ASSET_UPLOAD_WINDOW", "8")))
ASSET_UPLOAD_MAX_ATTEMPTS = max(1, int(os.getenv("IMG005_ASSET_UPLOAD_MAX_ATTEMPTS", "2")))
ASSET_UPLOAD_RETRY_BASE_DELAY_SEC = max(0.0, float(os.getenv("IMG005_ASSET_UPLOAD_RETRY_BASE_DELAY_SEC", "1.5")))
ASSET_UPLOAD_JITTER_MS = max(0, int(os.getenv("IMG005_ASSET_UPLOAD_JITTER_MS", "200")))
STAMP = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
SUITE_ID = f"img005-stage{STAGE}-{ROUNDS}rounds-{STAMP}"
SUITE_REPORT = Path("reports") / SUITE_ID
SUITE_REMOTE = f"/root/gptimage/backups/{SUITE_ID}"
SUITE_REPORT.mkdir(parents=True, exist_ok=True)

_SUBMIT_CLIENT = httpx.Client(
    http2=True,
    limits=httpx.Limits(
        max_connections=max(SUBMIT_WINDOW, 1),
        max_keepalive_connections=max(SUBMIT_WINDOW, 1),
        keepalive_expiry=120.0,
    ),
    timeout=httpx.Timeout(
        connect=SUBMIT_CONNECT_TIMEOUT,
        read=SUBMIT_READ_TIMEOUT,
        write=SUBMIT_READ_TIMEOUT,
        pool=30.0,
    ),
)

_ORIGINAL_REMOTE = legacy.remote
_ORIGINAL_SCP_TO_REMOTE = legacy.scp_to_remote


def remote_retry(cmd: str, timeout: float = 60) -> str:
    last_error: Exception | None = None
    for attempt in range(1, SSH_ATTEMPTS + 1):
        try:
            return _ORIGINAL_REMOTE(cmd, timeout=timeout)
        except Exception as exc:
            last_error = exc
            if attempt >= SSH_ATTEMPTS:
                break
            print(
                json.dumps(
                    {
                        "event": "ssh_retry",
                        "attempt": attempt,
                        "max_attempts": SSH_ATTEMPTS,
                        "timeout": timeout,
                        "error": repr(exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            time.sleep(SSH_RETRY_DELAY_SEC * attempt)
    assert last_error is not None
    raise last_error


def scp_to_remote_retry(local: str, remote_path: str, timeout: float = 120) -> None:
    last_error: Exception | None = None
    for attempt in range(1, SSH_ATTEMPTS + 1):
        try:
            _ORIGINAL_SCP_TO_REMOTE(local, remote_path, timeout=timeout)
            return
        except Exception as exc:
            last_error = exc
            if attempt >= SSH_ATTEMPTS:
                break
            print(
                json.dumps(
                    {
                        "event": "scp_retry",
                        "attempt": attempt,
                        "max_attempts": SSH_ATTEMPTS,
                        "timeout": timeout,
                        "local": local,
                        "remote_path": remote_path,
                        "error": repr(exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            time.sleep(SSH_RETRY_DELAY_SEC * attempt)
    assert last_error is not None
    raise last_error


legacy.remote = remote_retry
legacy.scp_to_remote = scp_to_remote_retry


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def data_url_to_file(data_url: str, filename: str) -> tuple[str, bytes, str]:
    header, _, payload = data_url.partition(",")
    mime = header.split(";", 1)[0].removeprefix("data:") or "image/png"
    return filename, base64.b64decode(payload), mime


def http_multipart(path: str, *, auth_key: str, files: list[tuple[str, bytes, str]], timeout: float = 240) -> dict[str, Any]:
    boundary = "----CodexIMG005" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for filename, data, content_type in files:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="image"; filename="{filename.replace(chr(34), "_")}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(chunks)
    headers = {
        "Authorization": f"Bearer {auth_key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(payload)),
    }
    started = time.time()
    req = urllib.request.Request(PUBLIC_BASE + path, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = raw[:1000].decode("utf-8", "replace")
            return {
                "ok": True,
                "status": resp.status,
                "elapsed_ms": round((time.time() - started) * 1000, 2),
                "bytes": len(raw),
                "request_bytes": len(payload),
                "body": body,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = raw[:1000].decode("utf-8", "replace")
        return {
            "ok": False,
            "status": exc.code,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "bytes": len(raw),
            "request_bytes": len(payload),
            "body": body,
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "request_bytes": len(payload),
            "error": repr(exc),
        }


def http_json_keepalive(
        path: str,
        *,
        auth_key: str,
        method: str = "GET",
        body: Any = None,
        timeout: httpx.Timeout | None = None,
) -> dict[str, Any]:
    """使用 HTTP/2 keepalive 发送小 JSON 请求，避免 submit 阶段 24 个新 TLS 握手同时打出。"""
    headers = {"Authorization": f"Bearer {auth_key}"}
    started = time.time()
    try:
        resp = _SUBMIT_CLIENT.request(
            method,
            PUBLIC_BASE + path,
            headers=headers,
            json=body,
            timeout=timeout,
        )
        raw = resp.content
        try:
            parsed = resp.json()
        except Exception:
            parsed = raw[:1000].decode("utf-8", "replace")
        return {
            "ok": 200 <= resp.status_code < 300,
            "status": resp.status_code,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "bytes": len(raw),
            "body": parsed,
            "http_version": resp.http_version,
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "error": repr(exc),
        }


def _is_retryable_submit_failure(res: dict[str, Any]) -> bool:
    if res.get("ok"):
        return False
    status = res.get("status")
    if status in {429, 400, 401, 403, 404}:
        return False
    if isinstance(status, int) and status >= 500:
        return True
    text = f"{res.get('error') or ''} {res.get('body') or ''}".lower()
    retryable_markers = [
        "handshake",
        "connect timeout",
        "connection reset",
        "connection aborted",
        "remote protocol error",
        "remotedisconnected",
        "remote end closed",
        "readtimeout",
        "timeout",
        "tls",
        "ssl",
        "eof",
    ]
    return any(marker in text for marker in retryable_markers)


def _is_retryable_asset_failure(res: dict[str, Any]) -> bool:
    if res.get("ok"):
        return False
    status = res.get("status")
    if status in {400, 401, 403, 404, 413, 415, 429}:
        return False
    if isinstance(status, int) and status >= 500:
        return True
    text = f"{res.get('error') or ''} {res.get('body') or ''}".lower()
    retryable_markers = [
        "handshake",
        "connect timeout",
        "connection reset",
        "connection aborted",
        "remote protocol error",
        "remotedisconnected",
        "remote end closed",
        "readtimeout",
        "timeout",
        "tls",
        "ssl",
        "eof",
    ]
    return any(marker in text for marker in retryable_markers)


def lookup_existing_task(task_id: str, auth_key: str) -> dict[str, Any] | None:
    ids = urllib.parse.quote(task_id)
    res = http_json_keepalive(
        "/api/image-tasks/status?ids=" + ids,
        auth_key=auth_key,
        timeout=httpx.Timeout(connect=SUBMIT_CONNECT_TIMEOUT, read=30.0, write=30.0, pool=10.0),
    )
    if not res.get("ok"):
        return None
    body = res.get("body")
    if not isinstance(body, dict):
        return None
    items = body.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if str(item.get("id") or "") == task_id:
            return item
    return None


def start_monitor(remote_report: str, run_id: str, local_report: Path) -> None:
    legacy.scp_to_remote("scripts/r56_panda_monitor.py", "/tmp/r56_panda_monitor.py")
    legacy.remote(f"mkdir -p {remote_report}", timeout=30)
    cmd = (
        f"cd /root/gptimage; nohup python3 /tmp/r56_panda_monitor.py "
        f"{remote_report} {run_id} </dev/null > {remote_report}/monitor.stdout.log 2>&1 & echo $!"
    )
    pid = legacy.remote(cmd, timeout=30).strip()
    (local_report / "remote-monitor-pid.txt").write_text(pid, encoding="utf-8")


def stop_monitor(remote_report: str) -> None:
    legacy.remote(f"touch {remote_report}/stop-monitor", timeout=20)
    legacy.wait_remote_file(f"{remote_report}/monitor.done", timeout=180)


def build_round_tasks(round_no: int, run_id: str, local_report: Path) -> list[dict[str, Any]]:
    legacy.RUN_ID = run_id
    legacy.LOCAL_REPORT = local_report
    legacy.random.seed(2026070505 + round_no)
    local_report.mkdir(parents=True, exist_ok=True)
    tasks = legacy.build_tasks()
    for task in tasks:
        task["round"] = round_no
        if task["kind"] == "edit":
            images = task["body"].pop("images", [])
            ref_names = task.get("reference_names") or [f"reference_{i + 1}.png" for i in range(len(images))]
            task["reference_files"] = [
                data_url_to_file(image, ref_names[i] if i < len(ref_names) else f"reference_{i + 1}.png")
                for i, image in enumerate(images)
            ]
        else:
            task["reference_files"] = []
    return tasks


def upload_assets_for_task(task: dict[str, Any], auth_key: str) -> dict[str, Any]:
    task_id = task["body"]["client_task_id"]
    if task["kind"] != "edit":
        return {"task_id": task_id, "kind": task["kind"], "ok": True, "skipped": True, "asset_ids": []}
    started = time.time()
    attempts: list[dict[str, Any]] = []
    res: dict[str, Any] = {}
    body: dict[str, Any] = {}
    items: list[Any] = []
    asset_ids: list[str] = []
    ok = False
    if ASSET_UPLOAD_JITTER_MS > 0:
        time.sleep(random.uniform(0, ASSET_UPLOAD_JITTER_MS) / 1000)
    for attempt in range(1, ASSET_UPLOAD_MAX_ATTEMPTS + 1):
        res = http_multipart("/api/image-assets/references", auth_key=auth_key, files=task["reference_files"])
        body = res.get("body") if isinstance(res.get("body"), dict) else {}
        items = body.get("items") if isinstance(body, dict) else []
        if not isinstance(items, list):
            items = []
        asset_ids = [str(item.get("asset_id")) for item in items if isinstance(item, dict) and item.get("asset_id")]
        ok = bool(res.get("ok")) and len(asset_ids) == len(task["reference_files"])
        retryable = _is_retryable_asset_failure(res) or (bool(res.get("ok")) and not ok)
        attempts.append({
            "attempt": attempt,
            "ok": ok,
            "transport_ok": bool(res.get("ok")),
            "retryable": retryable,
            "status": res.get("status"),
            "elapsed_ms": res.get("elapsed_ms"),
            "error": res.get("error"),
            "asset_count": len(asset_ids),
        })
        if ok or not retryable or attempt >= ASSET_UPLOAD_MAX_ATTEMPTS:
            break
        time.sleep(ASSET_UPLOAD_RETRY_BASE_DELAY_SEC * attempt)
    return {
        "task_id": task_id,
        "kind": task["kind"],
        "ok": ok,
        "status": res.get("status"),
        "elapsed_ms": round((time.time() - started) * 1000, 2),
        "last_attempt_elapsed_ms": res.get("elapsed_ms"),
        "attempt_count": len(attempts),
        "attempts": attempts,
        "request_bytes": res.get("request_bytes"),
        "response_bytes": res.get("bytes"),
        "reference_count": len(task["reference_files"]),
        "reference_total_bytes": task.get("reference_total_bytes", 0),
        "asset_ids": asset_ids,
        "error": res.get("error"),
        "body": {"item_count": len(items)} if ok else body,
    }


def submit_one(task: dict[str, Any], uploads: dict[str, dict[str, Any]], auth_key: str) -> dict[str, Any]:
    task_id = task["body"]["client_task_id"]
    body = dict(task["body"])
    if task["kind"] == "edit":
        upload = uploads.get(task_id) or {}
        if not upload.get("ok"):
            return {"task_id": task_id, "kind": task["kind"], "ok": False, "not_submitted": True, "error": "asset upload failed"}
        body["asset_ids"] = upload.get("asset_ids") or []
    started = time.time()
    attempts: list[dict[str, Any]] = []
    res: dict[str, Any] = {}
    existing: dict[str, Any] | None = None
    if SUBMIT_JITTER_MS > 0:
        time.sleep(random.uniform(0, SUBMIT_JITTER_MS) / 1000)
    for attempt in range(1, SUBMIT_MAX_ATTEMPTS + 1):
        res = http_json_keepalive(
            task["endpoint"],
            auth_key=auth_key,
            method="POST",
            body=body,
            timeout=httpx.Timeout(
                connect=SUBMIT_CONNECT_TIMEOUT,
                read=SUBMIT_READ_TIMEOUT,
                write=SUBMIT_READ_TIMEOUT,
                pool=30.0,
            ),
        )
        attempts.append({
            "attempt": attempt,
            "ok": bool(res.get("ok")),
            "status": res.get("status"),
            "elapsed_ms": res.get("elapsed_ms"),
            "error": res.get("error"),
            "http_version": res.get("http_version"),
        })
        if res.get("ok"):
            break
        existing = lookup_existing_task(task_id, auth_key)
        if existing:
            res = {
                "ok": True,
                "status": 200,
                "elapsed_ms": res.get("elapsed_ms"),
                "body": existing,
                "recovered_by_status_lookup": True,
            }
            break
        if attempt >= SUBMIT_MAX_ATTEMPTS or not _is_retryable_submit_failure(res):
            break
        time.sleep(SUBMIT_RETRY_BASE_DELAY_SEC * attempt)
    resp = res.get("body") if isinstance(res.get("body"), dict) else {}
    return {
        "task_id": task_id,
        "kind": task["kind"],
        "endpoint": task["endpoint"],
        "ok": bool(res.get("ok")),
        "status": res.get("status"),
        "elapsed_ms": round((time.time() - started) * 1000, 2),
        "response_elapsed_ms": res.get("elapsed_ms"),
        "body_status": resp.get("status") if isinstance(resp, dict) else None,
        "body": {key: resp.get(key) for key in ["id", "status", "created_at", "updated_at"]} if res.get("ok") and isinstance(resp, dict) else res.get("body"),
        "error": res.get("error"),
        "attempt_count": len(attempts),
        "attempts": attempts,
        "http_version": res.get("http_version"),
        "recovered_by_status_lookup": bool(res.get("recovered_by_status_lookup")),
        "payload_bytes": len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        "reference_count": task.get("reference_count", 0),
        "reference_total_bytes": task.get("reference_total_bytes", 0),
        "prompt_family": task.get("prompt_family"),
    }


def list_status(task_ids: list[str], auth_key: str) -> dict[str, Any]:
    ids = urllib.parse.quote(",".join(task_ids))
    res = http_json_keepalive(
        "/api/image-tasks/status?ids=" + ids,
        auth_key=auth_key,
        timeout=httpx.Timeout(connect=SUBMIT_CONNECT_TIMEOUT, read=60.0, write=30.0, pool=10.0),
    )
    items = []
    if isinstance(res.get("body"), dict) and isinstance(res["body"].get("items"), list):
        items = res["body"]["items"]
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "missing")
        counts[status] = counts.get(status, 0) + 1
    return {"response": res, "items": items, "counts": counts, "latency_ms": res.get("elapsed_ms")}


def manifest_for(run_id: str, remote_report: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "public_base": PUBLIC_BASE,
        "remote_report": remote_report,
        "input_mode": "IMG-005 two-stage: multipart asset upload first, then async task submit with asset_ids",
        "submit_transport": {
            "client": "httpx",
            "http2": True,
            "submit_window": SUBMIT_WINDOW,
            "max_attempts": SUBMIT_MAX_ATTEMPTS,
            "connect_timeout_secs": SUBMIT_CONNECT_TIMEOUT,
            "read_timeout_secs": SUBMIT_READ_TIMEOUT,
            "jitter_ms": SUBMIT_JITTER_MS,
        },
        "asset_upload_transport": {
            "client": "urllib",
            "upload_window": ASSET_UPLOAD_WINDOW,
            "max_attempts": ASSET_UPLOAD_MAX_ATTEMPTS,
            "retry_base_delay_secs": ASSET_UPLOAD_RETRY_BASE_DELAY_SEC,
            "jitter_ms": ASSET_UPLOAD_JITTER_MS,
        },
        "generation_count": sum(1 for task in tasks if task["kind"] == "generation"),
        "edit_count": sum(1 for task in tasks if task["kind"] == "edit"),
        "single_reference_edit_count": sum(1 for task in tasks if task["kind"] == "edit" and task.get("reference_count") == 1),
        "multi_reference_edit_count": sum(1 for task in tasks if task["kind"] == "edit" and int(task.get("reference_count") or 0) > 1),
        "total_reference_bytes": sum(int(task.get("reference_total_bytes") or 0) for task in tasks),
        "no_input_reduction": True,
        "reference_resolution": "768x768 textured PNG references",
        "tasks": [
            {
                "client_task_id": task["body"]["client_task_id"],
                "kind": task["kind"],
                "endpoint": task["endpoint"],
                "prompt": task["body"].get("prompt"),
                "model": task["body"].get("model"),
                "size": task["body"].get("size"),
                "quality": task["body"].get("quality"),
                "reference_count": task.get("reference_count", 0),
                "reference_total_bytes": task.get("reference_total_bytes", 0),
                "reference_names": task.get("reference_names", []),
                "prompt_family": task.get("prompt_family"),
            }
            for task in tasks
        ],
    }


def run_round(round_no: int, auth_key: str) -> dict[str, Any]:
    run_id = f"{SUITE_ID}-r{round_no:02d}"
    local_report = SUITE_REPORT / f"round-{round_no:02d}"
    remote_report = f"{SUITE_REMOTE}/round-{round_no:02d}"
    tasks = build_round_tasks(round_no, run_id, local_report)
    manifest = manifest_for(run_id, remote_report, tasks)
    write_json(local_report / "task-manifest.json", manifest)
    print(json.dumps({"event": "round_start", "round": round_no, "run_id": run_id, "local_report": str(local_report), "remote_report": remote_report}, ensure_ascii=False), flush=True)

    upload_results: list[dict[str, Any]] = []
    submit_results: list[dict[str, Any]] = []
    status_history: list[dict[str, Any]] = []
    final_status: dict[str, Any] | None = None
    upload_elapsed = 0.0
    submit_elapsed = 0.0
    monitor_started = False
    try:
        start_monitor(remote_report, run_id, local_report)
        monitor_started = True
        legacy.wait_remote_file(f"{remote_report}/monitor.ready", timeout=180)
        legacy.scp_to_remote(str(local_report / "task-manifest.json"), f"{remote_report}/task-manifest.json")

        edit_tasks = [task for task in tasks if task["kind"] == "edit"]
        print(json.dumps({"event": "asset_upload_start", "round": round_no, "edit_tasks": len(edit_tasks), "upload_window": min(len(edit_tasks), STAGE, ASSET_UPLOAD_WINDOW), "max_attempts": ASSET_UPLOAD_MAX_ATTEMPTS, "reference_total_bytes": manifest["total_reference_bytes"]}, ensure_ascii=False), flush=True)
        started = time.time()
        upload_workers = min(len(edit_tasks), STAGE, ASSET_UPLOAD_WINDOW)
        with concurrent.futures.ThreadPoolExecutor(max_workers=upload_workers) as executor:
            futures = [executor.submit(upload_assets_for_task, task, auth_key) for task in edit_tasks]
            for future in concurrent.futures.as_completed(futures):
                item = future.result()
                upload_results.append(item)
                print(json.dumps({"event": "asset_uploaded", "round": round_no, "task_id": item.get("task_id"), "ok": item.get("ok"), "status": item.get("status"), "elapsed_ms": item.get("elapsed_ms"), "attempt_count": item.get("attempt_count"), "asset_count": len(item.get("asset_ids") or [])}, ensure_ascii=False), flush=True)
        upload_elapsed = time.time() - started
        upload_results.sort(key=lambda row: row.get("task_id") or "")
        write_json(local_report / "asset-upload-results.json", {"elapsed_seconds": upload_elapsed, "results": upload_results})
        uploads = {row["task_id"]: row for row in upload_results}

        submit_workers = min(len(tasks), STAGE, SUBMIT_WINDOW)
        print(json.dumps({"event": "submit_start", "round": round_no, "count": len(tasks), "submit_window": submit_workers, "max_attempts": SUBMIT_MAX_ATTEMPTS}, ensure_ascii=False), flush=True)
        started = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=submit_workers) as executor:
            futures = [executor.submit(submit_one, task, uploads, auth_key) for task in tasks]
            for future in concurrent.futures.as_completed(futures):
                item = future.result()
                submit_results.append(item)
                print(json.dumps({"event": "submitted", "round": round_no, "task_id": item.get("task_id"), "kind": item.get("kind"), "ok": item.get("ok"), "status": item.get("status"), "elapsed_ms": item.get("elapsed_ms"), "body_status": item.get("body_status"), "attempt_count": item.get("attempt_count"), "http_version": item.get("http_version"), "recovered_by_status_lookup": item.get("recovered_by_status_lookup")}, ensure_ascii=False), flush=True)
        submit_elapsed = time.time() - started
        submit_results.sort(key=lambda row: row.get("task_id") or "")
        accepted_task_ids = [row["task_id"] for row in submit_results if row.get("ok") and row.get("status") in (200, 201, 202)]
        write_json(local_report / "submit-results.json", {"elapsed_seconds": submit_elapsed, "results": submit_results})
        write_json(local_report / "task-ids.json", {"task_ids": accepted_task_ids, "all_task_ids": [row["task_id"] for row in submit_results]})
        legacy.scp_to_remote(str(local_report / "task-ids.json"), f"{remote_report}/task-ids.json")
        print(json.dumps({"event": "submit_done", "round": round_no, "elapsed_seconds": round(submit_elapsed, 3), "accepted": len(accepted_task_ids), "total": len(submit_results)}, ensure_ascii=False), flush=True)

        started = time.time()
        while accepted_task_ids:
            time.sleep(POLL_INTERVAL)
            status = list_status(accepted_task_ids, auth_key)
            status["elapsed_seconds"] = time.time() - started
            status_history.append(status)
            print(json.dumps({"event": "status", "round": round_no, "elapsed_seconds": round(status["elapsed_seconds"], 1), "counts": status.get("counts"), "status_latency_ms": status.get("latency_ms")}, ensure_ascii=False), flush=True)
            items = status.get("items") or []
            if len(items) >= len(accepted_task_ids) and all(str(item.get("status")) in {"success", "error"} for item in items):
                final_status = status
                break
            if time.time() - started > MAX_WAIT_SECONDS:
                final_status = status
                final_status["timeout_reached"] = True
                break
        if not accepted_task_ids:
            final_status = {"items": [], "counts": {}, "no_accepted_tasks": True}
        write_json(local_report / "status-history.json", {"history": status_history, "final_status": final_status})
    finally:
        if monitor_started:
            try:
                stop_monitor(remote_report)
            except Exception as exc:
                (local_report / "monitor-stop-error.txt").write_text(repr(exc), encoding="utf-8")
        legacy.run_cmd(["scp", "-r", f"panda:{remote_report}", str(local_report / "remote")], timeout=300)

    return summarize_round(round_no, run_id, local_report, remote_report, manifest, upload_elapsed, upload_results, submit_elapsed, submit_results, status_history, final_status)


def summarize_round(
    round_no: int,
    run_id: str,
    local_report: Path,
    remote_report: str,
    manifest: dict[str, Any],
    upload_elapsed: float,
    upload_results: list[dict[str, Any]],
    submit_elapsed: float,
    submit_results: list[dict[str, Any]],
    status_history: list[dict[str, Any]],
    final_status: dict[str, Any] | None,
) -> dict[str, Any]:
    upload_lat = [float(row.get("elapsed_ms") or 0) for row in upload_results if row.get("elapsed_ms") is not None]
    submit_lat = [float(row.get("elapsed_ms") or 0) for row in submit_results if row.get("elapsed_ms") is not None]
    status_lat = [float(row.get("latency_ms") or 0) for row in status_history if row.get("latency_ms") is not None]
    items = final_status.get("items") if isinstance(final_status, dict) else []
    counts: dict[str, int] = {}
    if isinstance(items, list):
        for item in items:
            status = str(item.get("status") or "missing")
            counts[status] = counts.get(status, 0) + 1
    ok_uploads = [row for row in upload_results if row.get("ok")]
    failed_uploads = [row for row in upload_results if not row.get("ok")]
    ok_submits = [row for row in submit_results if row.get("ok") and row.get("status") in (200, 201, 202)]
    failed_submits = [row for row in submit_results if not (row.get("ok") and row.get("status") in (200, 201, 202))]
    summary_obj: dict[str, Any] = {
        "run_id": run_id,
        "round": round_no,
        "public_base": PUBLIC_BASE,
        "local_report": str(local_report),
        "remote_report": remote_report,
        "input_mix": {
            "generation_count": manifest["generation_count"],
            "edit_count": manifest["edit_count"],
            "single_reference_edit_count": manifest["single_reference_edit_count"],
            "multi_reference_edit_count": manifest["multi_reference_edit_count"],
            "total_reference_bytes": manifest["total_reference_bytes"],
            "no_input_reduction": True,
            "reference_resolution": manifest["reference_resolution"],
            "transport": "multipart asset upload + asset_ids submit",
        },
        "asset_upload": {
            "ok": len(ok_uploads),
            "failed": len(failed_uploads),
            "total": len(upload_results),
            "elapsed_seconds": upload_elapsed,
            "latency_ms": legacy.summary(upload_lat),
            "request_bytes": legacy.summary([float(row.get("request_bytes") or 0) for row in upload_results]),
            "failed_errors": [{"task_id": row.get("task_id"), "status": row.get("status"), "error": row.get("error"), "body": row.get("body")} for row in failed_uploads],
        },
        "submit": {
            "ok": len(ok_submits),
            "failed": len(failed_submits),
            "total": len(submit_results),
            "elapsed_seconds": submit_elapsed,
            "latency_ms": legacy.summary(submit_lat),
            "payload_bytes": legacy.summary([float(row.get("payload_bytes") or 0) for row in submit_results]),
            "attempts": {
                "max": max([int(row.get("attempt_count") or 0) for row in submit_results] or [0]),
                "retried": sum(1 for row in submit_results if int(row.get("attempt_count") or 0) > 1),
                "recovered_by_status_lookup": sum(1 for row in submit_results if row.get("recovered_by_status_lookup")),
            },
            "http_versions": {str(version): sum(1 for row in submit_results if str(row.get("http_version")) == str(version)) for version in sorted({row.get("http_version") for row in submit_results}, key=str)},
            "http_status_counts": {str(code): sum(1 for row in submit_results if str(row.get("status")) == str(code)) for code in sorted({row.get("status") for row in submit_results}, key=str)},
            "failed_errors": [{"task_id": row.get("task_id"), "kind": row.get("kind"), "status": row.get("status"), "error": row.get("error"), "body": row.get("body")} for row in failed_submits],
        },
        "status_query_latency_ms": legacy.summary(status_lat),
        "final_counts": counts,
    }
    remote_summary_path = local_report / "remote" / Path(remote_report).name / "monitor-summary.json"
    if not remote_summary_path.exists():
        remote_summary_path = local_report / "remote" / "monitor-summary.json"
    if remote_summary_path.exists():
        summary_obj["panda_monitor"] = json.loads(remote_summary_path.read_text(encoding="utf-8"))
    write_json(local_report / "summary.json", summary_obj)
    legacy.scp_to_remote(str(local_report / "summary.json"), f"{remote_report}/loadgen-summary.json")
    print(json.dumps({"event": "round_summary", "round": round_no, "summary": summary_obj}, ensure_ascii=False), flush=True)
    return summary_obj


def aggregate(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    result = {
        "suite_id": SUITE_ID,
        "stage": STAGE,
        "rounds": len(summaries),
        "public_base": PUBLIC_BASE,
        "local_report": str(SUITE_REPORT),
        "remote_report": SUITE_REMOTE,
        "requested_total": STAGE * len(summaries),
        "asset_upload_ok_total": sum(int((s.get("asset_upload") or {}).get("ok") or 0) for s in summaries),
        "asset_upload_failed_total": sum(int((s.get("asset_upload") or {}).get("failed") or 0) for s in summaries),
        "submit_ok_total": sum(int((s.get("submit") or {}).get("ok") or 0) for s in summaries),
        "submit_failed_total": sum(int((s.get("submit") or {}).get("failed") or 0) for s in summaries),
        "final_success_total": sum(int((s.get("final_counts") or {}).get("success") or 0) for s in summaries),
        "final_error_total": sum(int((s.get("final_counts") or {}).get("error") or 0) for s in summaries),
        "final_timeout_pending_total": sum(int((s.get("final_counts") or {}).get("timeout_pending") or 0) for s in summaries),
        "final_running_total": sum(int((s.get("final_counts") or {}).get("running") or 0) for s in summaries),
        "final_queued_total": sum(int((s.get("final_counts") or {}).get("queued") or 0) for s in summaries),
        "per_round": summaries,
    }
    result["final_unfinished_total"] = (
        int(result["final_timeout_pending_total"])
        + int(result["final_running_total"])
        + int(result["final_queued_total"])
    )
    result["p95_across_rounds"] = {
        "asset_upload_p95_ms_max": max([float(((s.get("asset_upload") or {}).get("latency_ms") or {}).get("p95") or 0) for s in summaries] or [0]),
        "submit_p95_ms_max": max([float(((s.get("submit") or {}).get("latency_ms") or {}).get("p95") or 0) for s in summaries] or [0]),
        "status_query_p95_ms_max": max([float((s.get("status_query_latency_ms") or {}).get("p95") or 0) for s in summaries] or [0]),
        "cpu_p95_pct_max": max([float((((s.get("panda_monitor") or {}).get("resources") or {}).get("cpu_pct") or {}).get("p95") or 0) for s in summaries] or [0]),
        "memory_mib_max": max([float((((s.get("panda_monitor") or {}).get("resources") or {}).get("memory_mib") or {}).get("max") or 0) for s in summaries] or [0]),
        "bandwidth_total_p95_mbps_max": max([float((((s.get("panda_monitor") or {}).get("resources") or {}).get("bandwidth_total_mbps") or {}).get("p95") or 0) for s in summaries] or [0]),
        "strict_bad_count_60m_max": max([int((s.get("panda_monitor") or {}).get("strict_bad_count_60m") or 0) for s in summaries] or [0]),
    }
    return result


def main() -> int:
    print(json.dumps({"event": "suite_start", "suite_id": SUITE_ID, "stage": STAGE, "rounds": ROUNDS, "local_report": str(SUITE_REPORT), "remote_report": SUITE_REMOTE}, ensure_ascii=False), flush=True)
    auth_key = legacy.fetch_auth_key()
    legacy.remote(f"mkdir -p {SUITE_REMOTE}", timeout=30)
    summaries = []
    for round_no in range(1, ROUNDS + 1):
        summaries.append(run_round(round_no, auth_key))
        if round_no < ROUNDS:
            print(json.dumps({"event": "cooldown", "seconds": ROUND_COOLDOWN_SECONDS}, ensure_ascii=False), flush=True)
            time.sleep(ROUND_COOLDOWN_SECONDS)
    agg = aggregate(summaries)
    write_json(SUITE_REPORT / "aggregate-summary.json", agg)
    legacy.scp_to_remote(str(SUITE_REPORT / "aggregate-summary.json"), f"{SUITE_REMOTE}/aggregate-summary.json")
    print(json.dumps({"event": "aggregate_summary", "summary": agg}, ensure_ascii=False), flush=True)
    _SUBMIT_CLIENT.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        error = traceback.format_exc()
        write_json(SUITE_REPORT / "ERROR.json", {"error": error})
        print(error, file=sys.stderr, flush=True)
        raise
