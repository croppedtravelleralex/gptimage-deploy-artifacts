#!/usr/bin/env python3
"""Capture pre-SlotLedger performance baseline for later comparison.

Fetches live Panda health + pipeline snapshot when reachable; otherwise seeds
from static reference values documented in docs/26-slot-lifecycle-rust-roadmap.md.

Outputs timestamped JSON + markdown under docs/captures/spa/.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CAPTURE_DIR = ROOT / "docs" / "captures" / "spa"
CAPTURE_PREFIX = "BASELINE-pre-slotledger"

# Authoritative static references from docs/26-slot-lifecycle-rust-roadmap.md
STATIC_DOCS_26: dict[str, Any] = {
    "doc": "docs/26-slot-lifecycle-rust-roadmap.md",
    "rss_mb": {
        "after_restart": 104,
        "post_conc10_evict_compact": 259,
        "post_conc10_pre_fix": 443,
    },
    "conc10_refs": {
        "PROD-conc10-20260724T150152Z": {"ok": 10, "total": 10, "note": "reference pass"},
        "PROD-conc10-20260725T023900Z": {"ok": 10, "total": 10, "note": "reference pass"},
        "PROD-conc10-20260725T040240Z": {"ok": 4, "total": 10, "note": "post inflight-leak fix"},
        "PROD-conc10-20260725T034701Z": {"ok": 0, "total": 10, "note": "image_inflight=10 leak"},
        "PROD-conc10-20260725T033622Z": {"ok": 6, "total": 10, "note": "shared egress"},
    },
    "dispatchable_snapshot": {
        "schedulable": 16,
        "ready_candidate_count": 6,
        "dispatchable_candidate_count": 6,
        "preflight_backoff_count": 0,
        "image_inflight_count": 3,
        "note": "humanlike image_next_ok_ts gap after conc10",
    },
    "pool": {
        "total": 19,
        "image_schedulable": 16,
        "unique_egress": 19,
    },
}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_auth_key() -> str:
    for candidate in (ROOT / "config.json", Path("/root/gptimage/config.json")):
        if not candidate.is_file():
            continue
        cfg = json.loads(candidate.read_text(encoding="utf-8"))
        key = str(cfg.get("auth-key") or cfg.get("auth_key") or "").strip()
        if key:
            return key
    return ""


def _http_get(
    base: str,
    path: str,
    *,
    auth_key: str = "",
    timeout: float = 15,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if auth_key:
        headers["Authorization"] = f"Bearer {auth_key}"
    req = urllib.request.Request(base.rstrip("/") + path, headers=headers, method="GET")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = raw[:1200].decode("utf-8", "replace")
            return {
                "ok": True,
                "status": resp.status,
                "elapsed_ms": round((time.time() - started) * 1000, 2),
                "body": body,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = raw[:1200].decode("utf-8", "replace")
        return {
            "ok": False,
            "status": exc.code,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "body": body,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "body": str(exc),
        }


def _probe_server(base: str) -> bool:
    res = _http_get(base, "/health?format=json", timeout=5)
    if not res.get("ok"):
        return False
    body = res.get("body")
    return isinstance(body, dict) and ("healthy" in body or "accounts" in body)


def _local_process_memory() -> dict[str, Any]:
    try:
        sys.path.insert(0, str(ROOT))
        from utils.process_memory import process_memory_snapshot

        mem = process_memory_snapshot()
        return mem if mem else {}
    except Exception as exc:
        return {"error": str(exc)}


def _extract_comparison(
    *,
    health: dict[str, Any] | None,
    pipeline_snapshot: dict[str, Any] | None,
    static: bool,
) -> dict[str, Any]:
    accounts = (health or {}).get("accounts") if isinstance(health, dict) else {}
    image_runtime = (health or {}).get("image_runtime") if isinstance(health, dict) else {}
    process_memory = (health or {}).get("process_memory") if isinstance(health, dict) else {}
    workload = (health or {}).get("workload") if isinstance(health, dict) else {}

    if not isinstance(accounts, dict):
        accounts = {}
    if not isinstance(image_runtime, dict):
        image_runtime = {}
    if not isinstance(process_memory, dict):
        process_memory = {}
    if not isinstance(workload, dict):
        workload = {}

    ready = int(
        image_runtime.get("ready_candidate_count")
        or accounts.get("ready_candidate_count")
        or 0
    )
    dispatchable = int(
        image_runtime.get("dispatchable_candidate_count")
        or accounts.get("dispatchable_candidate_count")
        or 0
    )
    inflight = int(
        image_runtime.get("image_inflight_count")
        or accounts.get("image_inflight_count")
        or 0
    )

    if static:
        snap = STATIC_DOCS_26["dispatchable_snapshot"]
        ready = int(snap["ready_candidate_count"])
        dispatchable = int(snap["dispatchable_candidate_count"])
        inflight = int(snap["image_inflight_count"])

    ps_queued = ss_queued = upload_queued = download_queued = None
    ps_active = ss_active = None
    pipeline_in_flight = None
    if isinstance(pipeline_snapshot, dict):
        pipeline_in_flight = pipeline_snapshot.get("in_flight")
        for key, var_prefix in (
            ("ps", "ps"),
            ("ss", "ss"),
            ("upload", "upload"),
            ("download", "download"),
        ):
            pool = pipeline_snapshot.get(key)
            if isinstance(pool, dict):
                if var_prefix == "ps":
                    ps_queued = pool.get("queued")
                    ps_active = pool.get("active")
                elif var_prefix == "ss":
                    ss_queued = pool.get("queued")
                    ss_active = pool.get("active")
                elif var_prefix == "upload":
                    upload_queued = pool.get("queued")
                elif var_prefix == "download":
                    download_queued = pool.get("queued")

    rss_mb = process_memory.get("rss_mb")
    if static and rss_mb is None:
        rss_mb = STATIC_DOCS_26["rss_mb"]["after_restart"]

    return {
        "version": (health or {}).get("version") if isinstance(health, dict) else None,
        "rss_mb": rss_mb,
        "image_inflight_count": inflight,
        "ready_candidate_count": ready,
        "dispatchable_candidate_count": dispatchable,
        "preflight_backoff_count": int(
            image_runtime.get("preflight_backoff_count")
            or accounts.get("preflight_backoff_count")
            or (STATIC_DOCS_26["dispatchable_snapshot"]["preflight_backoff_count"] if static else 0)
        ),
        "schedulable": int(
            accounts.get("schedulable")
            or accounts.get("image_schedulable")
            or (STATIC_DOCS_26["dispatchable_snapshot"]["schedulable"] if static else 0)
        ),
        "text_queue_depth": workload.get("text_queue_depth"),
        "image_queue_depth": workload.get("image_queue_depth"),
        "pipeline_in_flight": pipeline_in_flight,
        "ps_active": ps_active,
        "ps_queued": ps_queued,
        "ss_active": ss_active,
        "ss_queued": ss_queued,
        "upload_queued": upload_queued,
        "download_queued": download_queued,
        "image_global_concurrency_limit": image_runtime.get("image_global_concurrency_limit")
        or accounts.get("image_global_concurrency_limit"),
        "image_global_limit_reached": image_runtime.get("image_global_limit_reached")
        if image_runtime
        else accounts.get("image_global_limit_reached"),
    }


def _render_markdown(payload: dict[str, Any]) -> str:
    cmp_ = payload.get("comparison") or {}
    lines = [
        f"# BASELINE pre-SlotLedger — {payload.get('stamp')}",
        "",
        f"- Source: **{payload.get('source')}**",
        f"- Base URL: `{payload.get('base_url')}`",
        f"- Generated: {payload.get('generated_at')}",
        "",
        "## Comparison fields",
        "",
        "| Field | Value |",
        "|-------|-------|",
    ]
    for key in (
        "version",
        "rss_mb",
        "image_inflight_count",
        "ready_candidate_count",
        "dispatchable_candidate_count",
        "preflight_backoff_count",
        "schedulable",
        "text_queue_depth",
        "image_queue_depth",
        "pipeline_in_flight",
        "ps_active",
        "ps_queued",
        "ss_active",
        "ss_queued",
        "upload_queued",
        "download_queued",
        "image_global_concurrency_limit",
        "image_global_limit_reached",
    ):
        val = cmp_.get(key)
        lines.append(f"| {key} | {val if val is not None else '—'} |")

    if payload.get("source") == "static_docs_26":
        lines.extend(
            [
                "",
                "## Static reference (docs/26)",
                "",
                "### RSS (MB)",
                "",
                "| Scenario | MB |",
                "|----------|-----|",
            ]
        )
        for name, mb in STATIC_DOCS_26["rss_mb"].items():
            lines.append(f"| {name} | {mb} |")
        lines.extend(["", "### conc10 references", "", "| Capture | Result | Note |", "|---------|--------|------|"])
        for stamp, info in STATIC_DOCS_26["conc10_refs"].items():
            lines.append(
                f"| {stamp} | {info['ok']}/{info['total']} | {info.get('note', '')} |"
            )
        snap = STATIC_DOCS_26["dispatchable_snapshot"]
        lines.extend(
            [
                "",
                "### dispatchable=6 snapshot",
                "",
                f"- schedulable={snap['schedulable']}",
                f"- ready_candidate_count={snap['ready_candidate_count']}",
                f"- dispatchable_candidate_count={snap['dispatchable_candidate_count']}",
                f"- image_inflight_count={snap['image_inflight_count']}",
                f"- note: {snap['note']}",
            ]
        )

    if payload.get("local_process_memory"):
        lpm = payload["local_process_memory"]
        lines.extend(["", "## Local runner process_memory", "", f"- rss_mb: {lpm.get('rss_mb', 'n/a')}"])

    if payload.get("health_fetch"):
        hf = payload["health_fetch"]
        lines.extend(["", "## Health fetch", "", f"- ok: {hf.get('ok')} · status: {hf.get('status')} · {hf.get('elapsed_ms')} ms"])

    if payload.get("pipeline_fetch"):
        pf = payload["pipeline_fetch"]
        lines.extend(
            ["", "## Pipeline snapshot fetch", "", f"- ok: {pf.get('ok')} · status: {pf.get('status')} · {pf.get('elapsed_ms')} ms"]
        )

    lines.append("")
    return "\n".join(lines)


def capture(*, base_url: str | None = None) -> dict[str, Any]:
    stamp = _utc_stamp()
    base = (base_url or os.environ.get("PANDA_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    auth_key = _load_auth_key()
    generated_at = datetime.now(timezone.utc).isoformat()

    live = _probe_server(base)
    health_body: dict[str, Any] | None = None
    pipeline_body: dict[str, Any] | None = None
    health_fetch: dict[str, Any] | None = None
    pipeline_fetch: dict[str, Any] | None = None
    local_mem = _local_process_memory()

    if live:
        health_fetch = _http_get(base, "/health?format=json")
        if health_fetch.get("ok") and isinstance(health_fetch.get("body"), dict):
            health_body = health_fetch["body"]
        if auth_key:
            pipeline_fetch = _http_get(
                base,
                "/api/ops/image-pipeline/snapshot",
                auth_key=auth_key,
            )
            if pipeline_fetch.get("ok") and isinstance(pipeline_fetch.get("body"), dict):
                pipeline_body = pipeline_fetch["body"]
        source = "live"
    else:
        source = "static_docs_26"
        health_body = {
            "status": "static",
            "healthy": None,
            "version": None,
            "accounts": {
                **STATIC_DOCS_26["pool"],
                **STATIC_DOCS_26["dispatchable_snapshot"],
            },
            "process_memory": {"rss_mb": STATIC_DOCS_26["rss_mb"]["after_restart"]},
            "image_runtime": STATIC_DOCS_26["dispatchable_snapshot"],
            "workload": {"text_queue_depth": 0, "image_queue_depth": 0},
        }

    comparison = _extract_comparison(
        health=health_body,
        pipeline_snapshot=pipeline_body,
        static=not live,
    )

    payload: dict[str, Any] = {
        "capture_kind": CAPTURE_PREFIX,
        "stamp": stamp,
        "generated_at": generated_at,
        "source": source,
        "base_url": base,
        "panda_reachable": live,
        "comparison": comparison,
        "health": health_body,
        "pipeline_snapshot": pipeline_body,
        "static_reference": STATIC_DOCS_26 if not live else None,
        "local_process_memory": local_mem or None,
        "health_fetch": health_fetch,
        "pipeline_fetch": pipeline_fetch,
    }

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    json_path = CAPTURE_DIR / f"{CAPTURE_PREFIX}-{stamp}.json"
    md_path = CAPTURE_DIR / f"{CAPTURE_PREFIX}-{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render_markdown(payload), encoding="utf-8")

    payload["_output_paths"] = {"json": str(json_path), "markdown": str(md_path)}
    return payload


def main() -> int:
    result = capture()
    paths = result.get("_output_paths") or {}
    print(json.dumps({"paths": paths, "comparison": result.get("comparison"), "source": result.get("source")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
