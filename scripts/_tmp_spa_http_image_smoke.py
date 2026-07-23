#!/usr/bin/env python3
"""HTTP image smoke: prepare + conduit + /f/conversation via Clash."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from curl_cffi import requests  # noqa: E402

from scripts._tmp_spa_http_repro_aligned import (  # noqa: E402
    BASE,
    PROXY,
    SECRET,
    _fp,
    _hdr,
    _log,
    _requirements,
)
from services.openai_backend_api import iter_sse_payloads_until_first_payload  # noqa: E402
from services.protocol.chatgpt_web_request import (  # noqa: E402
    build_image_prepare_body,
    build_image_start_body,
    build_image_start_headers,
    require_conduit_token,
)
from utils.helper import ensure_ok  # noqa: E402

OUT = ROOT / "data" / "runlogs" / "spa_repro"


def once(token: str, fp: dict) -> dict:
    sess = requests.Session(impersonate=fp["impersonate"], proxy=PROXY, verify=False, timeout=90)
    req = _requirements(sess, fp, token)
    tz = "Asia/Tokyo"
    prompt = "a simple flat blue circle icon, no text"
    prep_path = "/backend-api/f/conversation/prepare"
    prep_body = build_image_prepare_body(prompt, "auto", timezone=tz, timezone_offset=-540)
    r = sess.post(BASE + prep_path, headers=_hdr(fp, prep_path, token), json=prep_body, timeout=60)
    ensure_ok(r, prep_path)
    conduit = require_conduit_token(r.json().get("conduit_token"))
    path = "/backend-api/f/conversation"
    body = build_image_start_body(prompt, "auto", timezone=tz, timezone_offset=-540)
    headers = _hdr(fp, path, token, build_image_start_headers(req, conduit))
    resp = sess.post(BASE + path, headers=headers, json=body, timeout=180, stream=True)
    ensure_ok(resp, path)
    cid = ""
    chunks = 0

    def ready(payload: str) -> bool:
        return bool(re.search(r'"conversation_id"\s*:\s*"[^"]+"', payload))

    for payload in iter_sse_payloads_until_first_payload(
        resp, 90, ready_predicate=ready, post_ready_timeout_secs=75
    ):
        chunks += 1
        if not cid:
            m = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
            if m:
                cid = m.group(1)
        if "image_gen" in payload or "file_id" in payload or "file-" in payload:
            break
        if chunks >= 30 and cid:
            break
    try:
        resp.close()
    except Exception:
        pass
    return {"ok": bool(cid or chunks), "chunks": chunks, "conversation_id": cid}


def main() -> int:
    secret = json.loads(SECRET.read_text(encoding="utf-8"))
    token = str(secret.get("access_token") or "").strip()
    fp = _fp(secret)
    _log(phase="image_start", proxy=PROXY, email=secret.get("email"))
    last = None
    for i in range(1, 8):
        try:
            out = once(token, fp)
            _log(phase="image_result", **out)
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / "http_image_ok.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
            return 0 if out.get("ok") else 1
        except Exception as exc:
            last = exc
            msg = str(exc)
            retryable = any(x in msg for x in ("TLS", "OPENSSL", "curl: (35)", "Recv failure", "Connection"))
            _log(phase="image_retry", attempt=i, error=msg[:220], retryable=retryable)
            if not retryable:
                break
            time.sleep(0.8 * i)
    _log(phase="image_fail", error=str(last)[:400])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
