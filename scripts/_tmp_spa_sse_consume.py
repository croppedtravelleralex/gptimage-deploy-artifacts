#!/usr/bin/env python3
"""Consume SPA SSE fully for one account; locate hang after prepare."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("GPTIMAGE_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT))

# reuse diag helpers by importing after path setup
import importlib.util

spec = importlib.util.spec_from_file_location("spa_sse_diag", ROOT / "scripts" / "_tmp_spa_sse_diag.py")
assert spec and spec.loader
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)

EMAIL = os.environ.get("DIAG_EMAIL", "qaflowgq5wyuxhe9@proton.me").strip().lower()
WALL = float(os.environ.get("DIAG_SSE_WALL", "90"))


def main() -> int:
    account = diag.load_account()
    token = str(account.get("access_token") or "").strip()
    proxy = str(account.get("proxy") or "").strip()
    fp = diag.fp_from(account)
    diag.log(phase="account", email=account.get("email"), quota=account.get("quota"), egress=account.get("proxy_egress_ip"))

    sess = diag.requests.Session(impersonate=fp["impersonate"], verify=False, timeout=60, proxy=proxy)
    req = diag.requirements(sess, fp, token)
    prep_path = "/backend-api/f/conversation/prepare"
    prep = sess.post(
        diag.BASE + prep_path,
        headers=diag.hdr(fp, prep_path, token),
        json=diag.prepare_body(diag.PROMPT),
        timeout=45,
    )
    diag.ensure_ok(prep, prep_path)
    conduit = str((prep.json() or {}).get("conduit_token") or "").strip()
    diag.log(phase="prepare_ok", conduit=bool(conduit))

    path = "/backend-api/f/conversation"
    body = diag.chat_body(diag.PROMPT, system_hints=[])
    headers = diag.hdr(fp, path, token, diag.sentinel_headers(req))
    headers["Accept"] = "text/event-stream"
    # intentionally NO conduit — same as failing bench
    t0 = time.time()
    resp = sess.post(
        diag.BASE + path,
        headers=headers,
        json=body,
        timeout=(20, 60),
        stream=True,
    )
    status = int(resp.status_code or 0)
    ctype = str((resp.headers or {}).get("content-type") or "")
    diag.log(phase="sse_headers", status=status, content_type=ctype, header_ms=int((time.time() - t0) * 1000))
    if status >= 400:
        text = (resp.text or "")[:400]
        diag.log(phase="sse_http_error", body=text)
        return 2

    chunks = 0
    cid = ""
    has_image_gen = False
    file_ids: list[str] = []
    first_event_ms = None
    image_gen_ms = None
    done = False
    last_payload = ""
    timed_out = False
    try:
        for line in resp.iter_lines():
            if time.time() - t0 >= WALL:
                timed_out = True
                break
            if not line:
                continue
            text = line.decode("utf-8", errors="ignore") if isinstance(line, bytes) else str(line)
            if first_event_ms is None:
                first_event_ms = int((time.time() - t0) * 1000)
                diag.log(phase="sse_first_event", ms=first_event_ms, line=text[:160])
            if not text.startswith("data:"):
                continue
            payload = text[5:].strip()
            if payload == "[DONE]":
                done = True
                break
            chunks += 1
            last_payload = payload[:240]
            if not cid:
                m = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
                if m:
                    cid = m.group(1)
                    diag.log(phase="sse_cid", ms=int((time.time() - t0) * 1000), cid=cid)
            if "image_gen" in payload and not has_image_gen:
                has_image_gen = True
                image_gen_ms = int((time.time() - t0) * 1000)
                diag.log(phase="sse_image_gen", ms=image_gen_ms)
            for m in re.finditer(r'file-service://(file-[A-Za-z0-9_-]+)', payload):
                if m.group(1) not in file_ids:
                    file_ids.append(m.group(1))
            for m in re.finditer(r'"file_id"\s*:\s*"(file-[A-Za-z0-9_-]+)"', payload):
                if m.group(1) not in file_ids:
                    file_ids.append(m.group(1))
            if has_image_gen and (file_ids or chunks >= 40):
                break
            if chunks >= 120:
                break
            if chunks in (1, 5, 10, 20, 40, 80):
                diag.log(phase="sse_progress", chunks=chunks, ms=int((time.time() - t0) * 1000), has_image_gen=has_image_gen, files=len(file_ids))
    finally:
        try:
            resp.close()
        except Exception:
            pass

    out = {
        "elapsed_ms": int((time.time() - t0) * 1000),
        "status": status,
        "first_event_ms": first_event_ms,
        "image_gen_ms": image_gen_ms,
        "chunks": chunks,
        "cid": cid,
        "has_image_gen": has_image_gen,
        "file_ids": file_ids[:8],
        "done": done,
        "wall_timeout": timed_out,
        "last_payload": last_payload,
    }
    diag.log(phase="sse_consume_done", **out)
    path_out = ROOT / "data" / "runlogs" / "spa_repro" / f"sse-consume-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path_out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    diag.log(phase="wrote", path=str(path_out))
    return 0 if has_image_gen else 3


if __name__ == "__main__":
    raise SystemExit(main())
