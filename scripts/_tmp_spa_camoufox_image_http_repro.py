#!/usr/bin/env python3
"""SPA-shaped image HTTP repro via Camoufox request API (browser TLS + Clash).

HAR: image = text shape (system_hints=[], no X-Conduit-Token / picture_v2).
Uses local PoW/turnstile; HTTP via Playwright request (Firefox stack).
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camoufox.sync_api import Camoufox  # noqa: E402

from services.openai_backend_api import (  # noqa: E402
    DEFAULT_CLIENT_BUILD_NUMBER,
    DEFAULT_CLIENT_VERSION,
)
from services.protocol.chatgpt_web_request import (  # noqa: E402
    build_chat_body,
    build_chat_headers,
    build_text_prepare_body,
)
from utils.helper import new_uuid  # noqa: E402
from utils.pow import (  # noqa: E402
    build_legacy_requirements_token,
    build_proof_token,
    parse_pow_resources,
)
from utils.turnstile import solve_turnstile_token  # noqa: E402

SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
OUT = ROOT / "data" / "runlogs" / "spa_repro"
PROXY = "http://127.0.0.1:7897"
BASE = "https://chatgpt.com"
PROMPT = "Create an image of a simple flat blue circle icon on white background, no text"


def _log(**kw) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def _proxy_dict(proxy: str) -> dict[str, str]:
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    return {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"}


class _Req:
    def __init__(self, token: str, proof: str = "", turnstile: str = "", so: str = ""):
        self.token = token
        self.proof_token = proof
        self.turnstile_token = turnstile
        self.so_token = so


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
    }
    if extra:
        h.update(extra)
    return h


def main() -> int:
    secret = json.loads(SECRET.read_text(encoding="utf-8"))
    session_token = str(secret.get("chatgpt_session_token") or "").strip()
    access_token = str(secret.get("access_token") or "").strip()
    if not session_token or not access_token:
        _log(ok=False, error="missing_session_or_access_token")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    fp_src = secret.get("fp") if isinstance(secret.get("fp"), dict) else {}
    fp = {
        "user-agent": fp_src.get("user-agent")
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
        "oai-device-id": fp_src.get("oai-device-id") or new_uuid(),
        "oai-session-id": new_uuid(),
        "accept-language": fp_src.get("accept-language") or "en-US,en;q=0.9",
    }
    tz, offset = "Asia/Tokyo", -540

    # geoip=False: Clash TLS currently breaks Camoufox public_ip() probe.
    with Camoufox(headless=True, proxy=_proxy_dict(PROXY), geoip=False) as browser:
        ctx = browser.new_context()
        ctx.add_cookies(
            [
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": session_token,
                    "domain": "chatgpt.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            ]
        )
        page = ctx.new_page()
        api = ctx.request
        _log(phase="goto")
        home = api.get(BASE + "/", timeout=120000)
        _log(phase="home", status=home.status, bytes=len(home.text()))
        if home.status >= 400:
            return 1
        scripts, build = parse_pow_resources(home.text() or "")
        p_token = build_legacy_requirements_token(fp["user-agent"], scripts, build)

        prep = api.post(
            BASE + "/backend-api/sentinel/chat-requirements/prepare",
            headers=_hdr(fp, "/backend-api/sentinel/chat-requirements/prepare", access_token),
            data=json.dumps({"p": p_token}),
            timeout=90000,
        )
        _log(phase="req_prepare", status=prep.status)
        if prep.status != 200:
            _log(error=prep.text()[:300])
            return 1
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
        _log(phase="req_finalize", status=fin.status)
        if fin.status != 200:
            _log(error=fin.text()[:300])
            return 1
        fin_data = fin.json()
        req_tok = str(fin_data.get("token") or "")
        if not req_tok:
            _log(ok=False, error="missing_requirements_token", body=str(fin_data)[:300])
            return 1
        req = _Req(req_tok, proof, turnstile, str(fin_data.get("so_token") or ""))
        # Skip page.goto — Clash often interrupts UI navigation while APIRequest still works.
        at = access_token
        sess = api.get(BASE + "/api/auth/session", timeout=60000)
        try:
            sess_json = sess.json() if sess.status == 200 else {}
        except Exception:
            sess_json = {}
        email = ((sess_json or {}).get("user") or {}).get("email")
        if (sess_json or {}).get("accessToken"):
            at = str(sess_json["accessToken"]).strip()
        _log(phase="session", status=sess.status, email=email, has_at=bool(at))
        if sess.status == 200 and not email:
            _log(ok=False, error="not_logged_in", body=str(sess_json)[:240])
            return 1

        conv_prep_body = build_text_prepare_body(PROMPT, "auto", timezone=tz, timezone_offset=offset)
        conv_prep = api.post(
            BASE + "/backend-api/f/conversation/prepare",
            headers=_hdr(fp, "/backend-api/f/conversation/prepare", at),
            data=json.dumps(conv_prep_body),
            timeout=90000,
        )
        _log(phase="conversation_prepare", status=conv_prep.status, body=conv_prep.text()[:200])
        if conv_prep.status != 200:
            (OUT / "http_image_camoufox_fail.json").write_text(
                json.dumps({"stage": "prepare", "status": conv_prep.status, "body": conv_prep.text()[:800]}, indent=2),
                encoding="utf-8",
            )
            return 1
        conduit = bool((conv_prep.json() or {}).get("conduit_token"))

        body = build_chat_body(
            [
                {
                    "id": new_uuid(),
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [PROMPT]},
                }
            ],
            "auto",
            timezone=tz,
            timezone_offset=offset,
            history_and_training_disabled=False,
        )
        body["system_hints"] = []  # SPA HAR

        headers = _hdr(fp, "/backend-api/f/conversation", at, build_chat_headers(req))
        headers["Accept"] = "text/event-stream"
        # SPA image HAR: no X-Conduit-Token

        resp = api.post(
            BASE + "/backend-api/f/conversation",
            headers=headers,
            data=json.dumps(body),
            timeout=180000,
        )
        text = resp.text()
        has_image_gen = "image_gen" in text
        has_file = bool(re.search(r"file-[A-Za-z0-9_-]+", text))
        cid_m = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', text)
        out = {
            "ok": resp.status == 200 and (has_image_gen or has_file or bool(cid_m)),
            "status": resp.status,
            "conversation_id": cid_m.group(1) if cid_m else "",
            "has_image_gen": has_image_gen,
            "has_file": has_file,
            "conduit_from_prepare": conduit,
            "sent_x_conduit": False,
            "system_hints": [],
            "sse_len": len(text),
            "sse_prefix": text[:500].replace("\n", " "),
        }
        _log(
            phase="conversation",
            status=out["status"],
            ok=out["ok"],
            has_image_gen=has_image_gen,
            has_file=has_file,
            cid=out["conversation_id"],
            prefix=out["sse_prefix"][:180],
        )
        path = OUT / ("http_image_camoufox_ok.json" if out["ok"] else "http_image_camoufox_fail.json")
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        _log(phase="done", path=str(path), ok=out["ok"])
        return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
