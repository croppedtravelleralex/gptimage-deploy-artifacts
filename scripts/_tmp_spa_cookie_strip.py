#!/usr/bin/env python3
"""A-layer minimal cookie set: Camoufox warm → curl_cffi strip one-by-one.

Flow:
  1) Camoufox headless + proxy: inject session-token, GET chatgpt.com/,
     sentinel prepare+finalize; export cookies (names logged, values in-memory only)
  2) curl_cffi same proxy: full-cookie baseline (prepare 200 [+ optional short SSE])
  3) For each cookie name: drop that cookie, retest
  4) Combos: session+oai-did only / CF-only / empty
  5) Write data/runlogs/spa_repro/bench3/cookie_strip_*.json (names + status only)

Usage:
  python scripts/_tmp_spa_cookie_strip.py
  python scripts/_tmp_spa_cookie_strip.py --proxy http://127.0.0.1:7897
  python scripts/_tmp_spa_cookie_strip.py --skip-sse
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camoufox.sync_api import Camoufox  # noqa: E402
from curl_cffi import requests  # noqa: E402

from services.openai_backend_api import (  # noqa: E402
    DEFAULT_CLIENT_BUILD_NUMBER,
    DEFAULT_CLIENT_VERSION,
)
from services.protocol.chatgpt_web_request import (  # noqa: E402
    build_chat_body,
    build_chat_headers,
    build_text_prepare_body,
)
from utils.helper import anonymize_token, iter_sse_payloads, new_uuid  # noqa: E402
from utils.pow import build_legacy_requirements_token, build_proof_token, parse_pow_resources  # noqa: E402
from utils.turnstile import solve_turnstile_token  # noqa: E402

SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
OUT_DIR = ROOT / "data" / "runlogs" / "spa_repro" / "bench3"
DEFAULT_PROXY = "http://127.0.0.1:7897"
BASE = "https://chatgpt.com"
TZ = "Asia/Tokyo"
TZ_OFFSET = -540
SESSION_COOKIE = "__Secure-next-auth.session-token"
OAI_DID = "oai-did"
CF_COOKIE_NAMES = ("__cf_bm", "_cfuvid", "__cflb")


def _log(**kw: Any) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


class _Req:
    def __init__(self, token: str, proof: str = "", turnstile: str = "", so: str = ""):
        self.token = token
        self.proof_token = proof
        self.turnstile_token = turnstile
        self.so_token = so


def _fp(secret: dict) -> dict:
    fp = dict(secret.get("fp") if isinstance(secret.get("fp"), dict) else {})
    fp.setdefault(
        "user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    )
    fp.setdefault("impersonate", "chrome131")
    fp.setdefault("oai-device-id", new_uuid())
    fp.setdefault("oai-session-id", new_uuid())
    fp.setdefault("accept-language", "en-US,en;q=0.9")
    return fp


def _proxy_dict(proxy: str) -> dict[str, str]:
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 7897
    return {"server": f"{scheme}://{host}:{port}"}


def _hdr(fp: dict, path: str, token: str, extra: dict | None = None) -> dict:
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": fp["user-agent"],
        "Accept-Language": fp["accept-language"],
        "Authorization": f"Bearer {token}",
        "OAI-Device-Id": fp["oai-device-id"],
        "OAI-Session-Id": fp["oai-session-id"],
        "OAI-Client-Version": DEFAULT_CLIENT_VERSION,
        "OAI-Client-Build-Number": DEFAULT_CLIENT_BUILD_NUMBER,
        "OAI-Language": "en-US",
        "Origin": BASE,
        "Referer": BASE + "/",
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    }
    if extra:
        h.update(extra)
    return h


def _is_tls_error(exc: BaseException) -> bool:
    msg = str(exc)
    return any(
        x in msg
        for x in ("TLS", "OPENSSL", "curl: (35)", "SSLEOF", "Recv failure", "Connection", "SSL")
    )


def _cookies_to_header(cookies: list[dict[str, Any]]) -> tuple[str, list[str]]:
    parts: list[str] = []
    names: list[str] = []
    for c in cookies:
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "")
        if not name:
            continue
        names.append(name)
        parts.append(f"{name}={value}")
    # preserve first-seen order for strip loop; unique names for logging
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return "; ".join(parts), ordered


def _filter_cookies(
    cookies: list[dict[str, Any]],
    *,
    drop: str | None = None,
    keep: set[str] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in cookies:
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        if drop and name == drop:
            continue
        if keep is not None and name not in keep:
            continue
        out.append(c)
    return out


def _camoufox_warm(fp: dict, access_token: str, session_token: str, proxy: str) -> dict[str, Any]:
    """Inject session-token, GET home + sentinel prepare/finalize; return cookies in-memory."""
    out: dict[str, Any] = {
        "ok": False,
        "home_status": 0,
        "req_prepare_status": 0,
        "req_finalize_status": 0,
        "cookie_names": [],
        "cookie_count": 0,
    }
    raw_cookies: list[dict[str, Any]] = []
    with Camoufox(headless=True, proxy=_proxy_dict(proxy), geoip=False) as browser:
        ctx = browser.new_context()
        ctx.set_default_timeout(120000)
        if session_token:
            ctx.add_cookies(
                [
                    {
                        "name": SESSION_COOKIE,
                        "value": session_token,
                        "domain": "chatgpt.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
        api = ctx.request

        t0 = time.time()
        try:
            eg = api.get("https://api.ipify.org?format=json", timeout=45000)
            eg_json = eg.json() if eg.status == 200 else {}
            egress_ip = str(eg_json.get("ip") or "")
        except Exception as exc:
            egress_ip = ""
            eg_json = {"error": str(exc)[:160]}
        out["egress"] = {"ok": bool(egress_ip), "ip": egress_ip, "ms": int((time.time() - t0) * 1000)}
        _log(phase="warm_egress", **out["egress"])

        home = api.get(BASE + "/", timeout=90000)
        home_text = home.text() if home.status < 500 else ""
        out["home_status"] = int(home.status)
        out["home_bytes"] = len(home_text or "")
        _log(phase="warm_home", status=out["home_status"], bytes=out["home_bytes"])

        scripts, build = parse_pow_resources(home_text) if home_text else ([], "")
        p_token = build_legacy_requirements_token(fp["user-agent"], scripts, build)

        prep = api.post(
            BASE + "/backend-api/sentinel/chat-requirements/prepare",
            headers=_hdr(fp, "/backend-api/sentinel/chat-requirements/prepare", access_token),
            data=json.dumps({"p": p_token}),
            timeout=90000,
        )
        out["req_prepare_status"] = int(prep.status)
        _log(phase="warm_req_prepare", status=out["req_prepare_status"], body=(prep.text() or "")[:120])
        if prep.status != 200:
            raw_cookies = list(ctx.cookies())
            _, names = _cookies_to_header(raw_cookies)
            if SESSION_COOKIE not in names and session_token:
                names = [SESSION_COOKIE] + names
            out["cookie_names"] = names
            out["cookie_count"] = len(names)
            out["_cookies"] = raw_cookies
            return out

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
            turnstile = solve_turnstile_token(ts["dx"], p_token) or ""

        fin = api.post(
            BASE + "/backend-api/sentinel/chat-requirements/finalize",
            headers=_hdr(fp, "/backend-api/sentinel/chat-requirements/finalize", access_token),
            data=json.dumps(
                {
                    "prepare_token": prep_data.get("prepare_token", ""),
                    "proofofwork": proof,
                    "turnstile": turnstile,
                }
            ),
            timeout=90000,
        )
        out["req_finalize_status"] = int(fin.status)
        _log(phase="warm_req_finalize", status=out["req_finalize_status"])
        out["ok"] = fin.status == 200

        raw_cookies = list(ctx.cookies())
        _, names = _cookies_to_header(raw_cookies)
        if SESSION_COOKIE not in names and session_token:
            # ensure injected name appears in strip list even if store omitted it
            names = [SESSION_COOKIE] + names
            raw_cookies = [
                {
                    "name": SESSION_COOKIE,
                    "value": session_token,
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ] + raw_cookies
        out["cookie_names"] = names
        out["cookie_count"] = len(names)
        out["_cookies"] = raw_cookies
    return out


def _curl_probe_once(
    sess: requests.Session,
    fp: dict,
    token: str,
    *,
    cookie_header: str,
    label: str,
    do_sse: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "label": label,
        "cookie_names": [],
        "home_status": 0,
        "req_prepare_status": 0,
        "req_finalize_status": 0,
        "conversation_prepare_status": 0,
        "sse_status": 0,
        "sse_chunks": 0,
        "ok": False,
    }
    if cookie_header:
        # names only for JSON (no values)
        out["cookie_names"] = sorted(
            {p.split("=", 1)[0].strip() for p in cookie_header.split(";") if p.strip() and "=" in p}
        )
    extra_cookie = {"Cookie": cookie_header} if cookie_header else None

    home = sess.get(
        BASE + "/",
        headers={
            "User-Agent": fp["user-agent"],
            **({"Cookie": cookie_header} if cookie_header else {}),
        },
        timeout=45,
    )
    out["home_status"] = int(home.status_code or 0)
    home_text = home.text or ""
    _log(phase="curl_home", label=label, status=out["home_status"], bytes=len(home_text))

    scripts, build = parse_pow_resources(home_text)
    p_token = build_legacy_requirements_token(fp["user-agent"], scripts, build)

    prep_path = "/backend-api/sentinel/chat-requirements/prepare"
    prep = sess.post(
        BASE + prep_path,
        headers=_hdr(fp, prep_path, token, extra_cookie),
        json={"p": p_token},
        timeout=45,
    )
    out["req_prepare_status"] = int(prep.status_code or 0)
    _log(
        phase="curl_req_prepare",
        label=label,
        status=out["req_prepare_status"],
        body=(prep.text or "")[:120],
    )
    if out["req_prepare_status"] != 200:
        out["error"] = f"req_prepare_status={out['req_prepare_status']}"
        return out

    if not do_sse:
        out["ok"] = True
        return out

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
        turnstile = solve_turnstile_token(ts["dx"], p_token) or ""

    fin_path = "/backend-api/sentinel/chat-requirements/finalize"
    fin = sess.post(
        BASE + fin_path,
        headers=_hdr(fp, fin_path, token, extra_cookie),
        json={
            "prepare_token": prep_data.get("prepare_token", ""),
            "proofofwork": proof,
            "turnstile": turnstile,
        },
        timeout=45,
    )
    out["req_finalize_status"] = int(fin.status_code or 0)
    _log(phase="curl_req_finalize", label=label, status=out["req_finalize_status"])
    if out["req_finalize_status"] != 200:
        out["error"] = f"req_finalize_status={out['req_finalize_status']}"
        return out

    fin_data = fin.json()
    req_tok = str(fin_data.get("token") or "")
    if not req_tok:
        out["error"] = "missing_requirements_token"
        return out
    req = _Req(req_tok, proof, turnstile, str(fin_data.get("so_token") or ""))

    prompt = "Reply with exactly: STRIP"
    prep_c_path = "/backend-api/f/conversation/prepare"
    prep_body = build_text_prepare_body(prompt, "auto", timezone=TZ, timezone_offset=TZ_OFFSET)
    conv_prep = sess.post(
        BASE + prep_c_path,
        headers=_hdr(fp, prep_c_path, token, extra_cookie),
        json=prep_body,
        timeout=60,
    )
    out["conversation_prepare_status"] = int(conv_prep.status_code or 0)
    _log(phase="curl_conversation_prepare", label=label, status=out["conversation_prepare_status"])
    if out["conversation_prepare_status"] != 200:
        out["error"] = f"conversation_prepare_status={out['conversation_prepare_status']}"
        return out

    path = "/backend-api/f/conversation"
    body = build_chat_body(
        [
            {
                "id": new_uuid(),
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [prompt]},
            }
        ],
        "auto",
        timezone=TZ,
        timezone_offset=TZ_OFFSET,
        history_and_training_disabled=False,
    )
    headers = _hdr(fp, path, token, {**build_chat_headers(req), **(extra_cookie or {})})
    resp = sess.post(BASE + path, headers=headers, json=body, timeout=180, stream=True)
    out["sse_status"] = int(resp.status_code or 0)
    chunks: list[str] = []
    if 200 <= out["sse_status"] < 300:
        for payload in iter_sse_payloads(resp):
            chunks.append(payload)
            if len(chunks) >= 4:
                break
    out["sse_chunks"] = len(chunks)
    try:
        resp.close()
    except Exception:
        pass
    out["ok"] = bool(chunks) and out["sse_status"] == 200
    _log(
        phase="curl_sse",
        label=label,
        status=out["sse_status"],
        sse_chunks=out["sse_chunks"],
        ok=out["ok"],
    )
    return out


def _curl_probe(
    fp: dict,
    token: str,
    proxy: str,
    *,
    cookie_header: str,
    label: str,
    do_sse: bool,
    attempts: int = 4,
) -> dict[str, Any]:
    last: dict[str, Any] = {"ok": False, "label": label, "error": "no_attempt"}
    for attempt in range(1, attempts + 1):
        try:
            sess = requests.Session(
                impersonate=str(fp.get("impersonate") or "chrome131"),
                proxy=proxy,
                verify=False,
                timeout=90,
            )
            try:
                result = _curl_probe_once(
                    sess,
                    fp,
                    token,
                    cookie_header=cookie_header,
                    label=label,
                    do_sse=do_sse,
                )
                result["attempt"] = attempt
                return result
            finally:
                try:
                    sess.close()
                except Exception:
                    pass
        except Exception as exc:
            retryable = _is_tls_error(exc)
            _log(phase="curl_retry", label=label, attempt=attempt, error=str(exc)[:200], retryable=retryable)
            last = {"ok": False, "label": label, "error": str(exc)[:400], "attempt": attempt}
            if not retryable:
                return last
            time.sleep(0.8 * attempt)
    return last


def _arm_summary(arm: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe arm: names + status only (no cookie values / tokens)."""
    return {
        "label": arm.get("label"),
        "cookie_names": arm.get("cookie_names") or [],
        "home_status": arm.get("home_status"),
        "req_prepare_status": arm.get("req_prepare_status"),
        "req_finalize_status": arm.get("req_finalize_status"),
        "conversation_prepare_status": arm.get("conversation_prepare_status"),
        "sse_status": arm.get("sse_status"),
        "sse_chunks": arm.get("sse_chunks"),
        "ok": arm.get("ok"),
        "error": arm.get("error"),
        "attempt": arm.get("attempt"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Camoufox warm cookies → curl_cffi strip probe")
    ap.add_argument("--proxy", default=DEFAULT_PROXY)
    ap.add_argument("--secret", type=Path, default=SECRET)
    ap.add_argument(
        "--skip-sse",
        action="store_true",
        help="Only test sentinel prepare (faster; no finalize/SSE)",
    )
    args = ap.parse_args()

    secret = json.loads(args.secret.read_text(encoding="utf-8"))
    access_token = str(secret.get("access_token") or "").strip()
    session_token = str(secret.get("chatgpt_session_token") or "").strip()
    if not access_token:
        _log(ok=False, error="missing_access_token")
        return 2
    if not session_token:
        _log(ok=False, error="missing_chatgpt_session_token")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fp = _fp(secret)
    proxy = str(args.proxy)
    do_sse = not bool(args.skip_sse)

    report: dict[str, Any] = {
        "mode": "cookie_strip",
        "proxy": proxy,
        "email": secret.get("email"),
        "token_fp": anonymize_token(access_token),
        "do_sse": do_sse,
        "arms": [],
        "ok": False,
    }
    _log(phase="start", proxy=proxy, email=secret.get("email"), do_sse=do_sse)

    warm = _camoufox_warm(fp, access_token, session_token, proxy)
    raw_cookies: list[dict[str, Any]] = list(warm.pop("_cookies", []) or [])
    report["camoufox_warm"] = {k: v for k, v in warm.items() if not str(k).startswith("_")}
    cookie_names: list[str] = list(warm.get("cookie_names") or [])
    _log(
        phase="warm_done",
        ok=warm.get("ok"),
        cookie_count=warm.get("cookie_count"),
        cookie_names=cookie_names,
    )

    full_header, _ = _cookies_to_header(raw_cookies)
    baseline = _curl_probe(
        fp,
        access_token,
        proxy,
        cookie_header=full_header,
        label="baseline_full",
        do_sse=do_sse,
    )
    report["arms"].append(_arm_summary(baseline))
    _log(
        phase="arm",
        label="baseline_full",
        prepare=baseline.get("req_prepare_status"),
        sse=baseline.get("sse_status"),
        ok=baseline.get("ok"),
    )

    for name in cookie_names:
        stripped = _filter_cookies(raw_cookies, drop=name)
        hdr, kept = _cookies_to_header(stripped)
        arm = _curl_probe(
            fp,
            access_token,
            proxy,
            cookie_header=hdr,
            label=f"drop:{name}",
            do_sse=do_sse,
        )
        arm["cookie_names"] = kept
        arm["dropped"] = name
        summary = _arm_summary(arm)
        summary["dropped"] = name
        report["arms"].append(summary)
        _log(
            phase="arm",
            label=summary["label"],
            dropped=name,
            prepare=summary.get("req_prepare_status"),
            sse=summary.get("sse_status"),
            ok=summary.get("ok"),
        )

    # combo: session-token + oai-did only
    keep_sess = {SESSION_COOKIE, OAI_DID}
    hdr_sess, kept_sess = _cookies_to_header(_filter_cookies(raw_cookies, keep=keep_sess))
    arm_sess = _curl_probe(
        fp,
        access_token,
        proxy,
        cookie_header=hdr_sess,
        label="only_session_oai_did",
        do_sse=do_sse,
    )
    arm_sess["cookie_names"] = kept_sess
    report["arms"].append(_arm_summary(arm_sess))
    _log(
        phase="arm",
        label="only_session_oai_did",
        cookie_names=kept_sess,
        prepare=arm_sess.get("req_prepare_status"),
        ok=arm_sess.get("ok"),
    )

    # combo: CF-related only
    keep_cf = set(CF_COOKIE_NAMES)
    hdr_cf, kept_cf = _cookies_to_header(_filter_cookies(raw_cookies, keep=keep_cf))
    arm_cf = _curl_probe(
        fp,
        access_token,
        proxy,
        cookie_header=hdr_cf,
        label="only_cf",
        do_sse=do_sse,
    )
    arm_cf["cookie_names"] = kept_cf
    report["arms"].append(_arm_summary(arm_cf))
    _log(
        phase="arm",
        label="only_cf",
        cookie_names=kept_cf,
        prepare=arm_cf.get("req_prepare_status"),
        ok=arm_cf.get("ok"),
    )

    # empty
    arm_empty = _curl_probe(
        fp,
        access_token,
        proxy,
        cookie_header="",
        label="empty",
        do_sse=do_sse,
    )
    arm_empty["cookie_names"] = []
    report["arms"].append(_arm_summary(arm_empty))
    _log(
        phase="arm",
        label="empty",
        prepare=arm_empty.get("req_prepare_status"),
        ok=arm_empty.get("ok"),
    )

    report["ok"] = bool(baseline.get("ok"))
    out_path = OUT_DIR / f"cookie_strip_{int(time.time())}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(
        phase="done",
        path=str(out_path),
        cookie_count=len(cookie_names),
        arms=len(report["arms"]),
        baseline_ok=baseline.get("ok"),
        ok=report["ok"],
    )
    return 0 if warm.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
