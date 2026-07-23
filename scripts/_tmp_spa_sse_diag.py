#!/usr/bin/env python3
"""Diagnose SPA image hang after prepare_ok: status TTFT vs picture_v2+conduit.

Run inside chatgpt2api-local:
  GPTIMAGE_ROOT=/app /app/.venv/bin/python /app/scripts/_tmp_spa_sse_diag.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("GPTIMAGE_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT))

from curl_cffi import requests  # noqa: E402

from services.account_service import account_service  # noqa: E402
from utils.helper import ensure_ok, new_uuid  # noqa: E402
from utils.pow import (  # noqa: E402
    build_legacy_requirements_token,
    build_proof_token,
    parse_pow_resources,
)
from utils.turnstile import solve_turnstile_token  # noqa: E402

try:
    from services.protocol.chatgpt_web_request import (  # noqa: E402
        build_chat_body,
        build_chat_headers,
        build_text_prepare_body,
    )
except Exception:
    build_chat_body = None  # type: ignore
    build_chat_headers = None  # type: ignore
    build_text_prepare_body = None  # type: ignore

BASE = "https://chatgpt.com"
EMAIL = os.environ.get("DIAG_EMAIL", "qaflowgq5wyuxhe9@proton.me").strip().lower()
PROMPT = (
    "Create a medium-detail digital illustration of a rainy Tokyo side street at dusk: "
    "neon shop signs reflecting on wet asphalt, a bicycle parked under a red awning, "
    "warm interior lights spilling onto the sidewalk, cinematic atmosphere, soft depth of field, "
    "no text, no watermark, no logos"
)


def log(**kw: Any) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def load_account() -> dict:
    account_service.reload_from_storage()
    for a in account_service.list_accounts():
        if str(a.get("email") or "").strip().lower() == EMAIL:
            return a
    raise SystemExit(f"account not found: {EMAIL}")


def fp_from(account: dict) -> dict:
    raw = account.get("fp") if isinstance(account.get("fp"), dict) else {}
    ua = str(raw.get("userAgent") or raw.get("user-agent") or "").strip()
    if not ua:
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    return {
        "user-agent": ua,
        "impersonate": str(raw.get("impersonate") or "chrome131"),
        "oai-device-id": str(raw.get("oai-device-id") or raw.get("deviceId") or new_uuid()),
        "oai-language": str(raw.get("oai-language") or "en-US"),
    }


def hdr(fp: dict, path: str, token: str, extra: dict | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {token}",
        "User-Agent": fp["user-agent"],
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": BASE + "/",
        "oai-device-id": fp["oai-device-id"],
        "oai-language": fp["oai-language"],
        "oai-client-version": "prod-cdd1ba1b5f28e6207ce49349d4bf91b4f49953f0",
    }
    if extra:
        h.update(extra)
    return h


def requirements(sess: requests.Session, fp: dict, token: str) -> dict:
    """Mirror scripts/_tmp_spa_image_bench3._requirements (known-good path)."""
    home_status = None
    home_text = ""
    try:
        home = sess.get(BASE + "/", headers={"User-Agent": fp["user-agent"]}, timeout=20)
        home_status = int(home.status_code or 0)
        if home_status < 400:
            home_text = home.text or ""
        log(
            phase="home",
            status=home_status,
            bytes=len(home.text or ""),
            cf=("cloudflare" in (home.text or "").lower() or home_status == 403),
        )
    except Exception as exc:
        log(phase="home_error", error=str(exc)[:200])
    scripts, build = parse_pow_resources(home_text) if home_text else ([], "")
    p_token = build_legacy_requirements_token(fp["user-agent"], scripts, build)
    prep_path = "/backend-api/sentinel/chat-requirements/prepare"
    prep = sess.post(
        BASE + prep_path,
        headers=hdr(fp, prep_path, token),
        json={"p": p_token},
        timeout=30,
    )
    ensure_ok(prep, "chat_requirements_prepare")
    prep_data = prep.json()
    proof = ""
    pow_info = prep_data.get("proofofwork") or {}
    if pow_info.get("required"):
        proof = build_proof_token(
            pow_info.get("seed", ""),
            pow_info.get("difficulty", ""),
            fp["user-agent"],
            script_sources=scripts,
            data_build=build,
        )
    turnstile = ""
    ts = prep_data.get("turnstile") or {}
    if ts.get("required") and ts.get("dx"):
        try:
            turnstile = solve_turnstile_token(ts["dx"], p_token) or ""
        except Exception as exc:
            log(phase="turnstile_skip", error=str(exc)[:120])
    fin_path = "/backend-api/sentinel/chat-requirements/finalize"
    fin = sess.post(
        BASE + fin_path,
        headers=hdr(fp, fin_path, token),
        json={
            "prepare_token": prep_data.get("prepare_token", ""),
            "proofofwork": proof,
            "turnstile": turnstile,
        },
        timeout=45,
    )
    ensure_ok(fin, "chat_requirements_finalize")
    data = fin.json()
    tok = str(data.get("token") or "").strip()
    if not tok:
        raise RuntimeError(f"missing_requirements_token:{data}")
    return {
        "token": tok,
        "proof_token": proof,
        "turnstile_token": turnstile,
        "home_status": home_status,
    }


def prepare_body(prompt: str) -> dict:
    if build_text_prepare_body is not None:
        return build_text_prepare_body(prompt, "auto", timezone="Asia/Tokyo", timezone_offset=-540)
    msg_id = new_uuid()
    return {
        "action": "next",
        "parent_message_id": "client-created-root",
        "model": "auto",
        "timezone_offset_min": -540,
        "timezone": "Asia/Tokyo",
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": [],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "partial_query": {
            "id": msg_id,
            "author": {"role": "user"},
            "content": {"content_type": "text", "parts": [prompt]},
        },
    }


def chat_body(prompt: str, *, system_hints: list[str]) -> dict:
    if build_chat_body is not None:
        body = build_chat_body(
            [
                {
                    "id": new_uuid(),
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [prompt]},
                }
            ],
            "auto",
            timezone="Asia/Tokyo",
            timezone_offset=-540,
            history_and_training_disabled=False,
        )
    else:
        body = {
            "action": "next",
            "messages": [
                {
                    "id": new_uuid(),
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [prompt]},
                }
            ],
            "parent_message_id": "client-created-root",
            "model": "auto",
            "timezone_offset_min": -540,
            "timezone": "Asia/Tokyo",
            "conversation_mode": {"kind": "primary_assistant"},
            "supports_buffering": True,
            "supported_encodings": ["v1"],
        }
    body["system_hints"] = list(system_hints)
    return body


def sentinel_headers(req: dict) -> dict:
    if build_chat_headers is not None:
        class _R:
            pass

        r = _R()
        r.token = req["token"]
        r.proof_token = req.get("proof_token") or ""
        r.turnstile_token = req.get("turnstile_token") or ""
        r.so_token = ""
        return build_chat_headers(r)  # type: ignore[arg-type]
    h = {
        "OpenAI-Sentinel-Chat-Requirements-Token": req["token"],
        "OpenAI-Sentinel-Chat-Requirements-Prepare-Token": req["token"],
    }
    if req.get("proof_token"):
        h["OpenAI-Sentinel-Proof-Token"] = req["proof_token"]
    if req.get("turnstile_token"):
        h["OpenAI-Sentinel-Turnstile-Token"] = req["turnstile_token"]
    return h


def probe_sse(
    sess: requests.Session,
    fp: dict,
    token: str,
    req: dict,
    *,
    label: str,
    system_hints: list[str],
    conduit: str,
    connect_timeout: float = 20.0,
    first_byte_timeout: float = 45.0,
) -> dict:
    path = "/backend-api/f/conversation"
    body = chat_body(PROMPT, system_hints=system_hints)
    headers = hdr(fp, path, token, sentinel_headers(req))
    headers["Accept"] = "text/event-stream"
    if conduit:
        headers["X-Conduit-Token"] = conduit
    log(
        phase="sse_start",
        label=label,
        hints=system_hints,
        has_conduit=bool(conduit),
        conduit_len=len(conduit or ""),
        body_keys=sorted(body.keys())[:30],
    )
    t0 = time.time()
    try:
        # (connect, read) — read timeout covers waiting for first/next chunk
        resp = sess.post(
            BASE + path,
            headers=headers,
            json=body,
            timeout=(connect_timeout, first_byte_timeout),
            stream=True,
        )
    except Exception as exc:
        return {
            "label": label,
            "ok": False,
            "error": f"post_exception:{exc.__class__.__name__}:{str(exc)[:240]}",
            "elapsed_ms": int((time.time() - t0) * 1000),
            "hints": system_hints,
            "has_conduit": bool(conduit),
        }
    header_ms = int((time.time() - t0) * 1000)
    status = int(getattr(resp, "status_code", 0) or 0)
    ctype = str((resp.headers or {}).get("content-type") or "")
    cf_ray = str((resp.headers or {}).get("cf-ray") or "")
    out: dict[str, Any] = {
        "label": label,
        "status": status,
        "content_type": ctype,
        "cf_ray": cf_ray,
        "header_ms": header_ms,
        "hints": system_hints,
        "has_conduit": bool(conduit),
    }
    # peek first bytes without hanging forever
    t1 = time.time()
    first = b""
    first_line = ""
    try:
        # prefer raw for first-byte timing
        raw = getattr(resp, "raw", None)
        if raw is not None and hasattr(raw, "read"):
            first = raw.read(512) or b""
        else:
            # fallback: one iter line
            for line in resp.iter_lines():
                if isinstance(line, bytes):
                    first = line
                else:
                    first = str(line).encode("utf-8", errors="ignore")
                break
        first_line = first.decode("utf-8", errors="ignore")[:240]
    except Exception as exc:
        out["first_byte_error"] = f"{exc.__class__.__name__}:{str(exc)[:200]}"
    out["first_byte_ms"] = int((time.time() - t1) * 1000)
    out["elapsed_ms"] = int((time.time() - t0) * 1000)
    out["first_bytes"] = len(first)
    out["first_line"] = first_line
    low = first_line.lower()
    out["looks_cf_html"] = ("<!doctype html" in low) or ("cloudflare" in low) or ("just a moment" in low)
    out["looks_sse"] = first_line.startswith("data:") or "text/event-stream" in ctype.lower()
    try:
        resp.close()
    except Exception:
        pass
    out["ok"] = status < 400 and not out["looks_cf_html"] and (out["first_bytes"] > 0)
    return out


def main() -> int:
    account = load_account()
    token = str(account.get("access_token") or "").strip()
    proxy = str(account.get("proxy") or "").strip()
    if not token or not proxy:
        raise SystemExit("missing token/proxy")
    fp = fp_from(account)
    log(
        phase="account",
        email=account.get("email"),
        quota=account.get("quota"),
        egress=account.get("proxy_egress_ip"),
        provider=account.get("proxy_provider"),
        proxy_host=proxy.split("@")[-1] if "@" in proxy else proxy,
    )

    sess = requests.Session(impersonate=fp["impersonate"], verify=False, timeout=60, proxy=proxy)
    t0 = time.time()
    try:
        eg = sess.get("https://api.ipify.org?format=json", timeout=20)
        log(phase="egress", status=eg.status_code, body=(eg.text or "")[:80], ms=int((time.time() - t0) * 1000))
    except Exception as exc:
        log(phase="egress_error", error=str(exc)[:200])

    req = requirements(sess, fp, token)
    log(phase="requirements_ok", home_status=req.get("home_status"), ms="n/a")

    prep_path = "/backend-api/f/conversation/prepare"
    t0 = time.time()
    prep = sess.post(
        BASE + prep_path,
        headers=hdr(fp, prep_path, token),
        json=prepare_body(PROMPT),
        timeout=45,
    )
    ensure_ok(prep, prep_path)
    prep_json = prep.json() if prep.content else {}
    conduit = str(prep_json.get("conduit_token") or "").strip()
    log(
        phase="prepare_ok",
        ms=int((time.time() - t0) * 1000),
        conduit=bool(conduit),
        conduit_len=len(conduit),
        prep_keys=sorted(prep_json.keys()) if isinstance(prep_json, dict) else [],
    )

    results = []
    # A: current failing bench shape
    results.append(
        probe_sse(
            sess,
            fp,
            token,
            req,
            label="A_spa_empty_hints_no_conduit",
            system_hints=[],
            conduit="",
            first_byte_timeout=40.0,
        )
    )
    log(phase="sse_result", **results[-1])

    # B: production-like Create Image UI
    # fresh prepare for conduit freshness
    prep2 = sess.post(
        BASE + prep_path,
        headers=hdr(fp, prep_path, token),
        json=prepare_body(PROMPT),
        timeout=45,
    )
    conduit2 = ""
    try:
        ensure_ok(prep2, prep_path)
        conduit2 = str((prep2.json() or {}).get("conduit_token") or "").strip()
    except Exception as exc:
        log(phase="prepare2_fail", error=str(exc)[:200])
    results.append(
        probe_sse(
            sess,
            fp,
            token,
            req,
            label="B_picture_v2_with_conduit",
            system_hints=["picture_v2"],
            conduit=conduit2 or conduit,
            first_byte_timeout=40.0,
        )
    )
    log(phase="sse_result", **results[-1])

    # C: empty hints BUT with conduit (ablation)
    results.append(
        probe_sse(
            sess,
            fp,
            token,
            req,
            label="C_spa_empty_hints_WITH_conduit",
            system_hints=[],
            conduit=conduit2 or conduit,
            first_byte_timeout=40.0,
        )
    )
    log(phase="sse_result", **results[-1])

    summary = {
        "email": EMAIL,
        "home_status": req.get("home_status"),
        "variants": [
            {
                "label": r.get("label"),
                "ok": r.get("ok"),
                "status": r.get("status"),
                "header_ms": r.get("header_ms"),
                "first_byte_ms": r.get("first_byte_ms"),
                "elapsed_ms": r.get("elapsed_ms"),
                "looks_cf_html": r.get("looks_cf_html"),
                "looks_sse": r.get("looks_sse"),
                "error": r.get("error"),
                "first_line": (r.get("first_line") or "")[:160],
            }
            for r in results
        ],
    }
    log(phase="summary", **summary)
    out = ROOT / "data" / "runlogs" / "spa_repro" / f"sse-diag-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"results": results, "summary": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    log(phase="wrote", path=str(out))
    return 0 if any(r.get("ok") for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
