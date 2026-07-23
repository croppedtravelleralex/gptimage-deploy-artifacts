#!/usr/bin/env python3
"""SPA HTTP image gen+download via Camoufox through local SOCKS → panda egress.

Use case: panda direct curl_cffi got CF 403 on chat-requirements/prepare;
test whether Camoufox (Firefox TLS) via SSH -D to panda can pass.

Prereq (local):
  ssh -D 18080 -N panda

Usage:
  python scripts/_tmp_spa_camoufox_via_panda_socks.py
  python scripts/_tmp_spa_camoufox_via_panda_socks.py --proxy socks5://127.0.0.1:18080
"""
from __future__ import annotations

import argparse
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
from utils.helper import new_uuid  # noqa: E402
from utils.pow import (  # noqa: E402
    build_legacy_requirements_token,
    build_proof_token,
    parse_pow_resources,
)
from utils.turnstile import solve_turnstile_token  # noqa: E402

SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
OUT = ROOT / "data" / "runlogs" / "spa_repro" / "bench3"
BASE = "https://chatgpt.com"
EXPECTED_PANDA_IP = "43.156.233.219"
MEDIUM_PROMPT = (
    "Create a medium-detail digital illustration of a rainy Tokyo side street at dusk: "
    "neon shop signs reflecting on wet asphalt, a bicycle parked under a red awning, "
    "warm interior lights spilling onto the sidewalk, cinematic atmosphere, soft depth of field, "
    "no text, no watermark, no logos"
)


def _log(**kw) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def _proxy_dict(proxy: str) -> dict[str, str]:
    parsed = urlparse(proxy if "://" in proxy else f"socks5://{proxy}")
    scheme = parsed.scheme or "socks5"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 18080
    return {"server": f"{scheme}://{host}:{port}"}


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
    fp.setdefault("oai-device-id", new_uuid())
    fp.setdefault("oai-session-id", new_uuid())
    fp.setdefault("accept-language", "en-US,en;q=0.9")
    fp.setdefault("sec-ch-ua", '"Not A(Brand";v="8", "Chromium";v="132"')
    fp.setdefault("sec-ch-ua-mobile", "?0")
    fp.setdefault("sec-ch-ua-platform", '"Windows"')
    fp.setdefault("sec-ch-ua-arch", '"x86"')
    fp.setdefault("sec-ch-ua-bitness", '"64"')
    fp.setdefault("sec-ch-ua-full-version", '"132.0.0.0"')
    fp.setdefault("sec-ch-ua-full-version-list", '"Not A(Brand";v="10.0.0.0", "Chromium";v="132.0.0.0"')
    fp.setdefault("sec-ch-ua-platform-version", '"15.0.0"')
    return fp


def _hdr(fp: dict, token: str, extra: dict | None = None) -> dict:
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


def _prepare_body(prompt: str, tz: str, offset: int) -> dict:
    msg_id = new_uuid()
    return {
        "action": "next",
        "parent_message_id": "client-created-root",
        "model": "auto",
        "timezone_offset_min": offset,
        "timezone": tz,
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": [],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_prepare_state": "none",
        "client_prepare_dispatch": "debounced",
        "client_prepare_source": "composer_editor_state",
        "partial_query": {
            "id": msg_id,
            "author": {"role": "user"},
            "content": {"content_type": "text", "parts": [prompt]},
        },
        "client_contextual_info": {
            "app_name": "chatgpt.com",
            "is_web_push_capable": True,
            "is_web_push_enabled": False,
        },
    }


def _chat_body(prompt: str, tz: str, offset: int) -> dict:
    return {
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
        "timezone_offset_min": offset,
        "timezone": tz,
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": [],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_prepare_state": "none",
        "enable_message_followups": True,
        "force_parallel_switch": "auto",
        "paragen_cot_summary_display_override": "allow",
        "client_contextual_info": {
            "app_name": "chatgpt.com",
            "is_web_push_capable": True,
            "is_web_push_enabled": False,
        },
    }


def _chat_headers(req: _Req) -> dict:
    h = {
        "OpenAI-Sentinel-Chat-Requirements-Token": req.token,
        "OpenAI-Sentinel-Chat-Requirements-Prepare-Token": req.token,
    }
    if req.proof_token:
        h["OpenAI-Sentinel-Proof-Token"] = req.proof_token
    if req.turnstile_token:
        h["OpenAI-Sentinel-Turnstile-Token"] = req.turnstile_token
    return h


def _image_magic(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="socks5://127.0.0.1:18080")
    ap.add_argument("--prompt", default=MEDIUM_PROMPT)
    ap.add_argument("--expect-ip", default=EXPECTED_PANDA_IP)
    args = ap.parse_args()

    secret = json.loads(SECRET.read_text(encoding="utf-8"))
    token = str(secret.get("access_token") or "").strip()
    if not token:
        _log(ok=False, error="missing_access_token")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    fp = _fp(secret)
    tz, offset = "Asia/Tokyo", -540
    timings: dict[str, int] = {}
    t_all = time.time()
    out: dict = {
        "mode": "local_camoufox_via_panda_socks",
        "proxy": args.proxy,
        "email": secret.get("email"),
        "protocol": "spa_text_shape_image_gen",
        "system_hints": [],
        "sent_x_conduit": False,
        "ok": False,
    }

    # geoip=False: public_ip probe often fails through SOCKS; we verify egress ourselves.
    with Camoufox(headless=True, proxy=_proxy_dict(args.proxy), geoip=False) as browser:
        ctx = browser.new_context()
        ctx.set_default_timeout(360000)
        api = ctx.request
        page = ctx.new_page()
        page.set_default_timeout(360000)

        t0 = time.time()
        try:
            eg = api.get("https://api.ipify.org?format=json", timeout=60000)
            eg_json = eg.json() if eg.status == 200 else {}
            egress_ip = str(eg_json.get("ip") or "")
        except Exception as exc:
            egress_ip = ""
            eg_json = {"error": str(exc)[:200]}
        timings["egress_ms"] = int((time.time() - t0) * 1000)
        out["egress"] = {"ok": bool(egress_ip), "ip": egress_ip, "raw": eg_json, "expect": args.expect_ip}
        _log(phase="egress", **out["egress"], ms=timings["egress_ms"])
        if args.expect_ip and egress_ip and egress_ip != args.expect_ip:
            _log(phase="egress_mismatch", got=egress_ip, expect=args.expect_ip)
            # continue anyway but flag

        # home (may CF)
        t0 = time.time()
        home = api.get(BASE + "/", timeout=120000)
        home_text = home.text() if home.status < 400 else ""
        timings["home_ms"] = int((time.time() - t0) * 1000)
        _log(phase="home", status=home.status, bytes=len(home_text), ms=timings["home_ms"])
        scripts, build = parse_pow_resources(home_text) if home_text else ([], "")
        p_token = build_legacy_requirements_token(fp["user-agent"], scripts, build)

        t0 = time.time()
        prep = api.post(
            BASE + "/backend-api/sentinel/chat-requirements/prepare",
            headers=_hdr(fp, token),
            data=json.dumps({"p": p_token}),
            timeout=90000,
        )
        _log(phase="req_prepare", status=prep.status, body=prep.text()[:180])
        if prep.status != 200:
            timings["total_ms"] = int((time.time() - t_all) * 1000)
            out["error"] = f"chat_requirements_prepare status={prep.status}"
            out["timings_ms"] = timings
            path = OUT / f"result_panda_socks_camoufox_{int(time.time())}.json"
            path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
            _log(phase="done", ok=False, path=str(path), timings=timings)
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
            headers=_hdr(fp, token),
            data=json.dumps(
                {
                    "prepare_token": prep_data.get("prepare_token", ""),
                    "proofofwork": proof,
                    "turnstile": turnstile,
                }
            ),
            timeout=90000,
        )
        timings["requirements_ms"] = int((time.time() - t0) * 1000)
        _log(phase="req_finalize", status=fin.status, ms=timings["requirements_ms"])
        if fin.status != 200:
            out["error"] = f"finalize status={fin.status} body={fin.text()[:200]}"
            timings["total_ms"] = int((time.time() - t_all) * 1000)
            out["timings_ms"] = timings
            path = OUT / f"result_panda_socks_camoufox_{int(time.time())}.json"
            path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
            _log(phase="done", ok=False, path=str(path))
            return 1
        fin_data = fin.json()
        req_tok = str(fin_data.get("token") or "")
        if not req_tok:
            out["error"] = "missing_requirements_token"
            return 1
        req = _Req(req_tok, proof, turnstile, str(fin_data.get("so_token") or ""))

        # conversation prepare with retry (SOCKS can hang)
        prep_json = {}
        for attempt in range(1, 4):
            t0 = time.time()
            try:
                conv_prep = api.post(
                    BASE + "/backend-api/f/conversation/prepare",
                    headers=_hdr(fp, token),
                    data=json.dumps(_prepare_body(args.prompt, tz, offset)),
                    timeout=90000,
                )
                timings["prepare_ms"] = int((time.time() - t0) * 1000)
                _log(
                    phase="conversation_prepare",
                    status=conv_prep.status,
                    ms=timings["prepare_ms"],
                    attempt=attempt,
                    body=conv_prep.text()[:160],
                )
                if conv_prep.status == 200:
                    prep_json = conv_prep.json() or {}
                    break
            except Exception as exc:
                timings["prepare_ms"] = int((time.time() - t0) * 1000)
                _log(phase="prepare_retry", attempt=attempt, error=str(exc)[:200])
                time.sleep(1.5 * attempt)
        else:
            out["error"] = "prepare_failed"
            timings["total_ms"] = int((time.time() - t_all) * 1000)
            out["timings_ms"] = timings
            path = OUT / f"result_panda_socks_camoufox_{int(time.time())}.json"
            path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
            return 1
        out["conduit_from_prepare"] = bool(prep_json.get("conduit_token"))

        # SSE via curl_cffi + Camoufox cookies (Playwright APIRequest cannot buffer long SSE).
        from curl_cffi import requests as cf_requests

        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies() if c.get("name"))
        headers = _hdr(fp, token, _chat_headers(req))
        headers["Accept"] = "text/event-stream"
        if cookie_header:
            headers["Cookie"] = cookie_header
        t0 = time.time()
        text_parts: list[str] = []
        status = 0
        sse_mode = "curl_cffi_socks"
        sse_err = ""
        try:
            sess = cf_requests.Session(
                impersonate=str(fp.get("impersonate") or "chrome131"),
                proxy=args.proxy.replace("socks5://", "socks5h://")
                if args.proxy.startswith("socks5://")
                else args.proxy,
                verify=False,
                timeout=300,
            )
            resp = sess.post(
                BASE + "/backend-api/f/conversation",
                headers=headers,
                json=_chat_body(args.prompt, tz, offset),
                stream=True,
                timeout=300,
            )
            status = int(resp.status_code or 0)
            for line in resp.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    s = line.decode("utf-8", errors="ignore")
                else:
                    s = str(line)
                text_parts.append(s)
                joined = "\n".join(text_parts)
                if "image_gen" in joined and (
                    "file_00000000" in joined or len(text_parts) >= 40
                ):
                    break
                if len(text_parts) >= 120:
                    break
            try:
                resp.close()
            except Exception:
                pass
            try:
                sess.close()
            except Exception:
                pass
        except Exception as exc:
            sse_err = str(exc)[:300]
            _log(phase="sse_error", error=sse_err)
        text = "\n".join(text_parts)
        timings["sse_ms"] = int((time.time() - t0) * 1000)
        has_image_gen = "image_gen" in text
        file_ids = re.findall(r'file-service://(file-[A-Za-z0-9_-]+)|"file_id"\s*:\s*"(file-[A-Za-z0-9_-]+)"', text)
        flat_files = []
        for a, b in file_ids:
            flat_files.append(a or b)
        sediment_ids = re.findall(r"sediment://([A-Za-z0-9_-]+)", text)
        for m in re.finditer(r"(file_00000000[A-Za-z0-9]+)", text):
            if m.group(1) not in sediment_ids:
                sediment_ids.append(m.group(1))
        cid_m = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', text)
        cid = cid_m.group(1) if cid_m else ""
        out.update(
            {
                "sse_status": status,
                "sse_mode": sse_mode,
                "conversation_id": cid,
                "has_image_gen": has_image_gen,
                "sse_len": len(text),
                "sse_file_ids": list(dict.fromkeys(flat_files))[:8],
                "sse_sediment_ids": list(dict.fromkeys(sediment_ids))[:8],
            }
        )
        _log(
            phase="sse_done",
            status=status,
            ms=timings["sse_ms"],
            cid=cid,
            has_image_gen=has_image_gen,
            sediment=out["sse_sediment_ids"][:2],
            mode=sse_mode,
            err=sse_err[:120] if sse_err else "",
        )
        if status != 200 or not cid:
            out["error"] = f"sse status={status} cid={cid} err={sse_err} prefix={text[:200]!r}"
            timings["total_ms"] = int((time.time() - t_all) * 1000)
            out["timings_ms"] = timings
            path = OUT / f"result_panda_socks_camoufox_{int(time.time())}.json"
            path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
            _log(phase="done", ok=False, path=str(path), timings=timings)
            return 1

        t0 = time.time()
        urls: list[str] = []
        for sid in out["sse_sediment_ids"]:
            try:
                r = api.get(
                    f"{BASE}/backend-api/conversation/{cid}/attachment/{sid}/download",
                    headers=_hdr(fp, token, {"Accept": "application/json"}),
                    timeout=90000,
                )
                if r.status == 200:
                    u = str((r.json() or {}).get("download_url") or (r.json() or {}).get("url") or "")
                    if u:
                        urls.append(u)
            except Exception as exc:
                _log(phase="attach_url_fail", id=sid, error=str(exc)[:160])
        if not urls:
            for i in range(12):
                time.sleep(3)
                try:
                    c = api.get(
                        f"{BASE}/backend-api/conversation/{cid}",
                        headers=_hdr(fp, token, {"Accept": "application/json"}),
                        timeout=60000,
                    )
                    if c.status != 200:
                        _log(phase="poll_status", attempt=i, status=c.status)
                        continue
                    blob = c.text()
                    for m in re.finditer(r"(file_00000000[A-Za-z0-9]+)", blob):
                        sid = m.group(1)
                        if sid not in out["sse_sediment_ids"]:
                            out["sse_sediment_ids"].append(sid)
                        r = api.get(
                            f"{BASE}/backend-api/conversation/{cid}/attachment/{sid}/download",
                            headers=_hdr(fp, token, {"Accept": "application/json"}),
                            timeout=90000,
                        )
                        if r.status == 200:
                            u = str((r.json() or {}).get("download_url") or (r.json() or {}).get("url") or "")
                            if u and u not in urls:
                                urls.append(u)
                    if urls:
                        break
                except Exception as exc:
                    _log(phase="poll_err", attempt=i, error=str(exc)[:160])
        timings["poll_resolve_ms"] = int((time.time() - t0) * 1000)
        out["download_urls"] = len(urls)
        _log(phase="urls_resolved", ms=timings["poll_resolve_ms"], urls=len(urls))

        t0 = time.time()
        images = []
        for u in urls:
            r = api.get(
                u,
                headers={
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Authorization": f"Bearer {token}",
                },
                timeout=180000,
            )
            data = r.body()
            kind = _image_magic(data)
            item = {"status": r.status, "bytes": len(data), "format": kind or "unknown"}
            if kind:
                p = OUT / f"panda_socks_camoufox_{int(time.time())}_{len(images)}.{'jpg' if kind == 'jpeg' else kind}"
                p.write_bytes(data)
                item["path"] = str(p)
                images.append(item)
            else:
                images.append(item)
                _log(phase="download_bad_magic", status=r.status, bytes=len(data), prefix=data[:20].hex())
        timings["download_ms"] = int((time.time() - t0) * 1000)
        out["images"] = images
        out["download_ok"] = any(i.get("format") in ("png", "jpeg", "webp") for i in images)
        out["ok"] = bool(out["download_ok"] and (has_image_gen or urls))
        timings["total_ms"] = int((time.time() - t_all) * 1000)
        out["timings_ms"] = timings
        path = OUT / f"result_panda_socks_camoufox_{int(time.time())}.json"
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        _log(phase="done", ok=out["ok"], path=str(path), timings=timings, images=images)
        _ = page
        return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
