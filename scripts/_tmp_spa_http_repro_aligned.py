#!/usr/bin/env python3
"""SPA-aligned HTTP text repro via Clash (prepare + /f/conversation) with TLS retries."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
from utils.helper import ensure_ok, iter_sse_payloads, new_uuid  # noqa: E402
from utils.pow import build_legacy_requirements_token, build_proof_token, parse_pow_resources  # noqa: E402
from utils.turnstile import solve_turnstile_token  # noqa: E402

SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
OUT = ROOT / "data" / "runlogs" / "spa_repro"
PROXY = "http://127.0.0.1:7897"
BASE = "https://chatgpt.com"


def _log(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


class _Req:
    def __init__(self, token: str, proof: str = "", turnstile: str = "", so: str = ""):
        self.token = token
        self.proof_token = proof
        self.turnstile_token = turnstile
        self.so_token = so


def _fp(secret: dict) -> dict:
    fp = secret.get("fp") if isinstance(secret.get("fp"), dict) else {}
    return {
        "user-agent": fp.get("user-agent")
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "impersonate": fp.get("impersonate") or "chrome131",
        "oai-device-id": fp.get("oai-device-id") or new_uuid(),
        "oai-session-id": fp.get("oai-session-id") or new_uuid(),
        "accept-language": fp.get("accept-language") or "en-US,en;q=0.9",
    }


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


def _requirements(sess: requests.Session, fp: dict, token: str) -> _Req:
    home = sess.get(BASE + "/", headers={"User-Agent": fp["user-agent"]}, timeout=45)
    scripts, build = parse_pow_resources(home.text or "")
    p_token = build_legacy_requirements_token(fp["user-agent"], scripts, build)
    prep_path = "/backend-api/sentinel/chat-requirements/prepare"
    prep = sess.post(
        BASE + prep_path,
        headers=_hdr(fp, prep_path, token),
        json={"p": p_token},
        timeout=45,
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
        turnstile = solve_turnstile_token(ts["dx"], p_token) or ""
    fin_path = "/backend-api/sentinel/chat-requirements/finalize"
    fin = sess.post(
        BASE + fin_path,
        headers=_hdr(fp, fin_path, token),
        json={
            "prepare_token": prep_data.get("prepare_token", ""),
            "proofofwork": proof,
            "turnstile": turnstile,
        },
        timeout=45,
    )
    ensure_ok(fin, "chat_requirements_finalize")
    data = fin.json()
    tok = str(data.get("token") or "")
    if not tok:
        raise RuntimeError(f"missing_requirements_token:{data}")
    return _Req(tok, proof, turnstile, str(data.get("so_token") or ""))


def run_text(token: str, secret: dict) -> dict:
    fp = _fp(secret)
    last_err = None
    for attempt in range(1, 8):
        try:
            sess = requests.Session(impersonate=fp["impersonate"], proxy=PROXY, verify=False, timeout=90)
            req = _requirements(sess, fp, token)
            tz = "Asia/Tokyo"
            prep_path = "/backend-api/f/conversation/prepare"
            prep_body = build_text_prepare_body("Reply with exactly: PONG", "auto", timezone=tz, timezone_offset=-540)
            prep = sess.post(BASE + prep_path, headers=_hdr(fp, prep_path, token), json=prep_body, timeout=60)
            ensure_ok(prep, prep_path)
            path = "/backend-api/f/conversation"
            body = build_chat_body(
                [
                    {
                        "id": new_uuid(),
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["Reply with exactly: PONG"]},
                    }
                ],
                "auto",
                timezone=tz,
                timezone_offset=-540,
                history_and_training_disabled=False,
            )
            headers = _hdr(fp, path, token, build_chat_headers(req))
            resp = sess.post(BASE + path, headers=headers, json=body, timeout=180, stream=True)
            ensure_ok(resp, path)
            chunks = []
            for payload in iter_sse_payloads(resp):
                chunks.append(payload)
                if len(chunks) >= 8:
                    break
            try:
                resp.close()
            except Exception:
                pass
            return {
                "ok": bool(chunks),
                "sse_chunks": len(chunks),
                "prefix": (chunks[0][:200] if chunks else ""),
                "attempt": attempt,
            }
        except Exception as exc:
            last_err = exc
            msg = str(exc)
            retryable = any(x in msg for x in ("TLS", "OPENSSL", "curl: (35)", "Recv failure", "Connection"))
            _log(phase="retry", attempt=attempt, error=msg[:200], retryable=retryable)
            if not retryable:
                break
            time.sleep(0.8 * attempt)
    return {"ok": False, "error": str(last_err)[:400]}


def main() -> int:
    secret = json.loads(SECRET.read_text(encoding="utf-8"))
    token = str(secret.get("access_token") or "").strip()
    if not token:
        _log(ok=False, error="missing_access_token")
        return 2
    _log(phase="start", proxy=PROXY, email=secret.get("email"))
    result = run_text(token, secret)
    _log(phase="text_result", **result)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"http_repro_aligned_{int(time.time())}.json"
    path.write_text(json.dumps({"proxy": PROXY, "email": secret.get("email"), "text": result}, indent=2), encoding="utf-8")
    _log(phase="done", path=str(path), ok=bool(result.get("ok")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
