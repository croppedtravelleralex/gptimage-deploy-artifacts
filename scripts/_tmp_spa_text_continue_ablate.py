#!/usr/bin/env python3
"""SPA text continue (N=3) + field ablation via Clash curl_cffi.

Fixed egress: http://127.0.0.1:7897
Secret: data/runlogs/spa_repro/qaflow_secret.json (gitignore; never dump token/cookies)

Phases:
  1) continue N rounds: prepare + /f/conversation; rounds 2..N carry conversation_id
     + parent_message_id parsed from SSE
  2) after round-1 success, drop SPA chat fields one-by-one and send a short turn;
     record whether status==200 and SSE chunks arrived

Usage:
  python scripts/_tmp_spa_text_continue_ablate.py
  python scripts/_tmp_spa_text_continue_ablate.py --rounds 3 --skip-ablate
  python scripts/_tmp_spa_text_continue_ablate.py --proxy http://127.0.0.1:7897
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any

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
from utils.helper import anonymize_token, ensure_ok, iter_sse_payloads, new_uuid  # noqa: E402
from utils.pow import build_legacy_requirements_token, build_proof_token, parse_pow_resources  # noqa: E402
from utils.turnstile import solve_turnstile_token  # noqa: E402

SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
OUT_DIR = ROOT / "data" / "runlogs" / "spa_repro" / "bench3"
DEFAULT_PROXY = "http://127.0.0.1:7897"
BASE = "https://chatgpt.com"
TZ = "Asia/Tokyo"
TZ_OFFSET = -540

# Fields removed one-at-a-time from a successful SPA chat body (docs/19 §B ablation).
ABLATION_FIELDS = (
    "supports_buffering",
    "supported_encodings",
    "enable_message_followups",
    "force_parallel_switch",
    "paragen_cot_summary_display_override",
    "client_prepare_state",
    "client_contextual_info",
    "system_hints",
    "timezone",
    "timezone_offset_min",
)


def _log(**kw: Any) -> None:
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


def _is_tls_error(exc: BaseException) -> bool:
    msg = str(exc)
    return any(
        x in msg
        for x in ("TLS", "OPENSSL", "curl: (35)", "SSLEOF", "Recv failure", "Connection", "SSL")
    )


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
        raise RuntimeError(f"missing_requirements_token:{type(data).__name__}")
    return _Req(tok, proof, turnstile, str(data.get("so_token") or ""))


def _parse_sse_ids(payloads: list[str]) -> tuple[str, str]:
    """Extract conversation_id + latest assistant/tool message id for parent_message_id."""
    cid = ""
    parent = ""
    for raw in payloads:
        try:
            event = json.loads(raw)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        cid = str(event.get("conversation_id") or cid).strip() or cid
        value = event.get("v")
        if isinstance(value, dict):
            cid = str(value.get("conversation_id") or cid).strip() or cid
        message = event.get("message")
        if not isinstance(message, dict) and isinstance(value, dict):
            message = value.get("message")
        if isinstance(message, dict):
            msg_id = str(message.get("id") or "").strip()
            author = message.get("author") or {}
            role = str(author.get("role") or "").strip().lower()
            if msg_id and role in {"assistant", "tool"}:
                parent = msg_id
    return cid, parent


def _user_message(text: str) -> dict[str, Any]:
    return {
        "id": new_uuid(),
        "author": {"role": "user"},
        "content": {"content_type": "text", "parts": [text]},
    }


def _turn(
    sess: requests.Session,
    fp: dict,
    token: str,
    *,
    prompt: str,
    conversation_id: str = "",
    parent_message_id: str = "",
    body_override: dict[str, Any] | None = None,
    max_chunks: int = 12,
) -> dict[str, Any]:
    req = _requirements(sess, fp, token)
    prep_path = "/backend-api/f/conversation/prepare"
    prep_body = build_text_prepare_body(
        prompt,
        "auto",
        timezone=TZ,
        timezone_offset=TZ_OFFSET,
        conversation_id=conversation_id,
        parent_message_id=parent_message_id,
    )
    prep = sess.post(BASE + prep_path, headers=_hdr(fp, prep_path, token), json=prep_body, timeout=60)
    prep_status = int(prep.status_code or 0)
    conduit = False
    if prep_status == 200:
        try:
            conduit = bool((prep.json() or {}).get("conduit_token"))
        except Exception:
            conduit = False
    else:
        return {
            "ok": False,
            "prepare_status": prep_status,
            "sse_status": 0,
            "sse_chunks": 0,
            "conversation_id": conversation_id,
            "parent_message_id": parent_message_id,
            "error": f"prepare_status={prep_status}",
            "body_prefix": (prep.text or "")[:180],
        }

    path = "/backend-api/f/conversation"
    if body_override is not None:
        body = body_override
    else:
        body = build_chat_body(
            [_user_message(prompt)],
            "auto",
            timezone=TZ,
            timezone_offset=TZ_OFFSET,
            history_and_training_disabled=False,
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
        )
    headers = _hdr(fp, path, token, build_chat_headers(req))
    resp = sess.post(BASE + path, headers=headers, json=body, timeout=180, stream=True)
    sse_status = int(resp.status_code or 0)
    chunks: list[str] = []
    if 200 <= sse_status < 300:
        for payload in iter_sse_payloads(resp):
            chunks.append(payload)
            if len(chunks) >= max_chunks:
                break
    else:
        err_prefix = (resp.text or "")[:180]
        try:
            resp.close()
        except Exception:
            pass
        return {
            "ok": False,
            "prepare_status": prep_status,
            "sse_status": sse_status,
            "sse_chunks": 0,
            "conduit_from_prepare": conduit,
            "conversation_id": conversation_id,
            "parent_message_id": parent_message_id,
            "error": f"sse_status={sse_status}",
            "body_prefix": err_prefix,
        }
    try:
        resp.close()
    except Exception:
        pass
    next_cid, next_parent = _parse_sse_ids(chunks)
    return {
        "ok": bool(chunks),
        "prepare_status": prep_status,
        "sse_status": sse_status,
        "sse_chunks": len(chunks),
        "conduit_from_prepare": conduit,
        "conversation_id": next_cid or conversation_id,
        "parent_message_id": next_parent or parent_message_id,
        "prefix": (chunks[0][:160] if chunks else ""),
    }


def _with_tls_retry(
    fp: dict,
    token: str,
    proxy: str,
    fn,
    *,
    attempts: int = 8,
) -> dict[str, Any]:
    last: dict[str, Any] = {"ok": False, "error": "no_attempt"}
    for attempt in range(1, attempts + 1):
        try:
            sess = requests.Session(
                impersonate=fp["impersonate"],
                proxy=proxy,
                verify=False,
                timeout=90,
            )
            try:
                result = fn(sess)
            finally:
                try:
                    sess.close()
                except Exception:
                    pass
            if isinstance(result, dict):
                result["attempt"] = attempt
            return result
        except Exception as exc:
            retryable = _is_tls_error(exc)
            _log(phase="retry", attempt=attempt, error=str(exc)[:200], retryable=retryable)
            last = {"ok": False, "error": str(exc)[:400], "attempt": attempt}
            if not retryable:
                return last
            time.sleep(0.8 * attempt)
    return last


def _ablate_body(base: dict[str, Any], drop_field: str, prompt: str) -> dict[str, Any]:
    body = copy.deepcopy(base)
    body["messages"] = [_user_message(prompt)]
    body.pop(drop_field, None)
    return body


def _safe_out(meta: dict[str, Any]) -> dict[str, Any]:
    """Strip secrets from persisted JSON."""
    out = copy.deepcopy(meta)
    out.pop("access_token", None)
    out.pop("cookie", None)
    out.pop("cookies", None)
    if "token_fp" not in out and "email" in out:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SPA text continue + field ablation (Clash)")
    ap.add_argument("--proxy", default=DEFAULT_PROXY)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--skip-ablate", action="store_true")
    ap.add_argument("--secret", type=Path, default=SECRET)
    args = ap.parse_args()

    secret = json.loads(args.secret.read_text(encoding="utf-8"))
    token = str(secret.get("access_token") or "").strip()
    if not token:
        _log(ok=False, error="missing_access_token")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fp = _fp(secret)
    proxy = str(args.proxy)
    rounds = max(1, int(args.rounds))

    report: dict[str, Any] = {
        "mode": "text_continue_ablate",
        "proxy": proxy,
        "email": secret.get("email"),
        "token_fp": anonymize_token(token),
        "rounds_requested": rounds,
        "continue_turns": [],
        "ablation": [],
        "ok": False,
    }
    _log(phase="start", proxy=proxy, email=secret.get("email"), rounds=rounds)

    cid = ""
    parent = ""
    continue_ok = True
    for i in range(1, rounds + 1):
        prompt = f"Reply with exactly: R{i}"
        _log(phase="continue_start", round=i, has_cid=bool(cid), has_parent=bool(parent))

        def _run(sess: requests.Session, _prompt=prompt, _cid=cid, _parent=parent) -> dict[str, Any]:
            return _turn(
                sess,
                fp,
                token,
                prompt=_prompt,
                conversation_id=_cid,
                parent_message_id=_parent,
            )

        turn = _with_tls_retry(fp, token, proxy, _run)
        report["continue_turns"].append({"round": i, "prompt": prompt, **turn})
        _log(
            phase="continue_done",
            round=i,
            ok=turn.get("ok"),
            prepare_status=turn.get("prepare_status"),
            sse_status=turn.get("sse_status"),
            sse_chunks=turn.get("sse_chunks"),
            conversation_id=str(turn.get("conversation_id") or "")[:36],
        )
        if not turn.get("ok"):
            continue_ok = False
            break
        cid = str(turn.get("conversation_id") or cid)
        parent = str(turn.get("parent_message_id") or parent)
        if i < rounds and (not cid or not parent):
            _log(phase="continue_ids_missing", round=i, cid=bool(cid), parent=bool(parent))
            continue_ok = False
            break

    report["continue_ok"] = continue_ok

    if continue_ok and not args.skip_ablate:
        # Baseline body from builders (same shape as round 1), then drop fields.
        base_body = build_chat_body(
            [_user_message("Reply with exactly: ABLATE")],
            "auto",
            timezone=TZ,
            timezone_offset=TZ_OFFSET,
            history_and_training_disabled=False,
        )
        for field in ABLATION_FIELDS:
            prompt = f"Reply with exactly: DROP_{field[:12]}"
            dropped = _ablate_body(base_body, field, prompt)
            _log(phase="ablate_start", drop_field=field)

            def _run_ab(sess: requests.Session, _prompt=prompt, _body=dropped) -> dict[str, Any]:
                return _turn(
                    sess,
                    fp,
                    token,
                    prompt=_prompt,
                    body_override=_body,
                    max_chunks=6,
                )

            result = _with_tls_retry(fp, token, proxy, _run_ab, attempts=5)
            entry = {
                "drop_field": field,
                "ok": bool(result.get("ok")),
                "prepare_status": result.get("prepare_status"),
                "sse_status": result.get("sse_status"),
                "sse_chunks": result.get("sse_chunks"),
                "error": result.get("error"),
                "attempt": result.get("attempt"),
            }
            report["ablation"].append(entry)
            _log(phase="ablate_done", **entry)

    report["ok"] = bool(continue_ok)
    report["ablation_ok_count"] = sum(1 for x in report["ablation"] if x.get("ok"))
    report["ablation_total"] = len(report["ablation"])

    out_path = OUT_DIR / f"text_continue_ablate_{int(time.time())}.json"
    out_path.write_text(
        json.dumps(_safe_out(report), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _log(
        phase="done",
        path=str(out_path),
        ok=report["ok"],
        continue_ok=continue_ok,
        ablation_ok_count=report["ablation_ok_count"],
        ablation_total=report["ablation_total"],
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
