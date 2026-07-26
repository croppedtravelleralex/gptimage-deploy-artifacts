#!/usr/bin/env python3
"""Sentinel ticket validation — longevity / reuse-gap / cross-IP matrix.

Run ONE phase at a time; orchestrator stops on first failure.
Full image path (SSE + poll + download) with TrafficMeter bandwidth.

Examples (Panda helper venv):
  python scripts/sentinel_ticket_validation_suite.py reuse-gap --gaps 60,120,300
  python scripts/sentinel_ticket_validation_suite.py cross-serial --round 1 --rounds-total 5
  python scripts/sentinel_ticket_validation_suite.py cross-concurrent --round 1 --workers 10
  python scripts/sentinel_ticket_validation_suite.py longevity --tier 10m
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_ROOT_OVERRIDE = str(os.environ.get("GPTIMAGE_ROOT") or "").strip()
if _ROOT_OVERRIDE and Path(_ROOT_OVERRIDE).is_dir():
    ROOT = Path(_ROOT_OVERRIDE).resolve()
sys.path.insert(0, str(ROOT))

from curl_cffi import requests  # noqa: E402

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
    classify_image_sse_failure,
    consume_image_sse,
    persist_image,
    write_evidence,
)
from services.openai_backend_api import OpenAIBackendAPI  # noqa: E402
from services.protocol.chatgpt_web_request import (  # noqa: E402
    build_image_start_body,
    build_image_start_headers,
)
from utils.helper import ensure_ok  # noqa: E402

BASE = "https://chatgpt.com"
DEFAULT_EMAIL = "qaflowxho1z6hynk@proton.me"
OUT_ROOT = ROOT / "data" / "runlogs" / "spa_repro" / "sentinel-ticket-validation-20260723-phase2"
PROMPT_SIMPLE = "a simple red circle on white background, no text"
PROMPT_LARGE = (
    "A sprawling futuristic megacity at dusk viewed from a rooftop garden: layered highways with "
    "glowing maglev lanes, holographic billboards in kanji and english, dense mid-rise apartments "
    "with laundry lines, a distant orbital elevator catching sunset, cherry trees in planters, "
    "soft volumetric fog, cinematic wide composition, ultra detailed environment concept art, "
    "no text, no watermark, no logos"
)
PROMPT_IMAGE_EDIT = (
    "Convert this reference into a minimalist ink wash painting: simplify shapes, muted grayscale "
    "with one accent red circle, preserve overall layout, no text"
)
REF_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFUlEQVR42mNk+M9Qz0AEYBxVSF+F"
    "ABJADveWkH6oAAAAAElFTkSuQmCC"
)
TZ, OFFSET = "Asia/Tokyo", -540
IMAGE_DEADLINE = 90.0
SSE_READ = 120.0

TIER_SECS: dict[str, int] = {
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "60m": 3600,
    "3h": 10800,
    "6h": 21600,
    "12h": 43200,
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(path: Path, event: str, **kw: Any) -> None:
    row = {"ts": _utc(), "event": event, **kw}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False), flush=True)


def _session(fp: dict, proxy: str | None) -> requests.Session:
    kw: dict[str, Any] = {"impersonate": fp["impersonate"], "verify": False, "timeout": 120}
    if proxy:
        kw["proxy"] = proxy
    return requests.Session(**kw)


def _secret_from_row(access_token: str, data: dict) -> dict[str, Any]:
    return {
        "email": data.get("email"),
        "access_token": access_token,
        "proxy": data.get("proxy"),
        "fp": data.get("fp") if isinstance(data.get("fp"), dict) else {},
        "proxy_egress_ip": data.get("proxy_egress_ip"),
        "quota": data.get("quota"),
        "status": data.get("status"),
        "cf_hint": _cf_hint(data),
    }


def _cf_hint(data: dict) -> str:
    err = " ".join(
        str(data.get(k) or "")
        for k in (
            "last_refresh_error",
            "last_token_refresh_error",
            "last_quota_refresh_error",
            "panda_verify_last_error",
        )
    ).lower()
    if "cf403" in err or "cloudflare" in err or "cf_edge" in err:
        return "cf_history"
    return "clean"


def load_secret_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_accounts_db(
    db_path: Path,
    *,
    min_quota: int = 1,
    limit: int = 20,
    prefer_cf: bool = True,
) -> list[dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    rows: list[dict[str, Any]] = []
    for access_token, raw in con.execute("select access_token, data from accounts"):
        data = json.loads(raw or "{}")
        q = int(data.get("quota") or 0)
        if q < min_quota and not data.get("unlimited"):
            continue
        if not str(data.get("proxy") or "").strip():
            continue
        if not str(access_token or "").strip():
            continue
        sec = _secret_from_row(access_token, data)
        sec["_quota"] = q
        rows.append(sec)
    cf = [r for r in rows if r.get("cf_hint") == "cf_history"]
    clean = [r for r in rows if r.get("cf_hint") != "cf_history"]
    clean.sort(key=lambda x: int(x.get("_quota") or 0), reverse=True)
    cf.sort(key=lambda x: int(x.get("_quota") or 0), reverse=True)
    if prefer_cf and cf:
        # mix: take some cf accounts first then fill with clean
        picked = cf[: max(2, limit // 3)] + clean
    else:
        picked = clean + cf
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in picked:
        email = str(item.get("email") or "").lower()
        if not email or email in seen:
            continue
        seen.add(email)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def load_accounts_by_emails(db_path: Path, emails: list[str]) -> list[dict[str, Any]]:
    wanted = {e.strip().lower() for e in emails if e.strip()}
    con = sqlite3.connect(str(db_path))
    out: list[dict[str, Any]] = []
    for access_token, raw in con.execute("select access_token, data from accounts"):
        data = json.loads(raw or "{}")
        email = str(data.get("email") or "").lower()
        if email not in wanted:
            continue
        if not str(data.get("proxy") or "").strip():
            continue
        if not str(access_token or "").strip():
            continue
        sec = _secret_from_row(access_token, data)
        sec["_quota"] = int(data.get("quota") or 0)
        out.append(sec)
    return out


def pick_alt_proxy(accounts: list[dict], primary: dict, *, avoid: set[str] | None = None) -> tuple[str, dict | None]:
    avoid = avoid or set()
    pxy = str(primary.get("proxy") or "")
    for acc in accounts:
        alt = str(acc.get("proxy") or "")
        if alt and alt != pxy and alt not in avoid:
            return alt, acc
    return "", None


def pick_fresh_accounts(
    accounts: list[dict[str, Any]],
    *,
    n: int,
    exclude: set[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for acc in accounts:
        email = str(acc.get("email") or "").lower()
        if not email or email in exclude:
            continue
        out.append(acc)
        if len(out) >= n:
            break
    return out


def prompt_for_mode(mode: str) -> tuple[str, bool, str]:
    """Return (prompt, spa_tool_path, label_suffix)."""
    m = str(mode or "text_simple").strip().lower()
    if m == "text_large":
        return PROMPT_LARGE, True, "text_large"
    if m in ("image_edit", "img2img"):
        return PROMPT_IMAGE_EDIT, False, "image_edit"
    return PROMPT_SIMPLE, True, "text_simple"


def mark_image_fail(access_token: str, *, label: str, error: str, log_path: Path) -> None:
    from services.account_service import account_service

    account_service.mark_image_result(access_token, False, error=error)
    _log(log_path, "image_result_marked_fail", label=label, error=error[:200])


def sync_quota_after_image(access_token: str, *, label: str, log_path: Path) -> dict[str, Any]:
    """Mirror production: decrement local quota then pull remote limits."""
    from services.account_service import account_service

    before = account_service.get_account(access_token) or {}
    before_q = int(before.get("quota") or 0)
    account_service.mark_image_result(access_token, True)
    after_mark = account_service.get_account(access_token) or {}
    after_mark_q = int(after_mark.get("quota") or 0)
    remote_q: int | None = None
    remote_err: str | None = None
    for attempt in range(3):
        try:
            remote = account_service.fetch_remote_info(access_token, "sentinel_validation_post_image")
            if remote:
                remote_q = int(remote.get("quota") or 0)
            remote_err = None
            break
        except Exception as exc:
            remote_err = _sanitize_error(exc, 200)
            if "cf_edge_block" not in remote_err or attempt >= 2:
                break
            time.sleep(1.5 * (attempt + 1))
    info = {
        "label": label,
        "email": before.get("email"),
        "quota_before": before_q,
        "quota_after_mark": after_mark_q,
        "quota_remote": remote_q,
        "remote_error": remote_err,
    }
    _log(log_path, "quota_sync", **info)
    return info


def preflight_image_account(access_token: str, *, label: str, log_path: Path) -> dict[str, Any]:
    """Mirror production get_available_access_token gate: remote /me+quota, must be schedulable."""
    from services.account_service import account_service

    if not str(access_token or "").strip():
        raise RuntimeError("preflight: missing access_token")
    account = account_service.fetch_remote_info(access_token, "sentinel_validation_preflight")
    if not isinstance(account, dict) or not account:
        raise RuntimeError("preflight: fetch_remote_info returned empty")
    email = str(account.get("email") or "")
    state = account_service.image_quota_state(account)
    avail = account_service.available_image_quota_for_account(account)
    ok_states = {"ready", "unlimited"}
    if state not in ok_states:
        raise RuntimeError(
            f"preflight: quota_state={state} email={email} quota={account.get('quota')} "
            f"restore_at={account.get('restore_at')}"
        )
    if avail == 0:
        raise RuntimeError(f"preflight: available_quota=0 email={email} state={state}")
    info = {
        "label": label,
        "email": email,
        "quota_state": state,
        "quota": int(account.get("quota") or 0),
        "available_quota": avail,
        "last_quota_refresh_at": account.get("last_quota_refresh_at"),
        "restore_at": account.get("restore_at"),
    }
    _log(log_path, "image_preflight_ok", **info)
    return info


def pick_concurrent_accounts(
    accounts: list[dict[str, Any]],
    workers: int,
    *,
    unique_egress: bool = False,
) -> list[dict[str, Any]]:
    """Pick top-quota accounts; optionally require distinct proxy / egress hint."""
    ordered = sorted(accounts, key=lambda x: int(x.get("_quota") or 0), reverse=True)
    picked: list[dict[str, Any]] = []
    seen_proxy: set[str] = set()
    seen_egress: set[str] = set()
    for acc in ordered:
        if len(picked) >= workers:
            break
        proxy = str(acc.get("proxy") or "").strip()
        if not proxy:
            continue
        egress = str(acc.get("proxy_egress_ip") or "").strip() or proxy.rsplit("@", 1)[-1]
        if unique_egress:
            if proxy in seen_proxy or egress in seen_egress:
                continue
            seen_proxy.add(proxy)
            seen_egress.add(egress)
        picked.append(acc)
    return picked


def _verify_quota_row_complete(row: dict[str, Any] | None) -> bool:
    if not row or not row.get("ok"):
        return False
    qs = row.get("quota_sync") or {}
    if qs.get("remote_error"):
        return False
    return qs.get("quota_remote") is not None


def _upload_reference(api: OpenAIBackendAPI) -> list[dict[str, Any]]:
    import base64

    data_url = f"data:image/png;base64,{REF_PNG_B64}"
    meta = api._upload_image(data_url, "validation_ref.png")
    return [meta]


def open_ticket(sess: requests.Session, fp: dict, token: str, meter: TrafficMeter) -> tuple[_Req, dict]:
    t0 = time.time()
    req, cf = _requirements(sess, meter, fp, token)
    egress = _probe_egress(sess, meter, None)
    return req, {
        "finalize_at": _utc(),
        "finalize_ms": int((time.time() - t0) * 1000),
        "egress_ip": (egress or {}).get("ip"),
        "token_len": len(req.token),
        "cf_observability": cf,
        "traffic_after_open": meter.snapshot(),
    }


def run_full_image(
    secret: dict,
    proxy: str | None,
    req: _Req,
    *,
    label: str,
    log_path: Path,
    prompt_mode: str = "text_simple",
    sync_quota: bool = True,
) -> dict[str, Any]:
    """prepare → SSE → poll → download with bandwidth metering."""
    token = str(secret.get("access_token") or "")
    fp = _fp_from_secret(secret)
    prompt, spa_tool, mode_tag = prompt_for_mode(prompt_mode)
    meter = TrafficMeter()
    timings: dict[str, int] = {}
    out: dict[str, Any] = {
        "label": label,
        "email": secret.get("email"),
        "prompt_mode": mode_tag,
        "ok": False,
        "started_at": _utc(),
    }
    references: list[dict[str, Any]] = []
    t_all = time.time()
    sess = _session(fp, proxy)
    try:
        egress = _probe_egress(sess, meter, proxy)
        out["egress"] = egress
        out["proxy_egress_hint"] = secret.get("proxy_egress_ip")

        api = OpenAIBackendAPI(access_token=token)
        api.account = {
            "email": secret.get("email"),
            "proxy": proxy or "",
            "fp": fp,
            "status": "正常",
        }
        api.session = sess
        api.fp = fp
        api.user_agent = fp["user-agent"]
        api.device_id = fp["oai-device-id"]
        api.session_id = fp["oai-session-id"]

        if mode_tag == "image_edit":
            references = _upload_reference(api)
            out["reference_file_id"] = references[0].get("file_id")

        t0 = time.time()
        prep_path = "/backend-api/f/conversation/prepare"
        prep = sess.post(
            BASE + prep_path,
            headers=_hdr(fp, prep_path, token),
            json=_make_image_prepare_body(prompt, "auto", TZ, OFFSET, spa_tool_path=spa_tool),
            timeout=60,
        )
        ensure_ok(prep, prep_path)
        prep_json = prep.json() if prep.text else {}
        conduit_token = str((prep_json or {}).get("conduit_token") or "").strip()
        timings["prepare_ms"] = int((time.time() - t0) * 1000)
        out["conduit_from_prepare"] = bool(conduit_token)
        if not spa_tool and not conduit_token:
            raise RuntimeError("missing_conduit_token_for_picture_v2")

        path = "/backend-api/f/conversation"
        body = build_image_start_body(
            prompt,
            "auto",
            references=references,
            timezone=TZ,
            timezone_offset=OFFSET,
            spa_tool_path=spa_tool,
        )
        headers = _hdr(
            fp,
            path,
            token,
            build_image_start_headers(req, conduit_token, spa_tool_path=spa_tool),
        )
        headers["Accept"] = "text/event-stream"
        t0 = time.time()
        resp = sess.post(BASE + path, headers=headers, json=body, timeout=300, stream=True)
        out["http_status"] = int(getattr(resp, "status_code", 0) or 0)
        ensure_ok(resp, path)
        t_sse = time.time()
        sse = consume_image_sse(
            resp.iter_lines(),
            t0=t_sse,
            gate_secs=IMAGE_DEADLINE,
            total_read_secs=SSE_READ,
        )
        resp.close()
        meter.resp_bytes += sse.sse_bytes
        meter.calls += 1
        timings["sse_ms"] = sse.total_ms
        out["conversation_id"] = sse.cid
        out["has_image_gen"] = bool(sse.has_image_gen_within_gate)
        sediment_ids = list(getattr(sse, "sediment_ids", None) or [])
        out["failure_class"] = classify_image_sse_failure(
            has_image_gen_within_gate=sse.has_image_gen_within_gate,
            gate_failed=sse.gate_failed,
            late_image_gen_seen=sse.late_image_gen_seen,
            tool_args_like_seen=sse.tool_args_like_seen,
            quiet_stream=sse.quiet_stream,
            chunks=sse.chunks,
        )
        if not sse.cid:
            raise RuntimeError("missing_conversation_id")
        if not sse.has_image_gen_within_gate:
            raise RuntimeError(f"no_image_gen:{out['failure_class']}")

        orig_get, orig_post = sess.get, sess.post

        def get_m(url, *a, **kw):
            r = orig_get(url, *a, **kw)
            if not kw.get("stream"):
                meter.add_resp(r)
            meter.calls += 1
            return r

        def post_m(url, *a, **kw):
            body_b = kw.get("data")
            if body_b is None and kw.get("json") is not None:
                body_b = json.dumps(kw["json"])
            meter.add_req(body_b if isinstance(body_b, (bytes, str)) else None)
            r = orig_post(url, *a, **kw)
            if not kw.get("stream"):
                meter.add_resp(r)
            meter.calls += 1
            return r

        sess.get = get_m  # type: ignore[method-assign]
        sess.post = post_m  # type: ignore[method-assign]

        t0 = time.time()
        from services.protocol.conversation import _resolve_image_urls_with_poll_cf_swap_retry

        urls = _resolve_image_urls_with_poll_cf_swap_retry(
            api,
            token,
            conversation_id=sse.cid,
            file_ids=list(sse.file_ids),
            sediment_ids=sediment_ids,
            poll_timeout=180.0,
            account_email=str(secret.get("email") or ""),
            is_text_reply=False,
            request=None,
            index=0,
            sediment_notify_ids=sediment_ids,
        )
        timings["resolve_ms"] = int((time.time() - t0) * 1000)
        if not urls:
            raise RuntimeError("no_image_urls")
        images = []
        t0 = time.time()
        blobs = api.download_image_bytes(urls)
        for idx, blob in enumerate(blobs[:3]):
            meter.resp_bytes += len(blob)
            images.append(persist_image(log_path.parent / "images", idx, blob))
        timings["download_ms"] = int((time.time() - t0) * 1000)
        out["images"] = images
        out["ok"] = True
        out["traffic"] = meter.snapshot()
        out["timings_ms"] = timings
        out["bandwidth"] = {
            "req_bytes": meter.req_bytes,
            "resp_bytes": meter.resp_bytes,
            "total_bytes": meter.req_bytes + meter.resp_bytes,
            "http_calls": meter.calls,
            "image_bytes": sum(int(i.get("bytes") or 0) for i in images),
        }
        if sync_quota and token:
            out["quota_sync"] = sync_quota_after_image(token, label=label, log_path=log_path)
    except Exception as exc:
        out["ok"] = False
        out["error"] = _sanitize_error(exc, 500)
        out["traceback"] = traceback.format_exc()[-1200:]
        out["traffic"] = meter.snapshot()
        out["timings_ms"] = timings
        if sync_quota and token:
            mark_image_fail(token, label=label, error=out["error"], log_path=log_path)
        _log(log_path, "image_failed", label=label, error=out["error"])
        raise
    finally:
        out["elapsed_ms"] = int((time.time() - t_all) * 1000)
        out["finished_at"] = _utc()
        _log(log_path, "image_done", **{k: out[k] for k in ("label", "ok", "egress", "bandwidth", "elapsed_ms") if k in out})
    return out


def cmd_reuse_gap(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    log_path = out_dir / "events.jsonl"
    gaps = [float(x) for x in str(args.gaps).split(",") if x.strip()]
    rounds = int(getattr(args, "rounds", 1) or 1)
    inter_round_sleep = float(getattr(args, "inter_round_sleep", 300) or 300)
    accounts = load_accounts_db(Path(args.accounts_db), limit=40)
    used: set[str] = set()
    report: dict[str, Any] = {
        "phase": "reuse_gap",
        "gaps": gaps,
        "rounds": rounds,
        "inter_round_sleep": inter_round_sleep,
        "round_results": [],
    }
    for round_no in range(1, rounds + 1):
        fresh = pick_fresh_accounts(accounts, n=1, exclude=used)
        if not fresh:
            raise SystemExit(f"reuse-gap round {round_no}: no fresh account")
        secret = fresh[0]
        email = str(secret.get("email") or "").lower()
        used.add(email)
        proxy = str(secret.get("proxy") or "")
        fp = _fp_from_secret(secret)
        token = str(secret.get("access_token") or "")
        round_doc: dict[str, Any] = {"round": round_no, "email": email, "runs": []}
        _log(log_path, "reuse_round_start", round=round_no, email=email, gaps=gaps)
        sess = _session(fp, proxy)
        meter = TrafficMeter()
        req, meta = open_ticket(sess, fp, token, meter)
        _log(log_path, "reuse_open", round=round_no, **meta)
        try:
            first = run_full_image(
                secret,
                proxy,
                req,
                label=f"reuse_r{round_no}_first",
                log_path=log_path,
                sync_quota=True,
            )
            round_doc["runs"].append({"step": "first", "result": first})
        except Exception as exc:
            round_doc["runs"].append({"step": "first", "ok": False, "error": _sanitize_error(exc, 400)})
            report["round_results"].append(round_doc)
            write_evidence(out_dir / "reuse_gap_report.json", report)
            return 1
        for gap in gaps:
            _log(log_path, "reuse_gap_sleep", round=round_no, gap_secs=gap)
            time.sleep(gap)
            try:
                second = run_full_image(
                    secret,
                    proxy,
                    req,
                    label=f"reuse_r{round_no}_after_{int(gap)}s",
                    log_path=log_path,
                    sync_quota=True,
                )
                round_doc["runs"].append({"step": f"after_{int(gap)}s", "gap_secs": gap, "result": second})
            except Exception as exc:
                round_doc["runs"].append(
                    {
                        "step": f"after_{int(gap)}s",
                        "gap_secs": gap,
                        "ok": False,
                        "error": _sanitize_error(exc, 400),
                    }
                )
                report["round_results"].append(round_doc)
                write_evidence(out_dir / "reuse_gap_report.json", report)
                if args.stop_on_error:
                    return 1
                break
        report["round_results"].append(round_doc)
        write_evidence(out_dir / "reuse_gap_report.json", report)
        if round_no < rounds:
            _log(log_path, "reuse_inter_round_sleep", secs=inter_round_sleep, next_round=round_no + 1)
            time.sleep(inter_round_sleep)
    return 0


def _finalize_verify_report(
    report: dict[str, Any],
    pool_accounts: list[dict[str, Any]],
    prev_rows: dict[str, dict[str, Any]],
) -> None:
    by_email = {str(r.get("email") or "").lower(): r for r in report.get("accounts") or []}
    merged: list[dict[str, Any]] = []
    for idx, secret in enumerate(pool_accounts):
        email = str(secret.get("email") or "")
        key = email.lower()
        row = by_email.get(key) or prev_rows.get(key)
        if row:
            merged.append(row)
        else:
            merged.append({"index": idx, "email": email, "quota_hint": secret.get("_quota"), "ok": False, "error": "not_run"})
    report["accounts"] = merged
    report["total"] = len(pool_accounts)


def cmd_verify_quota_all(args: argparse.Namespace) -> int:
    """One production-path image per pooled account; sync quota each time."""
    out_dir = Path(args.out_dir)
    log_path = out_dir / "events.jsonl"
    gap = float(getattr(args, "account_gap", 8) or 8)
    resume = bool(getattr(args, "resume", False))
    skip_emails = {
        str(e).strip().lower()
        for e in str(getattr(args, "skip_emails", "") or "").split(",")
        if str(e).strip()
    }
    only_emails = {
        str(e).strip().lower()
        for e in str(getattr(args, "only_emails", "") or "").split(",")
        if str(e).strip()
    }
    report_path = out_dir / "verify_quota_all_report.json"
    prev_rows: dict[str, dict[str, Any]] = {}
    if resume and report_path.is_file():
        for row in json.loads(report_path.read_text(encoding="utf-8")).get("accounts") or []:
            email = str(row.get("email") or "").lower()
            if email:
                prev_rows[email] = row
    pool_accounts = load_accounts_db(Path(args.accounts_db), limit=500, min_quota=1, prefer_cf=False)
    pool_accounts.sort(key=lambda x: int(x.get("_quota") or 0), reverse=True)
    run_accounts = pool_accounts
    if only_emails:
        run_accounts = [a for a in pool_accounts if str(a.get("email") or "").lower() in only_emails]
    report: dict[str, Any] = {
        "phase": "verify_quota_all",
        "total": len(pool_accounts),
        "account_gap_secs": gap,
        "resume": resume,
        "accounts": [],
    }
    _log(
        log_path,
        "verify_quota_all_start",
        total=len(pool_accounts),
        run=len(run_accounts),
        resume=resume,
        skip_emails=sorted(skip_emails),
        only_emails=sorted(only_emails),
    )
    ran_any = False
    for idx, secret in enumerate(run_accounts):
        email = str(secret.get("email") or "")
        email_key = email.lower()
        if email_key in skip_emails:
            prev = prev_rows.get(email_key)
            if prev:
                report["accounts"].append(prev)
            _log(log_path, "verify_quota_account_skip", index=idx, email=email, reason="skip_emails")
            continue
        prev = prev_rows.get(email_key)
        if resume and _verify_quota_row_complete(prev):
            report["accounts"].append(prev)
            _log(log_path, "verify_quota_account_skip", index=idx, email=email, reason="already_synced")
            continue
        proxy = str(secret.get("proxy") or "")
        fp = _fp_from_secret(secret)
        token = str(secret.get("access_token") or "")
        label = f"verify_all_{idx}_{email.split('@')[0][:12]}"
        row: dict[str, Any] = {"index": idx, "email": email, "quota_hint": secret.get("_quota")}
        _log(log_path, "verify_quota_account_start", index=idx, email=email, resume=bool(prev))
        try:
            sess = _session(fp, proxy)
            req, meta = open_ticket(sess, fp, token, TrafficMeter())
            result = run_full_image(
                secret,
                proxy,
                req,
                label=label,
                log_path=log_path,
                prompt_mode="text_simple",
                sync_quota=True,
            )
            qs = result.get("quota_sync") or {}
            row.update({"ok": True, "open": meta, "quota_sync": qs, "bandwidth": result.get("bandwidth")})
            if qs.get("remote_error"):
                row["ok"] = False
                row["error"] = f"quota_remote_sync_failed: {qs.get('remote_error')}"
        except Exception as exc:
            row.update({"ok": False, "error": _sanitize_error(exc, 500)})
            report["accounts"].append(row)
            _finalize_verify_report(report, pool_accounts, prev_rows)
            write_evidence(report_path, report)
            _log(log_path, "verify_quota_all_stop", failed_email=email, index=idx)
            return 1
        if not row.get("ok"):
            report["accounts"].append(row)
            _finalize_verify_report(report, pool_accounts, prev_rows)
            write_evidence(report_path, report)
            _log(log_path, "verify_quota_all_stop", failed_email=email, index=idx, reason="quota_remote_sync_failed")
            return 1
        report["accounts"].append(row)
        write_evidence(report_path, report)
        ran_any = True
        if idx + 1 < len(run_accounts):
            time.sleep(gap)
    _finalize_verify_report(report, pool_accounts, prev_rows)
    write_evidence(report_path, report)
    _log(log_path, "verify_quota_all_ok", total=len(pool_accounts), ran_any=ran_any)
    return 0


def cmd_retry_verify_alt_proxy(args: argparse.Namespace) -> int:
    """Re-verify failed accounts using a different pooled proxy (egress swap)."""
    out_dir = Path(args.out_dir)
    log_path = out_dir / "events.jsonl"
    emails = [e.strip().lower() for e in str(args.emails or "").split(",") if e.strip()]
    if not emails:
        raise SystemExit("retry-verify-alt-proxy requires --emails")
    pool = load_accounts_db(Path(args.accounts_db), limit=500, min_quota=1, prefer_cf=False)
    pool.sort(key=lambda x: int(x.get("_quota") or 0), reverse=True)
    targets = load_accounts_by_emails(Path(args.accounts_db), emails)
    by_email = {str(a.get("email") or "").lower(): a for a in targets}
    report_path = out_dir / "retry_verify_alt_proxy_report.json"
    report: dict[str, Any] = {"emails": emails, "results": []}
    used_proxies: set[str] = set()
    for email in emails:
        secret = by_email.get(email)
        if not secret:
            report["results"].append({"email": email, "ok": False, "error": "account_not_in_pool"})
            write_evidence(report_path, report)
            return 1
        old_proxy = str(secret.get("proxy") or "")
        used_proxies.add(old_proxy)
        alt_proxy, donor = pick_alt_proxy(pool, secret, avoid=used_proxies)
        if not alt_proxy:
            report["results"].append({"email": email, "ok": False, "error": "no_alt_proxy"})
            write_evidence(report_path, report)
            return 1
        used_proxies.add(alt_proxy)
        trial = dict(secret)
        trial["proxy"] = alt_proxy
        label = f"retry_alt_{email.split('@')[0][:12]}"
        _log(
            log_path,
            "retry_verify_alt_proxy_start",
            email=email,
            old_proxy_ep=old_proxy.split("@")[-1],
            new_proxy_ep=alt_proxy.split("@")[-1],
            donor_email=(donor or {}).get("email"),
        )
        try:
            sess = _session(_fp_from_secret(trial), alt_proxy)
            req, meta = open_ticket(sess, _fp_from_secret(trial), str(trial.get("access_token") or ""), TrafficMeter())
            result = run_full_image(
                trial,
                alt_proxy,
                req,
                label=label,
                log_path=log_path,
                prompt_mode="text_simple",
                sync_quota=True,
            )
            qs = result.get("quota_sync") or {}
            row = {
                "email": email,
                "ok": True,
                "old_proxy_ep": old_proxy.split("@")[-1],
                "new_proxy_ep": alt_proxy.split("@")[-1],
                "donor_email": (donor or {}).get("email"),
                "open": meta,
                "quota_sync": qs,
                "bandwidth": result.get("bandwidth"),
            }
            if qs.get("remote_error"):
                row["ok"] = False
                row["error"] = f"quota_remote_sync_failed: {qs.get('remote_error')}"
        except Exception as exc:
            row = {
                "email": email,
                "ok": False,
                "old_proxy_ep": old_proxy.split("@")[-1],
                "new_proxy_ep": alt_proxy.split("@")[-1],
                "error": _sanitize_error(exc, 500),
            }
            report["results"].append(row)
            write_evidence(report_path, report)
            _log(log_path, "retry_verify_alt_proxy_stop", email=email, error=row["error"])
            return 1
        if not row.get("ok"):
            report["results"].append(row)
            write_evidence(report_path, report)
            _log(log_path, "retry_verify_alt_proxy_stop", email=email, error=row.get("error"))
            return 1
        report["results"].append(row)
        write_evidence(report_path, report)
        time.sleep(float(getattr(args, "account_gap", 8) or 8))
    _log(log_path, "retry_verify_alt_proxy_ok", count=len(emails))
    return 0


def cmd_verify_quota(args: argparse.Namespace) -> int:
    """Single image + quota sync smoke for dashboard alignment."""
    out_dir = Path(args.out_dir)
    log_path = out_dir / "events.jsonl"
    secret = load_secret_file(Path(args.secret))
    proxy = str(secret.get("proxy") or "")
    fp = _fp_from_secret(secret)
    token = str(secret.get("access_token") or "")
    sess = _session(fp, proxy)
    req, meta = open_ticket(sess, fp, token, TrafficMeter())
    result = run_full_image(
        secret,
        proxy,
        req,
        label="verify_quota",
        log_path=log_path,
        sync_quota=True,
    )
    write_evidence(out_dir / "verify_quota_report.json", {"ok": True, "open": meta, "result": result})
    return 0


def cmd_cross_serial(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    log_path = out_dir / "events.jsonl"
    accounts = load_accounts_db(Path(args.accounts_db), limit=50, min_quota=1, prefer_cf=False)
    accounts.sort(key=lambda x: int(x.get("_quota") or 0), reverse=True)
    if not accounts:
        raise SystemExit("no accounts for cross-serial")
    round_no = int(args.round)
    primary = accounts[(round_no - 1) % len(accounts)]
    alt_proxy, alt_acc = pick_alt_proxy(accounts, primary)
    if not alt_proxy:
        raise SystemExit("no alt proxy for cross_ip")
    round_no = int(args.round)
    modes = ["cross_session_same_ip", "cross_ip"]
    if args.mode:
        modes = [args.mode]
    report_path = out_dir / "cross_serial_report.json"
    existing = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {"rounds": []}
    round_result: dict[str, Any] = {
        "round": round_no,
        "primary_email": primary.get("email"),
        "primary_quota_hint": primary.get("_quota"),
        "started_at": _utc(),
        "cases": [],
    }
    prompt_modes = ["text_large", "image_edit"]
    for idx, mode in enumerate(modes):
        fp = _fp_from_secret(primary)
        token = str(primary.get("access_token") or "")
        meter = TrafficMeter()
        sess_open = _session(fp, str(primary.get("proxy") or ""))
        req, meta = open_ticket(sess_open, fp, token, meter)
        use_proxy = str(primary.get("proxy") or "") if mode == "cross_session_same_ip" else alt_proxy
        use_secret = primary
        pm = prompt_modes[idx % len(prompt_modes)]
        label = f"serial_r{round_no}_{mode}_{pm}"
        _log(
            log_path,
            "cross_serial_start",
            round=round_no,
            mode=mode,
            prompt_mode=pm,
            open_ip=meta.get("egress_ip"),
            use_proxy_ep=str(use_proxy).split("@")[-1],
            alt_email=(alt_acc or {}).get("email"),
        )
        try:
            result = run_full_image(
                use_secret,
                use_proxy,
                req,
                label=label,
                log_path=log_path,
                prompt_mode=pm,
                sync_quota=True,
            )
            round_result["cases"].append({"mode": mode, "prompt_mode": pm, "open": meta, "ok": True, "result": result})
        except Exception as exc:
            round_result["cases"].append(
                {
                    "mode": mode,
                    "open": meta,
                    "ok": False,
                    "error": _sanitize_error(exc, 500),
                }
            )
            existing.setdefault("rounds", []).append(round_result)
            write_evidence(report_path, existing)
            return 1
        time.sleep(2)
    existing.setdefault("rounds", []).append(round_result)
    write_evidence(report_path, existing)
    _log(log_path, "cross_serial_round_ok", round=round_no)
    return 0


def _cross_worker(payload: dict) -> dict:
    secret = payload["secret"]
    stagger_ms = int(payload.get("stagger_ms") or 0)
    if stagger_ms > 0:
        time.sleep(stagger_ms / 1000.0)
    alt_proxy = payload["alt_proxy"]
    mode = payload["mode"]
    label = payload["label"]
    prompt_mode = payload.get("prompt_mode", "text_simple")
    log_path = Path(payload["log_path"])
    own_proxy_only = bool(payload.get("own_proxy_only"))
    do_preflight = bool(payload.get("preflight", False))
    fp = _fp_from_secret(secret)
    token = str(secret.get("access_token") or "")
    meter = TrafficMeter()
    try:
        if do_preflight:
            preflight_image_account(token, label=label, log_path=log_path)
    except Exception as exc:
        return {
            "ok": False,
            "label": label,
            "email": secret.get("email"),
            "open": None,
            "error": _sanitize_error(exc, 500),
            "preflight_failed": True,
        }
    sess = _session(fp, str(secret.get("proxy") or ""))
    req, meta = open_ticket(sess, fp, token, meter)
    if own_proxy_only:
        use_proxy = str(secret.get("proxy") or "")
        mode_tag = "own_proxy"
    else:
        use_proxy = str(secret.get("proxy") or "") if mode == "cross_session_same_ip" else alt_proxy
        mode_tag = mode
    try:
        result = run_full_image(
            secret,
            use_proxy,
            req,
            label=label,
            log_path=log_path,
            prompt_mode=prompt_mode,
            sync_quota=True,
        )
        return {
            "ok": True,
            "label": label,
            "email": secret.get("email"),
            "open": meta,
            "result": result,
            "use_proxy_mode": mode_tag,
        }
    except Exception as exc:
        return {
            "ok": False,
            "label": label,
            "email": secret.get("email"),
            "open": meta,
            "error": _sanitize_error(exc, 500),
            "use_proxy_mode": mode_tag,
        }


def cmd_cross_concurrent(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    log_path = out_dir / "events.jsonl"
    workers = int(args.workers)
    own_proxy_only = bool(getattr(args, "own_proxy_only", False))
    unique_egress = bool(getattr(args, "unique_egress", False))
    do_preflight = bool(getattr(args, "preflight", False))
    pool = load_accounts_db(Path(args.accounts_db), limit=workers + 80, min_quota=1, prefer_cf=False)
    picks = pick_concurrent_accounts(pool, workers, unique_egress=unique_egress or own_proxy_only)
    if len(picks) < workers:
        raise SystemExit(
            f"need {workers} accounts"
            f"{' with unique egress' if (unique_egress or own_proxy_only) else ''}, got {len(picks)}"
        )
    round_no = int(args.round)
    jobs = []
    for i, sec in enumerate(picks):
        alt_proxy, _ = pick_alt_proxy(pool, sec)
        if not alt_proxy:
            alt_proxy = str(pool[(i + 1) % len(pool)].get("proxy") or "")
        if own_proxy_only:
            mode = "own_proxy"
            pm = "text_large" if i % 2 == 0 else "image_edit"
        else:
            mode = "cross_ip" if i % 2 == 0 else "cross_session_same_ip"
            pm = "text_large" if i % 2 == 0 else "image_edit"
        jobs.append(
            {
                "secret": sec,
                "alt_proxy": alt_proxy,
                "mode": mode,
                "prompt_mode": pm,
                "label": f"conc_r{round_no}_w{i}_{mode}_{pm}",
                "log_path": str(log_path),
                "own_proxy_only": own_proxy_only,
                "preflight": do_preflight,
                "stagger_ms": i * 2000,
            }
        )
    _log(
        log_path,
        "cross_concurrent_start",
        round=round_no,
        workers=workers,
        own_proxy_only=own_proxy_only,
        unique_egress=unique_egress or own_proxy_only,
        preflight=do_preflight,
        emails=[j["secret"].get("email") for j in jobs],
        egress_hints=[j["secret"].get("proxy_egress_ip") for j in jobs],
    )
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in concurrent.futures.as_completed([pool.submit(_cross_worker, j) for j in jobs]):
            results.append(fut.result())
    report_path = out_dir / "cross_concurrent_report.json"
    existing = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {"rounds": []}
    round_doc = {"round": round_no, "finished_at": _utc(), "workers": workers, "results": results}
    existing.setdefault("rounds", []).append(round_doc)
    write_evidence(report_path, existing)
    failed = [r for r in results if not r.get("ok")]
    if failed:
        _log(log_path, "cross_concurrent_failed", round=round_no, failed=len(failed), first_error=failed[0].get("error"))
        return 1
    _log(log_path, "cross_concurrent_ok", round=round_no, bandwidth_total=sum(
        int((r.get("result") or {}).get("bandwidth", {}).get("total_bytes") or 0) for r in results
    ))
    return 0


def cmd_longevity(args: argparse.Namespace) -> int:
    tier = str(args.tier)
    delay = TIER_SECS.get(tier)
    if delay is None:
        raise SystemExit(f"unknown tier {tier}; choose from {list(TIER_SECS)}")
    out_dir = Path(args.out_dir) / "longevity" / tier
    log_path = out_dir / "events.jsonl"
    secret = load_secret_file(Path(args.secret))
    proxy = str(secret.get("proxy") or "")
    fp = _fp_from_secret(secret)
    token = str(secret.get("access_token") or "")
    sess = _session(fp, proxy)
    meter = TrafficMeter()
    _log(log_path, "longevity_start", tier=tier, delay_secs=delay)
    req, meta = open_ticket(sess, fp, token, meter)
    ticket_path = out_dir / "ticket_snapshot.json"
    write_evidence(
        ticket_path,
        {
            "tier": tier,
            "delay_secs": delay,
            "finalize_at": meta.get("finalize_at"),
            "token_len": meta.get("token_len"),
            "open_egress": meta.get("egress_ip"),
            "req": {
                "token_len": len(req.token),
                "proof_len": len(req.proof_token),
                "turnstile_len": len(req.turnstile_token),
            },
        },
    )
    _log(log_path, "longevity_sleep", tier=tier, delay_secs=delay)
    time.sleep(delay)
    try:
        result = run_full_image(
            secret,
            proxy,
            req,
            label=f"longevity_{tier}",
            log_path=log_path,
            sync_quota=True,
        )
        report = {"tier": tier, "delay_secs": delay, "ok": True, "open": meta, "use": result}
        write_evidence(out_dir / "longevity_result.json", report)
        _log(log_path, "longevity_ok", tier=tier, bandwidth=result.get("bandwidth"))
        return 0
    except Exception as exc:
        report = {
            "tier": tier,
            "delay_secs": delay,
            "ok": False,
            "open": meta,
            "error": _sanitize_error(exc, 500),
        }
        write_evidence(out_dir / "longevity_result.json", report)
        _log(log_path, "longevity_fail", tier=tier, error=report["error"])
        return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secret", default=str(ROOT / "data/runlogs/spa_repro/qaflow_secret.json"))
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--accounts-db", default=str(ROOT / "data/accounts.db"))
    ap.add_argument("--out-dir", default=str(OUT_ROOT))
    ap.add_argument("--stop-on-error", action="store_true", default=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_reuse = sub.add_parser("reuse-gap")
    p_reuse.add_argument("--gaps", default="60,120,180,240,300,360")
    p_reuse.add_argument("--rounds", type=int, default=1)
    p_reuse.add_argument("--inter-round-sleep", type=float, default=300)

    sub.add_parser("verify-quota")
    p_vqa = sub.add_parser("verify-quota-all")
    p_vqa.add_argument("--account-gap", type=float, default=8.0)
    p_vqa.add_argument("--resume", action="store_true", help="skip accounts already synced in report")
    p_vqa.add_argument("--skip-emails", default="", help="comma-separated emails to skip")
    p_vqa.add_argument("--only-emails", default="", help="comma-separated emails to run (subset)")

    p_cs = sub.add_parser("cross-serial")
    p_cs.add_argument("--round", type=int, required=True)
    p_cs.add_argument("--rounds-total", type=int, default=5)
    p_cs.add_argument("--mode", default="", help="cross_ip|cross_session_same_ip|empty=both")

    p_cc = sub.add_parser("cross-concurrent")
    p_cc.add_argument("--round", type=int, required=True)
    p_cc.add_argument("--workers", type=int, default=10)
    p_cc.add_argument(
        "--own-proxy-only",
        action="store_true",
        help="use each account's own proxy for ticket+image+poll (no cross_ip borrow)",
    )
    p_cc.add_argument(
        "--unique-egress",
        action="store_true",
        help="pick workers with distinct proxy URL / proxy_egress_ip",
    )
    p_cc.add_argument(
        "--preflight",
        action="store_true",
        help="run fetch_remote_info schedulability gate before open_ticket (default: off)",
    )

    p_retry = sub.add_parser("retry-verify-alt-proxy")
    p_retry.add_argument("--emails", required=True)
    p_retry.add_argument("--account-gap", type=float, default=8.0)

    p_long = sub.add_parser("longevity")
    p_long.add_argument("--tier", required=True, choices=sorted(TIER_SECS))

    args = ap.parse_args()
    if args.cmd == "reuse-gap":
        return cmd_reuse_gap(args)
    if args.cmd == "verify-quota":
        return cmd_verify_quota(args)
    if args.cmd == "verify-quota-all":
        return cmd_verify_quota_all(args)
    if args.cmd == "cross-serial":
        return cmd_cross_serial(args)
    if args.cmd == "cross-concurrent":
        return cmd_cross_concurrent(args)
    if args.cmd == "retry-verify-alt-proxy":
        return cmd_retry_verify_alt_proxy(args)
    if args.cmd == "longevity":
        return cmd_longevity(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
