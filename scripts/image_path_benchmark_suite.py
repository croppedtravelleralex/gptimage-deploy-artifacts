#!/usr/bin/env python3
"""Unified image-path benchmark suite — pure HTTP / browser / ticket pool + compare.

Evidence layout (default):
  data/runlogs/image-path-benchmark/{date}/
    pure_http/result_*.json
    browser/result_*.json
    ticket_pool/result_*.json
    compare-summary.json

Schema: image-path-benchmark/v1 (see SCHEMA_VERSION and normalize_run_record).

Examples:
  python scripts/image_path_benchmark_suite.py pure_http --mode local_clash --secret data/runlogs/spa_repro/qaflow_secret.json
  python scripts/image_path_benchmark_suite.py pure_http --runs 3 --mode panda_webshare
  python scripts/image_path_benchmark_suite.py browser --help
  python scripts/image_path_benchmark_suite.py ticket_pool --help
  python scripts/image_path_benchmark_suite.py compare \\
    --pure-http-dir data/runlogs/image-path-benchmark/20260724/pure_http \\
    --browser-dir data/runlogs/image-path-benchmark/20260724/browser \\
    --ticket-pool-dir data/runlogs/image-path-benchmark/20260724/ticket_pool
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_ROOT_OVERRIDE = str(os.environ.get("GPTIMAGE_ROOT") or "").strip()
if _ROOT_OVERRIDE and Path(_ROOT_OVERRIDE).is_dir():
    ROOT = Path(_ROOT_OVERRIDE).resolve()
sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = "image-path-benchmark/v1"
OUT_ROOT = ROOT / "data" / "runlogs" / "image-path-benchmark"
SECRET_DEFAULT = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
MEDIUM_PROMPT = (
    "Create a medium-detail digital illustration of a rainy Tokyo side street at dusk: "
    "neon shop signs reflecting on wet asphalt, a bicycle parked under a red awning, "
    "warm interior lights spilling onto the sidewalk, cinematic atmosphere, soft depth of field, "
    "no text, no watermark, no logos"
)

BROWSER_ENTRY_POINTS = [
    {
        "script": "scripts/_tmp_spa_camoufox_image_http_repro.py",
        "description": "Camoufox request API (Firefox TLS) + local Clash; spa_tool text shape",
        "proxy_default": "http://127.0.0.1:7897",
    },
    {
        "script": "scripts/_tmp_spa_camoufox_via_panda_socks.py",
        "description": "Camoufox via SSH -D SOCKS to panda egress; bypass curl_cffi CF on prepare",
        "proxy_default": "socks5://127.0.0.1:18080",
    },
    {
        "script": "scripts/_tmp_spa_camoufox_har.py",
        "description": "HAR replay / capture helper for browser TLS fingerprint studies",
    },
    {
        "script": "scripts/outlook_camoufox_stable_register.py",
        "description": "Production Camoufox registration pipeline (not image gen; related egress stack)",
    },
]

TICKET_POOL_ENTRY_POINTS = [
    {
        "service": "gptimage-gateway-rs Python helper",
        "url": "http://127.0.0.1:19001",
        "endpoint": "POST /v1/images/generations",
        "notes": "Rust :8013 fronts helper :19001; ticket_pool crate TTL=300s",
        "docs": "docs/22-ticket-image-pipeline-and-go-spike.md",
    },
]

try:
    import psutil  # type: ignore[import-untyped]

    _HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False


def _log(**kw: Any) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _p50_p90(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "p50": None, "p90": None, "mean": None, "min": None, "max": None}
    return {
        "n": len(values),
        "p50": _pct(values, 50),
        "p90": _pct(values, 90),
        "mean": round(statistics.mean(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_int(*values: object) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _compute_tokens_per_sec(usage: dict[str, Any] | None, sse_stream_ms: int | None) -> float | None:
    if not usage or not sse_stream_ms or sse_stream_ms <= 0:
        return None
    completion = _as_int(usage.get("completion_tokens"))
    if completion is None:
        return None
    return round(completion / (sse_stream_ms / 1000.0), 3)


def sample_resources() -> dict[str, Any]:
    if not _HAS_PSUTIL:
        return {"psutil_available": False}
    proc = psutil.Process()
    with proc.oneshot():
        mem = proc.memory_info()
        return {
            "psutil_available": True,
            "rss_bytes": int(mem.rss),
            "vms_bytes": int(mem.vms),
            "cpu_percent": round(proc.cpu_percent(interval=0.0), 3),
        }


def normalize_run_record(
    raw: dict[str, Any],
    *,
    path: str,
    run_id: str | None = None,
    resources_before: dict[str, Any] | None = None,
    resources_after: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map bench3 / pipeline / stub payloads into image-path-benchmark/v1."""
    timings = raw.get("timings_ms") if isinstance(raw.get("timings_ms"), dict) else {}
    phase = raw.get("phase_timings_ms") if isinstance(raw.get("phase_timings_ms"), dict) else {}
    traffic = raw.get("traffic") if isinstance(raw.get("traffic"), dict) else {}
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None

    wall_clock_ms = _first_int(
        phase.get("wall_clock_ms"),
        raw.get("wall_clock_ms"),
        raw.get("total_wall_ms"),
        timings.get("total_ms"),
    )
    sse_stream_ms = _first_int(
        phase.get("sse_stream_ms"),
        timings.get("sse_ms"),
        timings.get("sse_total_ms"),
    )
    download_ms = _first_int(phase.get("download_ms"), timings.get("download_ms"))

    upload_bytes = _first_int(
        raw.get("upload_bytes"),
        traffic.get("req_bytes"),
        traffic.get("upload_bytes"),
    )
    download_bytes = _first_int(
        raw.get("download_bytes"),
        traffic.get("resp_bytes"),
        traffic.get("download_bytes"),
    )
    sse_bytes = _first_int(raw.get("sse_bytes"), traffic.get("sse_bytes"))

    image_bytes = 0
    images = raw.get("images")
    if isinstance(images, list):
        for item in images:
            if isinstance(item, dict):
                image_bytes += int(item.get("bytes") or 0)
    elif isinstance(raw.get("image"), dict):
        image_bytes = int(raw["image"].get("bytes") or 0)

    tokens_per_sec = _as_float(raw.get("tokens_per_sec"))
    if tokens_per_sec is None:
        tokens_per_sec = _compute_tokens_per_sec(usage, sse_stream_ms)

    account = raw.get("account") if isinstance(raw.get("account"), dict) else {}
    proxy = raw.get("proxy") if isinstance(raw.get("proxy"), dict) else {}

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or raw.get("run_id") or f"{path}_{int(time.time())}",
        "generated_at": raw.get("generated_at") or datetime.now(timezone.utc).isoformat(),
        "path": path,
        "ok": bool(raw.get("ok")),
        "account": {
            "email": account.get("email") or raw.get("account_email"),
            "hash": account.get("hash") or raw.get("account_hash"),
        },
        "proxy": {
            "provider": proxy.get("provider") or raw.get("proxy_provider"),
            "hash": proxy.get("hash") or raw.get("proxy_hash"),
            "egress_ip": raw.get("egress", {}).get("ip") if isinstance(raw.get("egress"), dict) else raw.get("egress_ip"),
        },
        "prompt": raw.get("prompt") if isinstance(raw.get("prompt"), dict) else {
            "chars": len(str(raw.get("prompt_text") or "")),
            "sha256": hashlib.sha256(str(raw.get("prompt_text") or "").encode("utf-8")).hexdigest()
            if raw.get("prompt_text")
            else None,
        },
        "conversation_id": raw.get("conversation_id") or (raw.get("conversation") or {}).get("id"),
        "failure_class": raw.get("failure_class"),
        "error": raw.get("error"),
        "phase_timings_ms": {
            "wall_clock_ms": wall_clock_ms or 0,
            "sse_stream_ms": sse_stream_ms or 0,
            "download_ms": download_ms or 0,
            "egress_ms": _first_int(phase.get("egress_ms"), timings.get("egress_ms")) or 0,
            "requirements_ms": _first_int(phase.get("requirements_ms"), timings.get("requirements_ms")) or 0,
            "prepare_ms": _first_int(phase.get("prepare_ms"), timings.get("prepare_ms")) or 0,
            "poll_resolve_ms": _first_int(phase.get("poll_resolve_ms"), timings.get("poll_resolve_ms")) or 0,
            "sse_gate_ms": _first_int(phase.get("sse_gate_ms"), timings.get("sse_gate_ms")) or 0,
            "task_queue_ms": _first_int(phase.get("task_queue_ms"), raw.get("task_queue_ms")) or 0,
        },
        "traffic_bytes": {
            "upload_bytes": upload_bytes or 0,
            "download_bytes": download_bytes or 0,
            "sse_bytes": sse_bytes or 0,
            "total_bytes": _first_int(traffic.get("total_bytes"), (upload_bytes or 0) + (download_bytes or 0)),
            "image_bytes": image_bytes,
            "http_calls": _first_int(traffic.get("http_calls")),
        },
        "usage": usage,
        "tokens_per_sec": tokens_per_sec,
        "resources": {
            "before": resources_before,
            "after": resources_after,
        },
        "source_schema": raw.get("schema_version"),
        "raw": raw,
    }


def _load_json_files(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    # Prefer normalized result_*.json; fall back to other non-stub JSON if none.
    preferred = sorted(directory.glob("result_*.json"))
    candidates = preferred or sorted(directory.glob("*.json"))
    for path in candidates:
        if path.name in ("compare-summary.json", "manifest.json"):
            continue
        if path.name.startswith("bench3_raw_") or path.name.startswith("stub_"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("stub") is True:
            continue
        if payload.get("schema_version") == SCHEMA_VERSION and isinstance(payload.get("raw"), dict):
            rows.append(payload)
        else:
            path_name = directory.name.replace("-", "_")
            if path_name not in ("pure_http", "browser", "ticket_pool"):
                path_name = str(payload.get("path") or "unknown")
            rows.append(normalize_run_record(payload, path=path_name, run_id=path.stem))
    return rows


def _metric_values(runs: list[dict[str, Any]], *keys: str) -> list[float]:
    out: list[float] = []
    for run in runs:
        phase = run.get("phase_timings_ms") if isinstance(run.get("phase_timings_ms"), dict) else {}
        traffic = run.get("traffic_bytes") if isinstance(run.get("traffic_bytes"), dict) else {}
        for key in keys:
            value = phase.get(key)
            if value is None:
                value = traffic.get(key)
            if value is None:
                value = run.get(key)
            parsed = _as_float(value)
            if parsed is not None:
                out.append(parsed)
                break
    return out


def compare_paths(
    pure_http_dir: Path,
    browser_dir: Path,
    ticket_pool_dir: Path,
    *,
    out_path: Path | None = None,
) -> dict[str, Any]:
    groups = {
        "pure_http": _load_json_files(pure_http_dir),
        "browser": _load_json_files(browser_dir),
        "ticket_pool": _load_json_files(ticket_pool_dir),
    }

    def _group_summary(path: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
        ok_count = sum(1 for r in runs if r.get("ok"))
        return {
            "path": path,
            "evidence_dir": str({"pure_http": pure_http_dir, "browser": browser_dir, "ticket_pool": ticket_pool_dir}[path]),
            "runs": len(runs),
            "success_rate": round(ok_count / len(runs), 4) if runs else None,
            "metrics": {
                "wall_clock_ms": _p50_p90(_metric_values(runs, "wall_clock_ms")),
                "sse_stream_ms": _p50_p90(_metric_values(runs, "sse_stream_ms")),
                "download_ms": _p50_p90(_metric_values(runs, "download_ms")),
                "tokens_per_sec": _p50_p90(_metric_values(runs, "tokens_per_sec")),
                "upload_bytes": _p50_p90(_metric_values(runs, "upload_bytes")),
                "download_bytes": _p50_p90(_metric_values(runs, "download_bytes")),
            },
        }

    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "compare_kind": "three_path_image_benchmark",
        "paths": {name: _group_summary(name, rows) for name, rows in groups.items()},
    }

    if out_path is None:
        parent = pure_http_dir.parent
        if browser_dir.parent == ticket_pool_dir.parent == parent:
            out_path = parent / "compare-summary.json"
        else:
            out_path = OUT_ROOT / _utc_date() / "compare-summary.json"

    _atomic_write_json(out_path, summary)
    _log(phase="compare_done", path=str(out_path), paths={k: len(v) for k, v in groups.items()})
    return summary


def _resolve_proxy(mode: str, secret: dict[str, Any], proxy_override: str) -> str | None:
    if proxy_override:
        return proxy_override
    if mode == "local_clash":
        return "http://127.0.0.1:7897"
    if mode == "panda_direct":
        return None
    proxy = str(secret.get("proxy") or "").strip()
    return proxy or None


def cmd_pure_http(args: argparse.Namespace) -> int:
    from scripts._tmp_spa_image_bench3 import MEDIUM_PROMPT as BENCH_PROMPT  # noqa: WPS433
    from scripts._tmp_spa_image_bench3 import run_once, write_evidence  # noqa: WPS433

    secret_path = Path(args.secret)
    secret = json.loads(secret_path.read_text(encoding="utf-8"))
    if not secret.get("access_token"):
        _log(ok=False, error="missing_access_token")
        return 2

    proxy = _resolve_proxy(args.mode, secret, str(args.proxy or "").strip())
    if args.mode == "panda_webshare" and not proxy:
        _log(ok=False, error="missing_webshare_proxy_in_secret")
        return 2

    date_dir = OUT_ROOT / (args.date or _utc_date()) / "pure_http"
    date_dir.mkdir(parents=True, exist_ok=True)
    prompt = args.prompt or BENCH_PROMPT or MEDIUM_PROMPT
    exit_code = 0

    for run_idx in range(1, int(args.runs) + 1):
        resources_before = sample_resources()
        t0 = time.time()
        raw = run_once(
            secret,
            proxy,
            args.mode,
            prompt,
            protocol=args.protocol,
            image_gen_deadline=float(args.image_gen_deadline),
            sse_diagnostic_read_secs=float(args.sse_diagnostic_read_secs),
            out_dir=date_dir,
        )
        resources_after = sample_resources()
        run_id = f"pure_http_{args.mode}_{int(t0)}_{run_idx}"
        record = normalize_run_record(
            raw,
            path="pure_http",
            run_id=run_id,
            resources_before=resources_before,
            resources_after=resources_after,
        )
        out_path = date_dir / f"result_{args.mode}_{int(t0)}_{run_idx}.json"
        _atomic_write_json(out_path, record)
        write_evidence(date_dir / f"bench3_raw_{args.mode}_{int(t0)}_{run_idx}.json", raw)
        _log(
            phase="pure_http_done",
            run=run_idx,
            path=str(out_path),
            ok=record.get("ok"),
            wall_clock_ms=record["phase_timings_ms"]["wall_clock_ms"],
            sse_stream_ms=record["phase_timings_ms"]["sse_stream_ms"],
            download_ms=record["phase_timings_ms"]["download_ms"],
            upload_bytes=record["traffic_bytes"]["upload_bytes"],
            download_bytes=record["traffic_bytes"]["download_bytes"],
            tokens_per_sec=record.get("tokens_per_sec"),
            elapsed_wall_secs=round(time.time() - t0, 3),
        )
        if not record.get("ok"):
            exit_code = 1
        if run_idx < int(args.runs) and float(args.gap_secs) > 0:
            time.sleep(float(args.gap_secs))

    return exit_code


def cmd_browser_stub(args: argparse.Namespace) -> int:
    date_dir = OUT_ROOT / (args.date or _utc_date()) / "browser"
    date_dir.mkdir(parents=True, exist_ok=True)
    stub = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "path": "browser",
        "ok": False,
        "stub": True,
        "status": "not_implemented",
        "message": (
            "Browser path benchmark wrapper is a stub. Invoke a Camoufox entry script directly, "
            "then normalize its JSON with this suite or re-run compare after manual placement."
        ),
        "entry_points": BROWSER_ENTRY_POINTS,
        "recommended": {
            "local_clash": "python scripts/_tmp_spa_camoufox_image_http_repro.py",
            "panda_socks": "python scripts/_tmp_spa_camoufox_via_panda_socks.py --proxy socks5://127.0.0.1:18080",
        },
        "normalize_hint": "python scripts/image_path_benchmark_suite.py compare --pure-http-dir ... --browser-dir ... --ticket-pool-dir ...",
    }
    out_path = date_dir / f"stub_{int(time.time())}.json"
    _atomic_write_json(out_path, stub)
    _log(phase="browser_stub", path=str(out_path), entry_points=[e["script"] for e in BROWSER_ENTRY_POINTS])
    print(json.dumps(stub, ensure_ascii=False, indent=2))
    return 3


def cmd_ticket_pool_stub(args: argparse.Namespace) -> int:
    """BENCH-002: production /v1/images (ticket path) serial N runs via Panda API."""
    import urllib.error
    import urllib.request

    if getattr(args, "stub_only", False):
        date_dir = OUT_ROOT / (args.date or _utc_date()) / "ticket_pool"
        date_dir.mkdir(parents=True, exist_ok=True)
        helper_url = str(args.helper_url or "http://127.0.0.1:19001").rstrip("/")
        stub = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "path": "ticket_pool",
            "ok": False,
            "stub": True,
            "status": "not_implemented",
            "message": "Use default ticket_pool (production /v1/images) unless --stub-only.",
            "helper_url": helper_url,
            "entry_points": TICKET_POOL_ENTRY_POINTS,
            "docs": "docs/22-ticket-image-pipeline-and-go-spike.md",
        }
        out_path = date_dir / f"stub_{int(time.time())}.json"
        _atomic_write_json(out_path, stub)
        print(json.dumps(stub, ensure_ascii=False, indent=2))
        return 3

    date_dir = OUT_ROOT / (args.date or _utc_date()) / "ticket_pool"
    date_dir.mkdir(parents=True, exist_ok=True)

    base = str(getattr(args, "base_url", None) or "http://127.0.0.1:8012").rstrip("/")
    email = str(getattr(args, "email", None) or "qaflowakjewai6ps@proton.me").strip()
    auth = str(getattr(args, "auth_key", None) or "").strip()
    if not auth:
        for candidate in (ROOT / "config.json", Path("/root/gptimage/config.json")):
            if candidate.is_file():
                cfg = json.loads(candidate.read_text(encoding="utf-8"))
                auth = str(cfg.get("auth-key") or cfg.get("auth_key") or "").strip()
                if auth:
                    break
    if not auth:
        _log(ok=False, error="missing_auth_key")
        return 2

    prompt = getattr(args, "prompt", None) or MEDIUM_PROMPT
    exit_code = 0
    runs = max(1, int(getattr(args, "runs", 1) or 1))
    gap = max(0.0, float(getattr(args, "gap_secs", 0) or 0))
    timeout_secs = float(getattr(args, "timeout_secs", 540) or 540)

    for run_idx in range(1, runs + 1):
        resources_before = sample_resources()
        t0 = time.time()
        body = json.dumps(
            {
                "model": "gpt-image-2",
                "prompt": prompt,
                "n": 1,
                "response_format": "b64_json",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/v1/images/generations",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {auth}",
                "Content-Type": "application/json",
                "X-Preferred-Account-Email": email,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
                http_code = resp.status
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
            except Exception:
                payload = {"error": str(exc)}
            http_code = exc.code
        except Exception as exc:
            payload = {"error": str(exc)}
            http_code = 0

        elapsed_ms = int((time.time() - t0) * 1000)
        images = payload.get("data") if isinstance(payload, dict) else None
        b64_len = 0
        if isinstance(images, list) and images and isinstance(images[0], dict):
            b64_len = len(str(images[0].get("b64_json") or ""))
        ok = http_code == 200 and b64_len > 1000

        phase_timings: dict[str, Any] = {"wall_clock_ms": elapsed_ms}
        usage: dict[str, Any] = {}
        tokens_per_sec = None
        task_id = None
        try:
            logs_path = ROOT / "data" / "logs.jsonl"
            if logs_path.is_file():
                for line in reversed(logs_path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]):
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    if item.get("type") != "call":
                        continue
                    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
                    phases = detail.get("phase_timings_ms")
                    if not (isinstance(phases, dict) and phases):
                        continue
                    wall = int(detail.get("total_wall_ms") or detail.get("duration_ms") or 0)
                    if wall and abs(wall - elapsed_ms) > 20000:
                        continue
                    phase_timings = dict(phases)
                    if "wall_clock_ms" not in phase_timings:
                        phase_timings["wall_clock_ms"] = wall or elapsed_ms
                    usage = {
                        "prompt_tokens": detail.get("prompt_tokens"),
                        "completion_tokens": detail.get("completion_tokens"),
                    }
                    tokens_per_sec = detail.get("tokens_per_sec")
                    task_id = detail.get("task_id")
                    break
        except Exception:
            pass

        err = None
        if not ok:
            if isinstance(payload, dict):
                err = payload.get("detail")
                if err is None and isinstance(payload.get("error"), dict):
                    err = payload["error"].get("message")
                elif err is None:
                    err = payload.get("error")
            else:
                err = str(payload)

        raw = {
            "ok": ok,
            "http_code": http_code,
            "elapsed_ms": elapsed_ms,
            "account_email": email,
            "prompt_text": prompt,
            "task_id": task_id,
            "b64_len": b64_len,
            "phase_timings_ms": phase_timings,
            "usage": usage,
            "tokens_per_sec": tokens_per_sec,
            "error": err,
            "path": "ticket_pool",
            "source": "panda_v1_images",
        }
        resources_after = sample_resources()
        run_id = f"ticket_pool_v1_{int(t0)}_{run_idx}"
        record = normalize_run_record(
            raw,
            path="ticket_pool",
            run_id=run_id,
            resources_before=resources_before,
            resources_after=resources_after,
        )
        out_path = date_dir / f"result_v1_{int(t0)}_{run_idx}.json"
        _atomic_write_json(out_path, record)
        _log(
            phase="ticket_pool_done",
            run=run_idx,
            path=str(out_path),
            ok=record.get("ok"),
            wall_clock_ms=record["phase_timings_ms"]["wall_clock_ms"],
            sse_stream_ms=record["phase_timings_ms"]["sse_stream_ms"],
            tokens_per_sec=record.get("tokens_per_sec"),
            elapsed_wall_secs=round(time.time() - t0, 3),
        )
        if not ok:
            exit_code = 1
        if run_idx < runs and gap > 0:
            time.sleep(gap)

    return exit_code


def cmd_compare(args: argparse.Namespace) -> int:
    if args.parent_dir:
        parent = Path(args.parent_dir)
        pure_http_dir = parent / "pure_http"
        browser_dir = parent / "browser"
        ticket_pool_dir = parent / "ticket_pool"
        out_path = parent / "compare-summary.json"
    else:
        pure_http_dir = Path(args.pure_http_dir)
        browser_dir = Path(args.browser_dir)
        ticket_pool_dir = Path(args.ticket_pool_dir)
        out_path = Path(args.out) if args.out else None

    summary = compare_paths(
        pure_http_dir,
        browser_dir,
        ticket_pool_dir,
        out_path=out_path,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Image path benchmark suite (pure HTTP / browser / ticket pool)")
    sub = ap.add_subparsers(dest="command", required=True)

    ph = sub.add_parser("pure_http", help="Run pure HTTP bench via scripts/_tmp_spa_image_bench3.run_once")
    ph.add_argument("--mode", required=True, choices=["local_clash", "panda_direct", "panda_webshare"])
    ph.add_argument("--secret", default=str(SECRET_DEFAULT))
    ph.add_argument("--proxy", default="", help="Override proxy URL")
    ph.add_argument("--prompt", default="")
    ph.add_argument("--protocol", default="picture_v2", choices=["picture_v2", "spa_tool"])
    ph.add_argument("--image-gen-deadline", type=float, default=25.0)
    ph.add_argument("--sse-diagnostic-read-secs", type=float, default=90.0)
    ph.add_argument("--runs", type=int, default=1, help="Serial runs (default 1)")
    ph.add_argument("--gap-secs", type=float, default=0.0, help="Sleep between serial runs")
    ph.add_argument("--date", default="", help="Evidence date folder YYYYMMDD (default UTC today)")
    ph.set_defaults(handler=cmd_pure_http)

    br = sub.add_parser("browser", help="Browser path stub (documents Camoufox entry points)")
    br.add_argument("--date", default="")
    br.set_defaults(handler=cmd_browser_stub)

    tp = sub.add_parser("ticket_pool", help="Production /v1/images ticket path (serial N)")
    tp.add_argument("--helper-url", default="http://127.0.0.1:19001")
    tp.add_argument("--base-url", default="http://127.0.0.1:8012")
    tp.add_argument("--email", default="qaflowakjewai6ps@proton.me")
    tp.add_argument("--auth-key", default="")
    tp.add_argument("--prompt", default="")
    tp.add_argument("--runs", type=int, default=5)
    tp.add_argument("--gap-secs", type=float, default=30.0)
    tp.add_argument("--timeout-secs", type=float, default=540.0)
    tp.add_argument("--date", default="")
    tp.add_argument("--stub-only", action="store_true", help="Write stub JSON only (legacy)")
    tp.set_defaults(handler=cmd_ticket_pool_stub)

    cp = sub.add_parser("compare", help="Compare three evidence directories → compare-summary.json")
    cp.add_argument("--parent-dir", default="", help="Parent containing pure_http/ browser/ ticket_pool/")
    cp.add_argument("--pure-http-dir", default="")
    cp.add_argument("--browser-dir", default="")
    cp.add_argument("--ticket-pool-dir", default="")
    cp.add_argument("--out", default="", help="Output compare-summary.json path")
    cp.set_defaults(handler=cmd_compare)

    return ap


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "compare" and not args.parent_dir:
        if not (args.pure_http_dir and args.browser_dir and args.ticket_pool_dir):
            parser.error("compare requires --parent-dir OR all of --pure-http-dir --browser-dir --ticket-pool-dir")
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
