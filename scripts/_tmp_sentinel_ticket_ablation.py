#!/usr/bin/env python3
"""Sentinel ticket ablation: delay / reuse / cross-session / cross-IP / TTL / concurrent.

Does NOT download images — probes whether finalize token is accepted on SSE start.
Run on Panda with account sticky Webshare (mode panda_webshare).

Phases:
  baseline     — finalize → immediate SSE (control)
  delay        — sleep N seconds between finalize and SSE
  reuse        — one ticket, two SSE POSTs (same session)
  cross_session— ticket on session A, SSE on session B (same IP)
  cross_ip     — finalize IP A, SSE IP B (same fp/token)
  cross_both   — new session + different IP
  ttl_sweep    — delay grid to estimate survival window
  concurrent   — parallel finalize + parallel use (same account)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_ROOT_OVERRIDE = str(os.environ.get("GPTIMAGE_ROOT") or "").strip()
if _ROOT_OVERRIDE and Path(_ROOT_OVERRIDE).is_dir():
    ROOT = Path(_ROOT_OVERRIDE).resolve()
sys.path.insert(0, str(ROOT))

from curl_cffi import requests  # noqa: E402

# Reuse bench3 primitives (frozen billing path aligned).
from scripts._tmp_spa_image_bench3 import (  # noqa: E402
    TrafficMeter,
    _Req,
    _fp_from_secret,
    _hdr,
    _make_image_prepare_body,
    _make_image_start_body,
    _probe_egress,
    _requirements,
    _sanitize_error,
    write_evidence,
)
from services.protocol.chatgpt_web_request import build_image_start_headers  # noqa: E402
from utils.helper import ensure_ok  # noqa: E402

try:
    from scripts.spa_bench_sse import consume_image_sse  # noqa: E402
except ImportError:
    from spa_bench_sse import consume_image_sse  # type: ignore  # noqa: E402

BASE = "https://chatgpt.com"
DEFAULT_EMAIL = "qaflowakjewai6ps@proton.me"
DEFAULT_SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
DEFAULT_OUT = ROOT / "data" / "runlogs" / "spa_repro" / "sentinel-ticket-ablation-20260723"
SHORT_PROMPT = "a simple red circle on white background, no text"
TZ, OFFSET = "Asia/Tokyo", -540
# Probe mode: only need conversation_id (ticket accepted), not image_gen.
SSE_GATE_SECS = 10.0
SSE_READ_SECS = 15.0
CASE_GAP_SECS = 1.0

# Batched runs — one batch ≈ 2–8 min. Merge with --merge across batches.
BATCH_PLAN: dict[str, dict[str, Any]] = {
    "batch1": {
        "label": "control_reuse_session",
        "phases": ["baseline", "reuse", "cross_session"],
        "note": "基线 + 同票复用 + 跨 session 同 IP",
    },
    "batch2": {
        "label": "cross_egress",
        "phases": ["cross_ip", "cross_both"],
        "note": "跨 IP / 跨 session+IP",
    },
    "batch3": {
        "label": "delay_30s",
        "phases": ["delay"],
        "delay_secs": 30.0,
        "note": "延迟 30s 用票",
    },
    "batch4-60": {
        "label": "delay_60s",
        "phases": ["delay"],
        "delay_secs": 60.0,
        "note": "TTL 探针 60s",
    },
    "batch4-120": {
        "label": "delay_120s",
        "phases": ["delay"],
        "delay_secs": 120.0,
        "note": "TTL 探针 120s",
    },
    "batch4-300": {
        "label": "delay_300s",
        "phases": ["delay"],
        "delay_secs": 300.0,
        "note": "TTL 探针 300s",
    },
    "batch5": {
        "label": "concurrent",
        "phases": ["concurrent"],
        "concurrent_workers": 2,
        "note": "同账号并行开票+用票（workers=2）",
    },
}


def _log(**kw: Any) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def _session(fp: dict, proxy: str | None) -> requests.Session:
    kw: dict[str, Any] = {"impersonate": fp["impersonate"], "verify": False, "timeout": 120}
    if proxy:
        kw["proxy"] = proxy
    return requests.Session(**kw)


def _token_jwt_exp(token: str) -> int | None:
    """Best-effort decode JWT exp from sentinel token (opaque tokens return None)."""
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        import base64

        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def open_ticket(sess: requests.Session, fp: dict, access_token: str) -> tuple[_Req, dict[str, Any]]:
    meter = TrafficMeter()
    req, cf = _requirements(sess, meter, fp, access_token)
    egress = _probe_egress(sess, meter, None)
    meta = {
        "requirements_ms": meter.snapshot(),
        "egress_ip": (egress or {}).get("ip"),
        "token_len": len(req.token),
        "proof_len": len(req.proof_token),
        "turnstile_len": len(req.turnstile_token),
        "jwt_exp_unix": _token_jwt_exp(req.token),
        "cf_observability": cf,
    }
    return req, meta


def use_ticket_sse(
    sess: requests.Session,
    fp: dict,
    access_token: str,
    req: _Req,
    prompt: str,
    *,
    label: str = "",
    gate_secs: float = SSE_GATE_SECS,
    read_secs: float = SSE_READ_SECS,
) -> dict[str, Any]:
    """POST /f/conversation with frozen sentinel headers; light SSE consume."""
    meter = TrafficMeter()
    t0 = time.time()
    out: dict[str, Any] = {
        "label": label,
        "ok": False,
        "http_status": None,
        "conversation_id": "",
        "has_image_gen": False,
        "sse_chunks": 0,
        "error": None,
        "error_class": None,
        "elapsed_ms": 0,
        "egress_ip": None,
    }
    try:
        egress = _probe_egress(sess, meter, None)
        out["egress_ip"] = (egress or {}).get("ip")

        prep_path = "/backend-api/f/conversation/prepare"
        prep_body = _make_image_prepare_body(prompt, "auto", TZ, OFFSET, spa_tool_path=True)
        prep = sess.post(
            BASE + prep_path,
            headers=_hdr(fp, prep_path, access_token),
            json=prep_body,
            timeout=60,
        )
        ensure_ok(prep, prep_path)

        path = "/backend-api/f/conversation"
        body = _make_image_start_body(prompt, "auto", TZ, OFFSET, spa_tool_path=True)
        headers = _hdr(
            fp,
            path,
            access_token,
            build_image_start_headers(req, "", spa_tool_path=True),
        )
        headers["Accept"] = "text/event-stream"
        resp = sess.post(BASE + path, headers=headers, json=body, timeout=300, stream=True)
        out["http_status"] = int(getattr(resp, "status_code", 0) or 0)
        try:
            ensure_ok(resp, path)
        except Exception as exc:
            out["error"] = _sanitize_error(exc, 400)
            out["error_class"] = "http_reject"
            try:
                out["body_snip"] = (getattr(resp, "text", "") or "")[:500]
            except Exception:
                pass
            return out

        t_sse = time.time()
        sse = consume_image_sse(
            resp.iter_lines(),
            t0=t_sse,
            gate_secs=float(gate_secs),
            total_read_secs=float(read_secs),
        )
        try:
            resp.close()
        except Exception:
            pass
        out["conversation_id"] = sse.cid or ""
        out["has_image_gen"] = bool(sse.has_image_gen_within_gate)
        out["sse_chunks"] = int(sse.chunks or 0)
        out["ok"] = out["http_status"] == 200 and bool(out["conversation_id"])
        if not out["has_image_gen"] and out["ok"]:
            out["error_class"] = "sse_no_image_gen"
    except Exception as exc:
        out["error"] = _sanitize_error(exc, 400)
        out["error_class"] = out["error_class"] or "exception"
    finally:
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
    return out


def load_secret(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_alt_proxy_from_db(
    accounts_db: Path,
    *,
    exclude_proxy: str = "",
    exclude_email: str = "",
) -> dict[str, Any] | None:
    con = sqlite3.connect(str(accounts_db))
    con.row_factory = sqlite3.Row
    exclude_proxy = str(exclude_proxy or "").strip()
    exclude_email = str(exclude_email or "").strip().lower()
    for row in con.execute("select access_token, data from accounts"):
        data = json.loads(row["data"] or "{}")
        email = str(data.get("email") or "").strip().lower()
        proxy = str(data.get("proxy") or "").strip()
        if not proxy or email == exclude_email:
            continue
        if exclude_proxy and proxy == exclude_proxy:
            continue
        ip = str(data.get("proxy_egress_ip") or "").strip()
        return {
            "email": email,
            "access_token": row["access_token"],
            "proxy": proxy,
            "proxy_egress_ip": ip,
            "fp": data.get("fp") if isinstance(data.get("fp"), dict) else {},
        }
    return None


def case_baseline(secret: dict, proxy: str) -> dict[str, Any]:
    fp = _fp_from_secret(secret)
    token = str(secret.get("access_token") or "")
    sess = _session(fp, proxy)
    t0 = time.time()
    req, meta = open_ticket(sess, fp, token)
    use = use_ticket_sse(sess, fp, token, req, SHORT_PROMPT, label="baseline_immediate")
    return {
        "case": "baseline_immediate",
        "open": meta,
        "use": use,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def case_delay(secret: dict, proxy: str, delay_secs: float) -> dict[str, Any]:
    fp = _fp_from_secret(secret)
    token = str(secret.get("access_token") or "")
    sess = _session(fp, proxy)
    t0 = time.time()
    req, meta = open_ticket(sess, fp, token)
    finalize_at = time.time()
    if delay_secs > 0:
        time.sleep(delay_secs)
    use = use_ticket_sse(
        sess,
        fp,
        token,
        req,
        SHORT_PROMPT,
        label=f"delay_{int(delay_secs)}s",
    )
    return {
        "case": "delay",
        "delay_secs": delay_secs,
        "open": meta,
        "use": use,
        "wall_between_finalize_and_sse_secs": round(time.time() - finalize_at, 3),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def case_reuse(secret: dict, proxy: str) -> dict[str, Any]:
    fp = _fp_from_secret(secret)
    token = str(secret.get("access_token") or "")
    sess = _session(fp, proxy)
    req, meta = open_ticket(sess, fp, token)
    use1 = use_ticket_sse(sess, fp, token, req, SHORT_PROMPT, label="reuse_first")
    use2 = use_ticket_sse(sess, fp, token, req, SHORT_PROMPT, label="reuse_second_same_ticket")
    return {
        "case": "reuse_same_session",
        "open": meta,
        "first": use1,
        "second": use2,
        "reuse_accepted": bool(use2.get("ok") or use2.get("http_status") == 200),
    }


def case_cross_session(secret: dict, proxy: str) -> dict[str, Any]:
    fp = _fp_from_secret(secret)
    token = str(secret.get("access_token") or "")
    sess_a = _session(fp, proxy)
    req, meta = open_ticket(sess_a, fp, token)
    sess_b = _session(fp, proxy)  # new TCP/TLS session, same proxy + fp
    use = use_ticket_sse(sess_b, fp, token, req, SHORT_PROMPT, label="cross_session_same_ip")
    return {
        "case": "cross_session_same_ip",
        "open": meta,
        "use": use,
        "open_egress": meta.get("egress_ip"),
        "use_egress": use.get("egress_ip"),
    }


def case_cross_ip(secret: dict, proxy_a: str, proxy_b: str, alt: dict | None) -> dict[str, Any]:
    fp = _fp_from_secret(secret)
    token = str(secret.get("access_token") or "")
    sess_a = _session(fp, proxy_a)
    req, meta = open_ticket(sess_a, fp, token)
    sess_b = _session(fp, proxy_b)
    use = use_ticket_sse(sess_b, fp, token, req, SHORT_PROMPT, label="cross_ip")
    return {
        "case": "cross_ip",
        "open_proxy_hash": hashlib.sha256(proxy_a.encode()).hexdigest()[:12],
        "use_proxy_hash": hashlib.sha256(proxy_b.encode()).hexdigest()[:12],
        "alt_account_email": (alt or {}).get("email"),
        "open": meta,
        "use": use,
        "open_egress": meta.get("egress_ip"),
        "use_egress": use.get("egress_ip"),
        "ip_changed": meta.get("egress_ip") and use.get("egress_ip") and meta.get("egress_ip") != use.get("egress_ip"),
    }


def case_cross_both(secret: dict, proxy_a: str, proxy_b: str, alt: dict | None) -> dict[str, Any]:
    fp = _fp_from_secret(secret)
    token = str(secret.get("access_token") or "")
    sess_a = _session(fp, proxy_a)
    req, meta = open_ticket(sess_a, fp, token)
    # new session-id simulates fresh browser tab
    fp_b = dict(fp)
    fp_b["oai-session-id"] = fp_b.get("oai-session-id", "") + "-cross"
    sess_b = _session(fp_b, proxy_b)
    use = use_ticket_sse(sess_b, fp, token, req, SHORT_PROMPT, label="cross_session_cross_ip")
    return {
        "case": "cross_session_cross_ip",
        "alt_account_email": (alt or {}).get("email"),
        "open": meta,
        "use": use,
        "open_egress": meta.get("egress_ip"),
        "use_egress": use.get("egress_ip"),
    }


def _parallel_open(idx: int, fp: dict, proxy: str, token: str) -> dict[str, Any]:
    sess = _session(fp, proxy)
    t0 = time.time()
    try:
        req, meta = open_ticket(sess, fp, token)
        return {
            "worker": idx,
            "ok": True,
            "token_len": len(req.token),
            "egress_ip": meta.get("egress_ip"),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except Exception as exc:
        return {
            "worker": idx,
            "ok": False,
            "error": _sanitize_error(exc, 240),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }


def case_concurrent(secret: dict, proxy: str, workers: int) -> dict[str, Any]:
    fp = _fp_from_secret(secret)
    token = str(secret.get("access_token") or "")
    workers = max(1, min(int(workers), 5))
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        opens = list(pool.map(lambda i: _parallel_open(i, fp, proxy, token), range(workers)))
    # parallel use: each worker opens own ticket then SSE (production pattern)
    def _open_and_use(i: int) -> dict[str, Any]:
        sess = _session(fp, proxy)
        try:
            req, meta = open_ticket(sess, fp, token)
            use = use_ticket_sse(sess, fp, token, req, SHORT_PROMPT, label=f"concurrent_use_{i}")
            return {"worker": i, "open": meta, "use": use, "ok": bool(use.get("ok"))}
        except Exception as exc:
            return {"worker": i, "ok": False, "error": _sanitize_error(exc, 240)}

    time.sleep(1.0)  # brief gap after parallel finalize storm
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        uses = list(pool.map(_open_and_use, range(workers)))
    return {
        "case": "concurrent_same_account",
        "workers": workers,
        "parallel_finalize": opens,
        "parallel_finalize_ok": sum(1 for x in opens if x.get("ok")),
        "parallel_open_then_use": uses,
        "parallel_use_ok": sum(1 for x in uses if x.get("ok")),
        "browser_pool_hypothesis": (
            "If parallel_open_then_use succeeds at workers=N without browsers, "
            "HTTP per-call finalize scales concurrency; browser ticket pool not required for curl_cffi path."
        ),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def run_ttl_sweep(secret: dict, proxy: str, delays: list[float]) -> dict[str, Any]:
    results = []
    last_ok_delay = 0.0
    first_fail_delay: float | None = None
    for d in delays:
        _log(phase="ttl_sweep_step", delay_secs=d)
        row = case_delay(secret, proxy, d)
        ok = bool((row.get("use") or {}).get("ok"))
        results.append(
            {
                "delay_secs": d,
                "ok": ok,
                "http_status": (row.get("use") or {}).get("http_status"),
                "error_class": (row.get("use") or {}).get("error_class"),
                "has_image_gen": (row.get("use") or {}).get("has_image_gen"),
            }
        )
        if ok:
            last_ok_delay = d
        elif first_fail_delay is None:
            first_fail_delay = d
        # avoid hammering same account without pause
        time.sleep(2.0)
    return {
        "case": "ttl_sweep",
        "delays_tested": delays,
        "last_ok_delay_secs": last_ok_delay,
        "first_fail_delay_secs": first_fail_delay,
        "estimated_ttl_lower_bound_secs": last_ok_delay,
        "estimated_ttl_upper_bound_secs": first_fail_delay,
        "rows": results,
    }


def merge_report(existing: dict[str, Any] | None, batch: str, new_cases: list[dict[str, Any]], meta: dict) -> dict[str, Any]:
    prior_cases: list[dict[str, Any]] = []
    batches_run: list[str] = []
    if existing:
        prior_cases = list(existing.get("cases") or [])
        batches_run = list(existing.get("batches_run") or [])
    # Replace same case type from prior if re-run
    new_case_keys = {c.get("case") for c in new_cases}
    kept = [c for c in prior_cases if c.get("case") not in new_case_keys]
    all_cases = kept + new_cases
    if batch and batch not in batches_run:
        batches_run.append(batch)
    report = {
        "schema": "sentinel-ticket-ablation/v1",
        "meta": {**(existing.get("meta") if existing else {}), **meta},
        "batches_run": batches_run,
        "batch_plan": BATCH_PLAN,
        "summary": build_summary(all_cases),
        "cases": all_cases,
    }
    return report


def load_existing_report(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def resolve_batch(batch: str) -> tuple[set[str], dict[str, Any]]:
    plan = BATCH_PLAN.get(batch)
    if not plan:
        raise SystemExit(f"unknown batch {batch!r}; choose from: {', '.join(BATCH_PLAN)}")
    phases = {p.strip().lower() for p in plan.get("phases", [])}
    return phases, plan


def estimate_ttl_from_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    delays: list[tuple[float, bool]] = []
    for c in cases:
        if c.get("case") != "delay":
            continue
        d = float(c.get("delay_secs") or 0)
        ok = bool((c.get("use") or {}).get("ok"))
        delays.append((d, ok))
    if not delays:
        return {}
    delays.sort(key=lambda x: x[0])
    last_ok = 0.0
    first_fail: float | None = None
    for d, ok in delays:
        if ok:
            last_ok = d
        elif first_fail is None:
            first_fail = d
    baseline_ok = any(
        (c.get("use") or {}).get("ok")
        for c in cases
        if c.get("case") == "baseline_immediate"
    )
    if baseline_ok and last_ok == 0.0 and not any(d == 0 for d, _ in delays):
        last_ok = 0.0
    return {
        "delay_rows": [{"delay_secs": d, "ok": ok} for d, ok in delays],
        "last_ok_delay_secs": last_ok,
        "first_fail_delay_secs": first_fail,
        "estimated_ttl_lower_bound_secs": last_ok,
        "estimated_ttl_upper_bound_secs": first_fail,
    }


def build_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    ttl = estimate_ttl_from_cases(cases)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases),
        "ttl_estimate": ttl,
        "findings": {
            "baseline": next((c for c in cases if c.get("case") == "baseline_immediate"), {}),
            "cross_request_reuse": next(
                (c for c in cases if c.get("case") == "reuse_same_session"),
                {},
            ),
            "cross_session": next(
                (c for c in cases if c.get("case") == "cross_session_same_ip"),
                {},
            ),
            "cross_ip": next((c for c in cases if c.get("case") == "cross_ip"), {}),
            "cross_both": next(
                (c for c in cases if c.get("case") == "cross_session_cross_ip"),
                {},
            ),
            "concurrent": next(
                (c for c in cases if c.get("case") == "concurrent_same_account"),
                {},
            ),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Sentinel ticket ablation suite")
    ap.add_argument("--secret", default=str(DEFAULT_SECRET))
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--accounts-db", default=str(ROOT / "data" / "accounts.db"))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument(
        "--batch",
        default="",
        help=f"batched run: {','.join(BATCH_PLAN)} (overrides --phase)",
    )
    ap.add_argument(
        "--merge",
        action="store_true",
        help="merge into existing ablation_report.json in --out-dir",
    )
    ap.add_argument(
        "--phase",
        default="",
        help="legacy: baseline,delay,reuse,... (prefer --batch)",
    )
    ap.add_argument("--delay-secs", type=float, default=30.0)
    ap.add_argument(
        "--ttl-delays",
        default="0,5,15,30,60,120,180,300",
        help="comma-separated seconds for ttl_sweep",
    )
    ap.add_argument("--alt-proxy", default="", help="override alt proxy URL for cross_ip")
    ap.add_argument("--concurrent-workers", type=int, default=3)
    args = ap.parse_args()

    secret = load_secret(Path(args.secret))
    proxy = str(secret.get("proxy") or "").strip()
    if not proxy:
        raise SystemExit("secret missing proxy")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    alt: dict[str, Any] | None = None
    proxy_b = str(args.alt_proxy or "").strip()
    if not proxy_b:
        alt = load_alt_proxy_from_db(
            Path(args.accounts_db),
            exclude_proxy=proxy,
            exclude_email=str(secret.get("email") or args.email),
        )
        if alt:
            proxy_b = str(alt.get("proxy") or "")

    batch_name = str(args.batch or "").strip()
    batch_plan: dict[str, Any] = {}
    if batch_name:
        phases, batch_plan = resolve_batch(batch_name)
    elif str(args.phase or "").strip():
        phases = {p.strip().lower() for p in str(args.phase).split(",") if p.strip()}
        if "all" in phases:
            phases = set()
            for plan in BATCH_PLAN.values():
                phases.update(plan.get("phases", []))
    else:
        raise SystemExit("specify --batch batch1|batch2|... or --phase")

    delay_secs = float(batch_plan.get("delay_secs") or args.delay_secs)
    concurrent_workers = int(batch_plan.get("concurrent_workers") or args.concurrent_workers)

    cases: list[dict[str, Any]] = []
    meta = {
        "email": secret.get("email") or args.email,
        "proxy_egress_hint": secret.get("proxy_egress_ip"),
        "batch_id": batch_name or None,
        "batch_label": batch_plan.get("label"),
        "phases": sorted(phases),
        "alt_proxy_available": bool(proxy_b),
        "alt_email": (alt or {}).get("email"),
        "probe_mode": {"gate_secs": SSE_GATE_SECS, "read_secs": SSE_READ_SECS},
    }
    _log(phase="ablation_batch_start", **meta)

    if "baseline" in phases:
        cases.append(case_baseline(secret, proxy))
        time.sleep(CASE_GAP_SECS)

    if "delay" in phases:
        cases.append(case_delay(secret, proxy, delay_secs))
        time.sleep(CASE_GAP_SECS)

    if "reuse" in phases:
        cases.append(case_reuse(secret, proxy))
        time.sleep(CASE_GAP_SECS)

    if "cross_session" in phases:
        cases.append(case_cross_session(secret, proxy))
        time.sleep(CASE_GAP_SECS)

    if "cross_ip" in phases:
        if not proxy_b:
            cases.append({"case": "cross_ip", "skipped": True, "reason": "no_alt_proxy"})
        else:
            cases.append(case_cross_ip(secret, proxy, proxy_b, alt))
        time.sleep(CASE_GAP_SECS)

    if "cross_both" in phases:
        if not proxy_b:
            cases.append({"case": "cross_session_cross_ip", "skipped": True, "reason": "no_alt_proxy"})
        else:
            cases.append(case_cross_both(secret, proxy, proxy_b, alt))
        time.sleep(CASE_GAP_SECS)

    if "ttl_sweep" in phases:
        delays = [float(x.strip()) for x in str(args.ttl_delays).split(",") if x.strip()]
        cases.append(run_ttl_sweep(secret, proxy, delays))

    if "concurrent" in phases:
        cases.append(case_concurrent(secret, proxy, concurrent_workers))

    out_path = out_dir / "ablation_report.json"
    existing = load_existing_report(out_path) if args.merge else None
    report = merge_report(existing, batch_name, cases, meta)
    write_evidence(out_path, report)
    _log(phase="ablation_batch_done", batch_id=batch_name, out=str(out_path), cases=len(cases))
    print(json.dumps({"batch": batch_name, "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
