#!/usr/bin/env python3
from __future__ import annotations

import base64
import concurrent.futures
import datetime as dt
import json
import math
import random
import re
import struct
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any


PUBLIC_BASE = "https://gptimage.relai.asia"
STAGE = int(sys.argv[1]) if len(sys.argv) > 1 else 18
if STAGE not in {18, 24, 30}:
    raise SystemExit("stage must be one of: 18, 24, 30")
POLL_INTERVAL = 15.0
MAX_WAIT_SECONDS = 45 * 60
RUN_STAMP = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
RUN_ID = f"r56stage{STAGE}-local-{RUN_STAMP}"
LOCAL_REPORT = Path("reports") / f"loadtest-{RUN_STAMP}-stage-{STAGE}"
REMOTE_REPORT = f"/root/gptimage/backups/loadtest-{RUN_STAMP}-stage-{STAGE}"
LOCAL_REPORT.mkdir(parents=True, exist_ok=True)
random.seed(2026070418)


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def run_cmd(cmd: list[str], timeout: float = 60, *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        timeout=timeout,
    )


def remote(cmd: str, timeout: float = 60) -> str:
    proc = run_cmd(["ssh", "-o", "ConnectTimeout=10", "panda", cmd], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ssh failed rc={proc.returncode}: {proc.stdout}")
    return proc.stdout or ""


def scp_to_remote(local: str, remote_path: str, timeout: float = 120) -> None:
    proc = run_cmd(["scp", local, f"panda:{remote_path}"], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"scp failed rc={proc.returncode}: {proc.stdout}")


def fetch_auth_key() -> str:
    out = remote(
        "python3 -c \"import json;print(json.load(open('/root/gptimage/config.json')).get('auth-key',''))\"",
        timeout=30,
    )
    key = out.strip()
    if not key:
        raise RuntimeError("empty Panda auth key")
    return key


def http_json(
    path: str,
    *,
    auth_key: str,
    method: str = "GET",
    body: Any = None,
    timeout: float = 120,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {auth_key}"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    started = time.time()
    req = urllib.request.Request(PUBLIC_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = raw[:1000].decode("utf-8", "replace")
            return {
                "ok": True,
                "status": resp.status,
                "elapsed_ms": round((time.time() - started) * 1000, 2),
                "bytes": len(raw),
                "body": parsed,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = raw[:1000].decode("utf-8", "replace")
        return {
            "ok": False,
            "status": exc.code,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "bytes": len(raw),
            "body": parsed,
            "error": str(exc),
        }
    except Exception as exc:
        return {"ok": False, "elapsed_ms": round((time.time() - started) * 1000, 2), "error": repr(exc)}


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p / 100
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - k) + vals[hi] * (k - lo)


def summary(values: list[float]) -> dict[str, Any]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"count": 0}
    return {
        "count": len(vals),
        "min": round(min(vals), 3),
        "p50": round(pct(vals, 50) or 0, 3),
        "p95": round(pct(vals, 95) or 0, 3),
        "p99": round(pct(vals, 99) or 0, 3),
        "max": round(max(vals), 3),
        "avg": round(sum(vals) / len(vals), 3),
    }


def png_bytes(width: int, height: int, variant: int) -> bytes:
    rows = []
    rng = random.Random(variant * 99991 + 7)
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            cx = (x - width / 2) / width
            cy = (y - height / 2) / height
            radial = max(0, 1 - (cx * cx + cy * cy) * 2.8)
            stripe = 1 if ((x + variant * 17) // 37 + (y // 53)) % 2 == 0 else 0
            panel = 1 if (width * 0.18 < x < width * 0.82 and height * 0.20 < y < height * 0.74) else 0
            diag = 1 if abs((x - y + variant * 23) % 180 - 90) < 7 else 0
            grain = rng.randint(0, 42)
            r = int((45 + x * 160 / width + 70 * radial + 35 * stripe + grain) % 256)
            g = int((35 + y * 150 / height + 60 * panel + 20 * diag + grain // 2) % 256)
            b = int((90 + (x + y) * 70 / (width + height) + 80 * (1 - radial) + 60 * diag + grain // 3) % 256)
            if panel and (x % 97 < 5 or y % 89 < 5):
                r, g, b = 235, 220, 170
            if (x - width * 0.68) ** 2 + (y - height * 0.38) ** 2 < (width * 0.11) ** 2:
                r, g, b = 230, 88, 72
            if (x - width * 0.32) ** 2 / (width * 0.13) ** 2 + (y - height * 0.62) ** 2 / (
                height * 0.09
            ) ** 2 < 1:
                r, g, b = 70, 150, 210
            row.extend([r, g, b])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def reference_data_url(index: int) -> tuple[str, int, str]:
    data = png_bytes(768, 768, index)
    name = f"reference_{index:02d}.png"
    (LOCAL_REPORT / name).write_bytes(data)
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii"), len(data), name


def task_meta(
    kind: str,
    endpoint: str,
    body: dict[str, Any],
    ref_count: int,
    ref_bytes: int,
    family: str,
    ref_names: list[str] | None = None,
) -> dict[str, Any]:
    payload_bytes = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return {
        "kind": kind,
        "endpoint": endpoint,
        "body": body,
        "payload_bytes": payload_bytes,
        "reference_count": ref_count,
        "reference_total_bytes": ref_bytes,
        "reference_names": ref_names or [],
        "prompt_family": family,
    }


def build_tasks() -> list[dict[str, Any]]:
    gen_count = {18: 6, 24: 8, 30: 10}[STAGE]
    edit_count = STAGE - gen_count
    single_ref_edit_count = {18: 8, 24: 10, 30: 12}[STAGE]
    refs = [reference_data_url(i) for i in range(1, max(13, edit_count + 1))]
    gen_prompts = [
        "Create a precise product-advertising render: matte black smart thermos on a wet stone table, warm backlight, condensation droplets, shallow depth of field, realistic reflections, no text, no logo.",
        "Design a detailed isometric cyberpunk desk setup at night: transparent monitor, mechanical keyboard, small plants, rain on window, neon blue and amber lighting, high object density, no text.",
        "Generate a cinematic editorial portrait of a woman in a structured white coat standing in a minimal gallery, soft rim light, natural skin texture, 85mm lens feel, no text.",
        "Create a cozy Japanese reading corner, tatami, low walnut table, ceramic tea set, morning sunlight, dust particles, layered textiles, accurate perspective, no text.",
        "Render a detailed fantasy market alley with fabric awnings, ceramic lamps, fruit crates, wet cobblestone, many small props, dusk atmosphere, realistic scale, no readable signs.",
        "Create a clean packaging concept for a premium skincare bottle: frosted glass, cream background, botanical shadows, studio lighting, exact centered composition, no text or logo.",
        "Create a realistic outdoor camping product scene: compact lantern, enamel mug, folded map, pine forest background, dusk blue hour, detailed fabric and metal textures, no text.",
        "Generate a high-detail botanical science illustration style image of a glass greenhouse workbench, seedlings, brass tools, water droplets, soft daylight, no labels, no text.",
        "Create a cinematic wide shot of a modern train platform after rain, reflections, commuters in the distance, strong perspective lines, cool color grade, no readable signage.",
        "Render a premium home office setup with walnut desk, large window, books, ceramic lamp, layered shadows, realistic cable management, no text, no logo.",
    ]
    edit_prompts = [
        "Use the reference image as the base style board. Transform it into a realistic premium sneaker campaign image: keep the same color palette and geometric rhythm, add one white sneaker as the hero object, studio lighting, no text.",
        "Use the reference image as material and layout inspiration. Create a modern living room wall art scene with the same dominant colors, preserving the abstract shapes as framed artwork, photorealistic interior, no text.",
        "Use the reference as a visual motif. Generate a high-end cafe counter scene where the shapes become ceramic tile patterns, include espresso machine, pastry tray, warm morning light, no text.",
        "Use the reference composition as a mood board. Create a fashion editorial image: model wearing a jacket inspired by the reference colors and panel lines, realistic fabric detail, no text.",
        "Use the reference image to guide palette and texture. Create a futuristic electric bicycle product render in a clean studio, integrate the abstract panel pattern subtly into the background, no text.",
        "Use the reference as a style source. Create an architectural visualization of a boutique hotel lobby with similar color blocking, polished floor reflections, realistic lighting, no text.",
        "Use the reference image as a color and texture guide. Create a detailed food photography scene: plated dessert with geometric chocolate decoration, ceramic plate, soft side light, no text.",
        "Use the reference as the base mood. Create a realistic album-cover-like still life without text: headphones, vinyl record, glass table, colored light reflections, no typography.",
        "Use both reference images: first for color palette, second for geometric layout. Create a realistic concept car interior dashboard, premium materials, ambient lighting, no text.",
        "Use both reference images: first for texture, second for composition. Create a luxury watch macro product render, metal reflections, precise dial details, no text or logo.",
        "Use both reference images as style and layout guides. Create a cinematic kitchen countertop scene with glassware, fruit, marble, reflective surfaces, high detail, no text.",
        "Use both reference images as visual constraints. Create a realistic museum installation room with abstract panels, visitors in the distance, controlled lighting, accurate perspective, no text.",
        "Use the reference image as a material board. Create a realistic boutique perfume display with glass bottles, colored acrylic panels, soft caustic reflections, no text.",
        "Use the reference image as shape and palette guidance. Create a premium gaming handheld product render on a reflective black table, colored background panels, no text.",
        "Use both reference images: first for palette, second for spatial rhythm. Create a realistic hotel suite bedroom with abstract headboard wall, fabric detail, no text.",
        "Use both reference images to guide texture and color. Create a detailed studio photograph of a ceramic table lamp and books, controlled shadows, no text.",
    ]
    tasks = []
    for i, prompt in enumerate(gen_prompts[:gen_count]):
        body = {
            "client_task_id": f"{RUN_ID}-gen-{i:02d}",
            "prompt": prompt,
            "model": "gpt-image-2",
            "size": "1024x1024",
            "quality": "auto",
        }
        tasks.append(task_meta("generation", "/api/image-tasks/generations", body, 0, 0, "text_complex"))
    for i, prompt in enumerate(edit_prompts[:edit_count]):
        selected = [refs[i % len(refs)]] if i < single_ref_edit_count else [refs[(i * 2) % len(refs)], refs[(i * 2 + 1) % len(refs)]]
        body = {
            "client_task_id": f"{RUN_ID}-edit-{i:02d}",
            "prompt": prompt,
            "model": "gpt-image-2",
            "size": "1024x1024",
            "quality": "auto",
            "images": [item[0] for item in selected],
        }
        tasks.append(
            task_meta(
                "edit",
                "/api/image-tasks/edits",
                body,
                len(selected),
                sum(item[1] for item in selected),
                "edit_reference_multi" if len(selected) > 1 else "edit_reference_single",
                [item[2] for item in selected],
            )
        )
    assert len(tasks) == STAGE
    return tasks


def submit_one(task: dict[str, Any], auth_key: str) -> dict[str, Any]:
    started = time.time()
    res = http_json(task["endpoint"], auth_key=auth_key, method="POST", body=task["body"], timeout=180)
    body = res.get("body") if isinstance(res.get("body"), dict) else {}
    return {
        "task_id": task["body"]["client_task_id"],
        "kind": task["kind"],
        "endpoint": task["endpoint"],
        "status": res.get("status"),
        "ok": bool(res.get("ok")),
        "elapsed_ms": round((time.time() - started) * 1000, 2),
        "response_elapsed_ms": res.get("elapsed_ms"),
        "body_status": body.get("status"),
        "body": {key: body.get(key) for key in ["id", "status", "created_at", "updated_at"]} if res.get("ok") else res.get("body"),
        "error": res.get("error"),
        "payload_bytes": task["payload_bytes"],
        "reference_count": task["reference_count"],
        "reference_total_bytes": task["reference_total_bytes"],
        "prompt_family": task["prompt_family"],
    }


def list_status(task_ids: list[str], auth_key: str) -> dict[str, Any]:
    ids = urllib.parse.quote(",".join(task_ids))
    res = http_json("/api/image-tasks/status?ids=" + ids, auth_key=auth_key, timeout=60)
    items = []
    if isinstance(res.get("body"), dict) and isinstance(res["body"].get("items"), list):
        items = res["body"]["items"]
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "missing")
        counts[status] = counts.get(status, 0) + 1
    return {"response": res, "items": items, "counts": counts, "latency_ms": res.get("elapsed_ms")}


def wait_remote_file(path: str, timeout: float) -> None:
    end = time.time() + timeout
    while time.time() < end:
        out = remote(f"test -f {path} && echo yes || true", timeout=20).strip()
        if out == "yes":
            return
        time.sleep(3)
    raise TimeoutError(f"remote file not ready: {path}")


def start_monitor() -> None:
    scp_to_remote("scripts/r56_panda_monitor.py", "/tmp/r56_panda_monitor.py")
    remote(f"mkdir -p {REMOTE_REPORT}", timeout=30)
    cmd = (
        f"cd /root/gptimage; nohup python3 /tmp/r56_panda_monitor.py "
        f"{REMOTE_REPORT} {RUN_ID} </dev/null > {REMOTE_REPORT}/monitor.stdout.log 2>&1 & echo $!"
    )
    pid = remote(cmd, timeout=30).strip()
    (LOCAL_REPORT / "remote-monitor-pid.txt").write_text(pid, encoding="utf-8")


def stop_monitor() -> None:
    remote(f"touch {REMOTE_REPORT}/stop-monitor", timeout=20)
    wait_remote_file(f"{REMOTE_REPORT}/monitor.done", timeout=180)


def main() -> int:
    print(json.dumps({"event": "start", "run_id": RUN_ID, "local_report": str(LOCAL_REPORT), "remote_report": REMOTE_REPORT}, ensure_ascii=False), flush=True)
    auth_key = fetch_auth_key()
    monitor_started = False
    tasks = build_tasks()
    manifest = {
        "run_id": RUN_ID,
        "public_base": PUBLIC_BASE,
        "remote_report": REMOTE_REPORT,
        "tasks": [
            {key: value for key, value in task.items() if key != "body"}
            | {
                "client_task_id": task["body"]["client_task_id"],
                "prompt": task["body"]["prompt"],
                "model": task["body"]["model"],
                "size": task["body"].get("size"),
                "quality": task["body"].get("quality"),
            }
            for task in tasks
        ],
        "total_payload_bytes": sum(task["payload_bytes"] for task in tasks),
        "total_reference_bytes": sum(task["reference_total_bytes"] for task in tasks),
        "generation_count": sum(1 for task in tasks if task["kind"] == "generation"),
        "edit_count": sum(1 for task in tasks if task["kind"] == "edit"),
    }
    submit_results: list[dict[str, Any]] = []
    submit_elapsed = 0.0
    status_history: list[dict[str, Any]] = []
    final_status: dict[str, Any] | None = None

    try:
        start_monitor()
        monitor_started = True
        print(json.dumps({"event": "wait_monitor_ready"}, ensure_ascii=False), flush=True)
        wait_remote_file(f"{REMOTE_REPORT}/monitor.ready", timeout=180)
        print(json.dumps({"event": "monitor_ready"}, ensure_ascii=False), flush=True)

        write_json(LOCAL_REPORT / "task-manifest.json", manifest)
        scp_to_remote(str(LOCAL_REPORT / "task-manifest.json"), f"{REMOTE_REPORT}/task-manifest.json")

        print(
            json.dumps(
                {
                    "event": "submit_start",
                    "count": len(tasks),
                    "generation_count": manifest["generation_count"],
                    "edit_count": manifest["edit_count"],
                    "payload_total_bytes": manifest["total_payload_bytes"],
                    "reference_total_bytes": manifest["total_reference_bytes"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        submit_started = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=STAGE) as executor:
            futures = [executor.submit(submit_one, task, auth_key) for task in tasks]
            for future in concurrent.futures.as_completed(futures):
                item = future.result()
                submit_results.append(item)
                print(
                    json.dumps(
                        {
                            "event": "submitted",
                            "task_id": item["task_id"],
                            "kind": item["kind"],
                            "status": item["status"],
                            "ok": item["ok"],
                            "elapsed_ms": item["elapsed_ms"],
                            "body_status": item.get("body_status"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        submit_results.sort(key=lambda row: row["task_id"])
        submit_elapsed = time.time() - submit_started
        accepted_task_ids = [
            row["task_id"]
            for row in submit_results
            if row.get("ok") and row.get("status") in (200, 201, 202)
        ]
        all_task_ids = [row["task_id"] for row in submit_results]
        write_json(LOCAL_REPORT / "submit-results.json", {"elapsed_seconds": submit_elapsed, "results": submit_results})
        write_json(LOCAL_REPORT / "task-ids.json", {"task_ids": accepted_task_ids, "all_task_ids": all_task_ids})
        scp_to_remote(str(LOCAL_REPORT / "task-ids.json"), f"{REMOTE_REPORT}/task-ids.json")

        print(
            json.dumps(
                {
                    "event": "submit_done",
                    "elapsed_seconds": round(submit_elapsed, 3),
                    "ok": len(accepted_task_ids),
                    "total": len(submit_results),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        started = time.time()
        while accepted_task_ids:
            time.sleep(POLL_INTERVAL)
            status = list_status(accepted_task_ids, auth_key)
            status["elapsed_seconds"] = time.time() - started
            status_history.append(status)
            print(
                json.dumps(
                    {
                        "event": "status",
                        "elapsed_seconds": round(status["elapsed_seconds"], 1),
                        "counts": status.get("counts"),
                        "status_latency_ms": status.get("latency_ms"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
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
        write_json(LOCAL_REPORT / "status-history.json", {"history": status_history, "final_status": final_status})
    finally:
        if monitor_started:
            try:
                stop_monitor()
            except Exception as exc:
                (LOCAL_REPORT / "monitor-stop-error.txt").write_text(repr(exc), encoding="utf-8")
        run_cmd(["scp", "-r", f"panda:{REMOTE_REPORT}", str(LOCAL_REPORT / "remote")], timeout=300)

    submit_lat = [float(row["elapsed_ms"]) for row in submit_results]
    status_lat = [float(row.get("latency_ms") or 0) for row in status_history if row.get("latency_ms") is not None]
    items = final_status.get("items") if isinstance(final_status, dict) else []
    counts: dict[str, int] = {}
    if isinstance(items, list):
        for item in items:
            status = str(item.get("status") or "missing")
            counts[status] = counts.get(status, 0) + 1
    ok_submits = [row for row in submit_results if row.get("ok") and row.get("status") in (200, 201, 202)]
    failed_submits = [row for row in submit_results if not (row.get("ok") and row.get("status") in (200, 201, 202))]
    summary_obj = {
        "run_id": RUN_ID,
        "public_base": PUBLIC_BASE,
        "local_report": str(LOCAL_REPORT),
        "remote_report": REMOTE_REPORT,
        "input_mix": {
            "generation_count": manifest["generation_count"],
            "edit_count": manifest["edit_count"],
            "single_reference_edit_count": sum(1 for task in tasks if task["kind"] == "edit" and task["reference_count"] == 1),
            "multi_reference_edit_count": sum(1 for task in tasks if task["kind"] == "edit" and task["reference_count"] > 1),
            "total_payload_bytes": manifest["total_payload_bytes"],
            "total_reference_bytes": manifest["total_reference_bytes"],
            "no_input_reduction": True,
            "reference_resolution": "768x768 textured PNG references",
        },
        "submit": {
            "ok": len(ok_submits),
            "failed": len(failed_submits),
            "total": len(submit_results),
            "elapsed_seconds": submit_elapsed,
            "latency_ms": summary(submit_lat),
            "http_status_counts": {str(code): sum(1 for row in submit_results if str(row.get("status")) == str(code)) for code in sorted({row.get("status") for row in submit_results}, key=str)},
            "failed_errors": [
                {
                    "task_id": row.get("task_id"),
                    "kind": row.get("kind"),
                    "elapsed_ms": row.get("elapsed_ms"),
                    "error": row.get("error"),
                    "reference_count": row.get("reference_count"),
                    "reference_total_bytes": row.get("reference_total_bytes"),
                }
                for row in failed_submits
            ],
        },
        "status_query_latency_ms": summary(status_lat),
        "final_counts": counts,
    }
    remote_summary_path = LOCAL_REPORT / "remote" / Path(REMOTE_REPORT).name / "monitor-summary.json"
    if remote_summary_path.exists():
        summary_obj["panda_monitor"] = json.loads(remote_summary_path.read_text(encoding="utf-8"))
    write_json(LOCAL_REPORT / "summary.json", summary_obj)
    scp_to_remote(str(LOCAL_REPORT / "summary.json"), f"{REMOTE_REPORT}/loadgen-summary.json")
    print(json.dumps({"event": "summary", "summary": summary_obj}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        error = traceback.format_exc()
        (LOCAL_REPORT / "ERROR.txt").write_text(error, encoding="utf-8")
        print(error, file=sys.stderr, flush=True)
        raise
