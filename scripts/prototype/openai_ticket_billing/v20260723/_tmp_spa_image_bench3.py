#!/usr/bin/env python3
"""3-env SPA HTTP image bench: gen + download + timing/traffic.

Modes:
  local_clash      — proxy http://127.0.0.1:7897
  panda_direct     — no proxy (panda egress IP)
  panda_webshare   — account sticky Webshare

Success: SSE/poll yields image file_id AND image bytes downloaded (magic OK).
Uses SPA-aligned shape (system_hints=[], no X-Conduit-Token on SSE).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
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
from PIL import Image  # noqa: E402

from services.openai_backend_api import (  # noqa: E402
    DEFAULT_CLIENT_BUILD_NUMBER,
    DEFAULT_CLIENT_VERSION,
    OpenAIBackendAPI,
)
from utils.helper import ensure_ok, new_uuid  # noqa: E402
from utils.pow import (  # noqa: E402
    build_legacy_requirements_token,
    build_proof_token,
    parse_pow_resources,
)
from utils.turnstile import solve_turnstile_token  # noqa: E402
from services.request_shape import body_shape, header_shape  # noqa: E402


def _load_spa_bench_sse():
    try:
        from scripts.spa_bench_sse import (  # noqa: WPS433
            classify_image_sse_failure,
            consume_image_sse,
            empty_cf_observability,
            merge_propagated_cf,
        )
        return classify_image_sse_failure, consume_image_sse, empty_cf_observability, merge_propagated_cf
    except ImportError:
        import importlib.util

        sibling = Path(__file__).resolve().parent / "spa_bench_sse.py"
        spec = importlib.util.spec_from_file_location("spa_bench_sse", sibling)
        if spec is None or spec.loader is None:
            raise
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod.classify_image_sse_failure, mod.consume_image_sse, mod.empty_cf_observability, mod.merge_propagated_cf


(
    classify_image_sse_failure,
    consume_image_sse,
    empty_cf_observability,
    merge_propagated_cf,
) = _load_spa_bench_sse()

try:
    from services.protocol.chatgpt_web_request import (  # noqa: E402
        build_chat_body,
        build_chat_headers,
        build_text_prepare_body,
    )
except Exception:
    build_chat_body = None  # type: ignore[assignment]
    build_chat_headers = None  # type: ignore[assignment]
    build_text_prepare_body = None  # type: ignore[assignment]

try:
    from services.protocol.chatgpt_web_request import (  # noqa: E402
        build_image_prepare_body,
        build_image_start_body,
        build_image_start_headers,
    )
except Exception:
    build_image_prepare_body = None  # type: ignore[assignment]
    build_image_start_body = None  # type: ignore[assignment]
    build_image_start_headers = None  # type: ignore[assignment]

BASE = "https://chatgpt.com"
SECRET_DEFAULT = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
OUT_DIR = ROOT / "data" / "runlogs" / "spa_repro" / "bench3"

MEDIUM_PROMPT = (
    "Create a medium-detail digital illustration of a rainy Tokyo side street at dusk: "
    "neon shop signs reflecting on wet asphalt, a bicycle parked under a red awning, "
    "warm interior lights spilling onto the sidewalk, cinematic atmosphere, soft depth of field, "
    "no text, no watermark, no logos"
)


def _log(**kw: Any) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def _short_hash(value: object, length: int = 12) -> str:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length] if text else ""


def account_hash(access_token: object) -> str:
    """与 services.log_service._account_hash 保持一致，且不暴露 token。"""
    return _short_hash(access_token)


def proxy_hash(proxy: object) -> str:
    return _short_hash(proxy)


def _sanitize_error(value: object, limit: int = 320) -> str:
    text = str(value or value.__class__.__name__ if value is not None else "error")
    text = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", r"\1<redacted>@", text)
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", text)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<redacted-email>", text)
    text = re.sub(
        r'(?i)(access_token|refresh_token|cookie|turnstile|prepare_token)(["\']?\s*[:=]\s*["\']?)[^\s,"\'}]+',
        r"\1\2<redacted>",
        text,
    )
    return text[: max(1, int(limit))]


def _cf_classification(value: object) -> str:
    text = str(value or "").lower()
    if any(marker in text for marker in ("status=403", "http_403", "cloudflare", "cf_abort", "edge_html_block")):
        return "cf403"
    if "status=429" in text or "http_429" in text:
        return "rate_limited"
    return "none"


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp_path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def write_evidence(path: Path, result: dict[str, Any]) -> None:
    payload = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write_bytes(path, payload)


def persist_image(out_dir: Path, index: int, blob: bytes) -> dict[str, Any]:
    kind = _image_magic_ok(blob)
    width = 0
    height = 0
    if kind:
        with Image.open(io.BytesIO(blob)) as image:
            width, height = image.size
    item: dict[str, Any] = {
        "index": int(index),
        "bytes": len(blob),
        "format": kind or "unknown",
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(blob).hexdigest(),
    }
    if kind:
        ext = "jpg" if kind == "jpeg" else kind
        path_out = Path(out_dir) / f"image_{index}.{ext}"
        _atomic_write_bytes(path_out, blob)
        item["path"] = path_out.name
    return item


class TrafficMeter:
    def __init__(self) -> None:
        self.req_bytes = 0
        self.resp_bytes = 0
        self.calls = 0

    def add_req(self, body: bytes | str | None) -> None:
        if body is None:
            return
        if isinstance(body, bytes):
            self.req_bytes += len(body)
        else:
            self.req_bytes += len(str(body).encode("utf-8", errors="ignore"))

    def add_resp(self, resp: Any) -> None:
        self.calls += 1
        try:
            cl = resp.headers.get("Content-Length") or resp.headers.get("content-length")
            if cl and str(cl).isdigit():
                self.resp_bytes += int(cl)
                return
        except Exception:
            pass
        try:
            content = getattr(resp, "content", None)
            if content is not None:
                self.resp_bytes += len(content)
                return
        except Exception:
            pass
        try:
            text = getattr(resp, "text", None)
            if text is not None:
                self.resp_bytes += len(text.encode("utf-8", errors="ignore"))
        except Exception:
            pass

    def snapshot(self) -> dict[str, int]:
        return {
            "http_calls": self.calls,
            "req_bytes": self.req_bytes,
            "resp_bytes": self.resp_bytes,
            "total_bytes": self.req_bytes + self.resp_bytes,
        }


class _Req:
    def __init__(self, token: str, proof: str = "", turnstile: str = "", so: str = ""):
        self.token = token
        self.proof_token = proof
        self.turnstile_token = turnstile
        self.so_token = so


def _fallback_prepare_body(prompt: str, model: str, timezone: str, timezone_offset: int) -> dict:
    msg_id = new_uuid()
    return {
        "action": "next",
        "parent_message_id": "client-created-root",
        "model": model,
        "timezone_offset_min": timezone_offset,
        "timezone": timezone,
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


def _fallback_chat_body(prompt: str, model: str, timezone: str, timezone_offset: int) -> dict:
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
        "model": model,
        "timezone_offset_min": timezone_offset,
        "timezone": timezone,
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


def _fallback_chat_headers(req: _Req) -> dict:
    h = {
        "OpenAI-Sentinel-Chat-Requirements-Token": req.token,
        "OpenAI-Sentinel-Chat-Requirements-Prepare-Token": req.token,
    }
    if req.proof_token:
        h["OpenAI-Sentinel-Proof-Token"] = req.proof_token
    if req.turnstile_token:
        h["OpenAI-Sentinel-Turnstile-Token"] = req.turnstile_token
    return h


def _make_prepare_body(prompt: str, model: str, timezone: str, timezone_offset: int) -> dict:
    if build_text_prepare_body is not None:
        return build_text_prepare_body(prompt, model, timezone=timezone, timezone_offset=timezone_offset)
    return _fallback_prepare_body(prompt, model, timezone, timezone_offset)


def _make_chat_body(prompt: str, model: str, timezone: str, timezone_offset: int) -> dict:
    if build_chat_body is not None:
        body = build_chat_body(
            [
                {
                    "id": new_uuid(),
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [prompt]},
                }
            ],
            model,
            timezone=timezone,
            timezone_offset=timezone_offset,
            history_and_training_disabled=False,
        )
        body["system_hints"] = []
        return body
    return _fallback_chat_body(prompt, model, timezone, timezone_offset)


def _make_image_prepare_body(
    prompt: str,
    model: str,
    timezone: str,
    timezone_offset: int,
    *,
    spa_tool_path: bool,
) -> dict:
    if build_image_prepare_body is None:
        raise RuntimeError("build_image_prepare_body_unavailable")
    return build_image_prepare_body(
        prompt,
        model,
        timezone=timezone,
        timezone_offset=timezone_offset,
        spa_tool_path=spa_tool_path,
    )


def _make_image_start_body(
    prompt: str,
    model: str,
    timezone: str,
    timezone_offset: int,
    *,
    spa_tool_path: bool,
) -> dict:
    if build_image_start_body is None:
        raise RuntimeError("build_image_start_body_unavailable")
    return build_image_start_body(
        prompt,
        model,
        references=[],
        timezone=timezone,
        timezone_offset=timezone_offset,
        spa_tool_path=spa_tool_path,
    )


def _make_chat_headers(req: _Req) -> dict:
    if build_chat_headers is not None:
        return build_chat_headers(req)
    return _fallback_chat_headers(req)


def _image_magic_ok(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return ""


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


def _metered_request(sess: requests.Session, meter: TrafficMeter, method: str, url: str, **kwargs: Any):
    body = kwargs.get("data")
    if body is None and kwargs.get("json") is not None:
        body = json.dumps(kwargs["json"])
    meter.add_req(body if isinstance(body, (bytes, str)) else None)
    # also count URL roughly negligible
    fn = getattr(sess, method.lower())
    resp = fn(url, **kwargs)
    # for stream, content may be lazy — caller should mark
    if not kwargs.get("stream"):
        meter.add_resp(resp)
    return resp


def _bump_cf_obs(cf_obs: dict[str, Any], exc: BaseException, *, phase: str) -> None:
    if _cf_classification(exc) != "cf403":
        return
    if phase == "requirements":
        cf_obs["requirements_cf403"] = int(cf_obs.get("requirements_cf403") or 0) + 1
    elif phase == "start":
        cf_obs["start_cf403"] = int(cf_obs.get("start_cf403") or 0) + 1
    elif phase == "tasks":
        cf_obs["tasks_cf403"] = int(cf_obs.get("tasks_cf403") or 0) + 1


def _requirements(sess: requests.Session, meter: TrafficMeter, fp: dict, token: str) -> tuple[_Req, dict[str, Any]]:
    cf_obs = empty_cf_observability()
    home_ok = False
    home_text = ""
    home_status: int | None = None
    try:
        home = _metered_request(sess, meter, "get", BASE + "/", headers={"User-Agent": fp["user-agent"]}, timeout=45)
        home_status = int(getattr(home, "status_code", 0) or 0)
        cf_obs["home_status"] = home_status
        if home_status < 400:
            home_ok = True
            home_text = home.text or ""
        else:
            if home_status == 403:
                cf_obs["home_403_soft_fail"] = True
            _log(phase="home_soft_fail", status=home_status, note="continue_with_default_pow")
    except Exception as exc:
        _log(phase="home_soft_fail", error=str(exc)[:180], note="continue_with_default_pow")
    scripts, build = parse_pow_resources(home_text) if home_ok else ([], "")
    p_token = build_legacy_requirements_token(fp["user-agent"], scripts, build)
    prep_path = "/backend-api/sentinel/chat-requirements/prepare"
    prep = _metered_request(
        sess,
        meter,
        "post",
        BASE + prep_path,
        headers=_hdr(fp, prep_path, token),
        json={"p": p_token},
        timeout=45,
    )
    try:
        ensure_ok(prep, "chat_requirements_prepare")
    except Exception as exc:
        _bump_cf_obs(cf_obs, exc, phase="requirements")
        raise
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
    fin = _metered_request(
        sess,
        meter,
        "post",
        BASE + fin_path,
        headers=_hdr(fp, fin_path, token),
        json={
            "prepare_token": prep_data.get("prepare_token", ""),
            "proofofwork": proof,
            "turnstile": turnstile,
        },
        timeout=45,
    )
    try:
        ensure_ok(fin, "chat_requirements_finalize")
    except Exception as exc:
        _bump_cf_obs(cf_obs, exc, phase="requirements")
        raise
    data = fin.json()
    tok = str(data.get("token") or "")
    if not tok:
        raise RuntimeError(f"missing_requirements_token:{data}")
    return _Req(tok, proof, turnstile, str(data.get("so_token") or "")), merge_propagated_cf(cf_obs)


def _fp_from_secret(secret: dict) -> dict:
    fp = dict(secret.get("fp") if isinstance(secret.get("fp"), dict) else {})
    fp.setdefault(
        "user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    fp.setdefault("impersonate", "chrome131")
    fp.setdefault("oai-device-id", new_uuid())
    fp.setdefault("oai-session-id", new_uuid())
    fp.setdefault("accept-language", "en-US,en;q=0.9")
    # OpenAIBackendAPI._headers may require client hints when present in account fp.
    fp.setdefault("sec-ch-ua", '"Google Chrome";v="131", "Not?A_Brand";v="8", "Chromium";v="131"')
    fp.setdefault("sec-ch-ua-mobile", "?0")
    fp.setdefault("sec-ch-ua-platform", '"Windows"')
    fp.setdefault("sec-ch-ua-arch", '"x86"')
    fp.setdefault("sec-ch-ua-bitness", '"64"')
    fp.setdefault("sec-ch-ua-full-version", '"131.0.6778.86"')
    fp.setdefault(
        "sec-ch-ua-full-version-list",
        '"Google Chrome";v="131.0.6778.86", "Not?A_Brand";v="10.0.0.4", "Chromium";v="131.0.6778.86"',
    )
    fp.setdefault("sec-ch-ua-platform-version", '"15.0.0"')
    return fp


def _probe_egress(sess: requests.Session, meter: TrafficMeter, proxy: str | None) -> dict:
    t0 = time.time()
    try:
        r = _metered_request(
            sess,
            meter,
            "get",
            "https://api.ipify.org?format=json",
            timeout=20,
        )
        data = r.json()
        return {"ok": True, "ip": data.get("ip"), "elapsed_ms": int((time.time() - t0) * 1000)}
    except Exception as exc:
        return {
            "ok": False,
            "error": _sanitize_error(exc, 220),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }


def run_cf_probe(
    proxy: str,
    *,
    fp: dict | None = None,
    access_token: str = "",
    account: dict | None = None,
) -> dict[str, Any]:
    """Lightweight home + requirements probe for Webshare CF scan."""
    fp_data = dict(fp or {})
    fp_data.setdefault(
        "user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    fp_data.setdefault("impersonate", "chrome131")
    meter = TrafficMeter()
    cf_obs = empty_cf_observability()
    home_status: int | None = None
    requirements_ok = False
    egress = {"ok": False}
    t0 = time.time()
    sess = requests.Session(impersonate=fp_data["impersonate"], verify=False, timeout=45, proxy=proxy)
    token = str(access_token or "").strip()
    try:
        egress = _probe_egress(sess, meter, proxy)
        if token:
            req, req_cf = _requirements(sess, meter, fp_data, token)
            cf_obs = merge_propagated_cf(req_cf)
            home_status = int(cf_obs.get("home_status") or 0) or None
            requirements_ok = bool(req.token)
        else:
            home = _metered_request(sess, meter, "get", BASE + "/", headers={"User-Agent": fp_data["user-agent"]}, timeout=45)
            home_status = int(getattr(home, "status_code", 0) or 0)
            cf_obs["home_status"] = home_status
            if home_status == 403:
                cf_obs["home_403_soft_fail"] = True
            scripts, build = parse_pow_resources(home.text or "") if home_status < 400 else ([], "")
            p_token = build_legacy_requirements_token(fp_data["user-agent"], scripts, build)
            prep_path = "/backend-api/sentinel/chat-requirements/prepare"
            headers = _hdr(fp_data, prep_path, token) if token else {"User-Agent": fp_data["user-agent"]}
            prep = _metered_request(
                sess,
                meter,
                "post",
                BASE + prep_path,
                headers=headers,
                json={"p": p_token},
                timeout=45,
            )
            try:
                ensure_ok(prep, "chat_requirements_prepare")
                requirements_ok = True
            except Exception as exc:
                _bump_cf_obs(cf_obs, exc, phase="requirements")
    except Exception as exc:
        cf_obs = merge_propagated_cf(cf_obs)
        return {
            "ok": False,
            "proxy_hash": proxy_hash(proxy),
            "egress": egress,
            "home_status": home_status,
            "requirements_ok": requirements_ok,
            "cf_observability": cf_obs,
            "cf_classification": _cf_classification(exc),
            "error": _sanitize_error(exc, 220),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    cf_obs = merge_propagated_cf(cf_obs)
    propagated = int(cf_obs.get("propagated_cf") or 0) > 0
    return {
        "ok": requirements_ok and not propagated,
        "proxy_hash": proxy_hash(proxy),
        "egress": egress,
        "home_status": home_status,
        "requirements_ok": requirements_ok,
        "cf_observability": cf_obs,
        "cf_classification": "cf403" if propagated else "none",
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def run_once(
    secret: dict,
    proxy: str | None,
    mode: str,
    prompt: str,
    *,
    protocol: str = "picture_v2",
    image_gen_deadline: float = 25.0,
    sse_diagnostic_read_secs: float = 90.0,
    out_dir: Path | None = None,
) -> dict:
    meter = TrafficMeter()
    timings: dict[str, int] = {}
    token = str(secret.get("access_token") or "").strip()
    fp = _fp_from_secret(secret)
    protocol = (protocol or "picture_v2").strip().lower()
    if protocol not in ("picture_v2", "spa_tool"):
        protocol = "picture_v2"
    use_picture_v2 = protocol == "picture_v2"
    evidence_dir = Path(out_dir) if out_dir is not None else OUT_DIR
    evidence_dir.mkdir(parents=True, exist_ok=True)
    account_id = account_hash(token)
    proxy_id = proxy_hash(proxy)
    out: dict[str, Any] = {
        "schema_version": "pure-http-image-canary/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "account": {"email": secret.get("email"), "hash": account_id},
        "proxy": {
            "provider": str(secret.get("proxy_provider") or ("webshare" if mode == "panda_webshare" else mode)),
            "hash": proxy_id,
        },
        "prompt": {"chars": len(prompt), "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()},
        "ok": False,
        "protocol": "picture_v2_conduit" if use_picture_v2 else "spa_tool_pure_http",
        "system_hints": ["picture_v2"] if use_picture_v2 else [],
        "sent_x_conduit": False,
        "image_gen_deadline_secs": float(image_gen_deadline),
        "sse_diagnostic_read_secs": float(sse_diagnostic_read_secs),
        "cf_observability": empty_cf_observability(),
        "sse_event_timeline": [],
        "failure_class": None,
        "request_shapes": {},
        "conversation": {
            "id": "",
            "has_image_gen": False,
            "file_ids": [],
            "sediment_ids": [],
        },
        "images": [],
    }
    t_all = time.time()
    sess_kwargs: dict[str, Any] = {
        "impersonate": fp["impersonate"],
        "verify": False,
        "timeout": 120,
    }
    if proxy:
        sess_kwargs["proxy"] = proxy

    last_err = None
    # Canary is deliberately one-shot. Retrying the same account/egress after a
    # 403 increases CF pressure and destroys the meaning of a single result.
    for attempt in range(1, 2):
        try:
            sess = requests.Session(**sess_kwargs)
            # egress
            t0 = time.time()
            egress = _probe_egress(sess, meter, proxy)
            timings["egress_ms"] = int((time.time() - t0) * 1000)
            out["egress"] = egress
            _log(phase="egress", mode=mode, **egress)

            # requirements
            t0 = time.time()
            req, req_cf = _requirements(sess, meter, fp, token)
            timings["requirements_ms"] = int((time.time() - t0) * 1000)
            out["cf_observability"].update(req_cf)
            _log(phase="requirements_ok", mode=mode, ms=timings["requirements_ms"], attempt=attempt)

            tz, offset = "Asia/Tokyo", -540
            # prepare
            t0 = time.time()
            prep_path = "/backend-api/f/conversation/prepare"
            prep_body = _make_image_prepare_body(
                prompt,
                "auto",
                tz,
                offset,
                spa_tool_path=not use_picture_v2,
            )
            prep_headers = _hdr(fp, prep_path, token)
            out["request_shapes"]["prepare"] = {
                "body": body_shape(prep_body),
                "headers": header_shape(prep_headers),
            }
            prep = _metered_request(
                sess,
                meter,
                "post",
                BASE + prep_path,
                headers=prep_headers,
                json=prep_body,
                timeout=60,
            )
            try:
                ensure_ok(prep, prep_path)
            except Exception as exc:
                _bump_cf_obs(out["cf_observability"], exc, phase="requirements")
                raise
            prep_json = prep.json()
            conduit_token = str(prep_json.get("conduit_token") or "").strip()
            timings["prepare_ms"] = int((time.time() - t0) * 1000)
            out["conduit_from_prepare"] = bool(conduit_token)
            if use_picture_v2 and not conduit_token:
                raise RuntimeError("missing_conduit_token_for_picture_v2")
            _log(phase="prepare_ok", mode=mode, ms=timings["prepare_ms"], conduit=out["conduit_from_prepare"])

            # conversation SSE：picture_v2 需 X-Conduit-Token；spa_tool 走文本 shape 不带 conduit
            t0 = time.time()
            path = "/backend-api/f/conversation"
            if use_picture_v2:
                body = _make_image_start_body(prompt, "auto", tz, offset, spa_tool_path=False)
                if build_image_start_headers is not None:
                    headers = _hdr(
                        fp,
                        path,
                        token,
                        build_image_start_headers(req, conduit_token, spa_tool_path=False),
                    )
                else:
                    headers = _hdr(fp, path, token, _make_chat_headers(req))
                    headers["X-Conduit-Token"] = conduit_token
                out["sent_x_conduit"] = bool(headers.get("X-Conduit-Token"))
            else:
                body = _make_image_start_body(prompt, "auto", tz, offset, spa_tool_path=True)
                if build_image_start_headers is None:
                    raise RuntimeError("build_image_start_headers_unavailable")
                headers = _hdr(
                    fp,
                    path,
                    token,
                    build_image_start_headers(req, "", spa_tool_path=True),
                )
            headers["Accept"] = "text/event-stream"
            out["request_shapes"]["start"] = {
                "body": body_shape(body),
                "headers": header_shape(headers),
            }
            resp = _metered_request(
                sess,
                meter,
                "post",
                BASE + path,
                headers=headers,
                json=body,
                timeout=300,
                stream=True,
            )
            try:
                ensure_ok(resp, path)
            except Exception as exc:
                _bump_cf_obs(out["cf_observability"], exc, phase="start")
                raise

            t_sse = time.time()
            total_read = max(float(sse_diagnostic_read_secs), float(image_gen_deadline))
            sse_result = consume_image_sse(
                resp.iter_lines(),
                t0=t_sse,
                gate_secs=float(image_gen_deadline),
                total_read_secs=total_read,
            )
            try:
                resp.close()
            except Exception:
                pass
            meter.resp_bytes += sse_result.sse_bytes
            meter.calls += 1

            cid = sse_result.cid
            has_image_gen = sse_result.has_image_gen_within_gate
            file_ids = list(sse_result.file_ids)
            sediment_ids = list(sse_result.sediment_ids)
            chunks = sse_result.chunks
            image_gen_deadline_hit = sse_result.gate_failed and not sse_result.has_image_gen_within_gate

            timings["conversation_start_ms"] = int((time.time() - t_sse) * 1000) if sse_result.first_event_ms is None else sse_result.first_event_ms
            timings["sse_gate_ms"] = sse_result.gate_ms or int(float(image_gen_deadline) * 1000)
            timings["sse_diagnostic_ms"] = sse_result.diagnostic_ms or 0
            timings["sse_total_ms"] = sse_result.total_ms
            timings["sse_ms"] = sse_result.total_ms
            if sse_result.image_gen_ms is not None:
                timings["sse_image_gen_ms"] = sse_result.image_gen_ms
            if sse_result.first_event_ms is not None:
                timings["sse_first_event_ms"] = sse_result.first_event_ms

            out["sse_event_timeline"] = sse_result.event_timeline
            out["sse_diagnostic"] = {
                "enabled": total_read > float(image_gen_deadline),
                "gate_secs": float(image_gen_deadline),
                "total_read_secs": total_read,
                "late_image_gen_seen": sse_result.late_image_gen_seen,
                "extra_chunks_after_gate": sse_result.extra_chunks_after_gate,
                "diagnostic_stopped_reason": sse_result.diagnostic_stopped_reason,
            }
            out.update(
                {
                    "conversation_id": cid,
                    "sse_chunks": chunks,
                    "sse_bytes": sse_result.sse_bytes,
                    "has_image_gen": has_image_gen,
                    "sse_file_ids": file_ids[:8],
                    "sse_sediment_ids": sediment_ids[:8],
                    "sse_first_event_ms": sse_result.first_event_ms,
                    "image_gen_deadline_hit": image_gen_deadline_hit,
                    "sse_last_payloads": list(sse_result.last_payloads),
                }
            )
            out["conversation"] = {
                "id": cid,
                "has_image_gen": has_image_gen,
                "file_ids": file_ids[:8],
                "sediment_ids": sediment_ids[:8],
            }
            _log(
                phase="sse_done",
                mode=mode,
                ms=timings["sse_ms"],
                cid=cid,
                has_image_gen=has_image_gen,
                file_ids=file_ids[:4],
                chunks=chunks,
                deadline_hit=image_gen_deadline_hit,
                late_image_gen=sse_result.late_image_gen_seen,
            )
            failure_class = classify_image_sse_failure(
                has_image_gen_within_gate=sse_result.has_image_gen_within_gate,
                gate_failed=sse_result.gate_failed,
                late_image_gen_seen=sse_result.late_image_gen_seen,
                tool_args_like_seen=sse_result.tool_args_like_seen,
                quiet_stream=sse_result.quiet_stream,
                chunks=chunks,
            )
            out["failure_class"] = failure_class
            if image_gen_deadline_hit and not has_image_gen:
                raise RuntimeError(
                    f"no_image_gen_within_{int(image_gen_deadline)}s"
                    f"(chunks={chunks}, protocol={protocol}, failure_class={failure_class})"
                )
            if not cid:
                raise RuntimeError("missing_conversation_id")

            # poll + download via OpenAIBackendAPI helpers (same session/proxy)
            t0 = time.time()
            api = OpenAIBackendAPI(access_token=token)
            try:
                api.account = {
                    "email": secret.get("email"),
                    "access_token": token,
                    "fp": secret.get("fp") or {},
                    "proxy": proxy or "",
                    "proxy_provider": "bench",
                    "status": "正常",
                }
                try:
                    api.session.close()
                except Exception:
                    pass
                api.session = sess
                api.fp = fp
                api.user_agent = fp["user-agent"]
                api.device_id = fp["oai-device-id"]
                api.session_id = fp["oai-session-id"]
                api.access_token = token

                # wrap resolve/download to meter traffic
                orig_get = sess.get
                orig_post = sess.post

                def get_m(url, *a, **kw):
                    r = orig_get(url, *a, **kw)
                    if not kw.get("stream"):
                        meter.add_resp(r)
                    meter.calls += 1
                    return r

                def post_m(url, *a, **kw):
                    body = kw.get("data")
                    if body is None and kw.get("json") is not None:
                        body = json.dumps(kw["json"])
                    meter.add_req(body if isinstance(body, (bytes, str)) else None)
                    r = orig_post(url, *a, **kw)
                    if not kw.get("stream"):
                        meter.add_resp(r)
                    return r

                sess.get = get_m  # type: ignore[method-assign]
                sess.post = post_m  # type: ignore[method-assign]

                try:
                    urls = api.resolve_conversation_image_urls(
                        cid,
                        list(file_ids),
                        list(sediment_ids),
                        poll=True,
                        poll_timeout_secs=180.0,
                    )
                except Exception as exc:
                    _bump_cf_obs(out["cf_observability"], exc, phase="tasks")
                    raise
                timings["poll_resolve_ms"] = int((time.time() - t0) * 1000)
                out["download_urls"] = len(urls)
                _log(phase="urls_resolved", mode=mode, ms=timings["poll_resolve_ms"], urls=len(urls))

                t0 = time.time()
                images = api.download_image_bytes(urls)
                timings["download_ms"] = int((time.time() - t0) * 1000)
                for i, blob in enumerate(images):
                    item = persist_image(evidence_dir, i, blob)
                    out["images"].append(item)
                out["download_ok"] = bool(images) and any(_image_magic_ok(b) for b in images)
                out["ok"] = bool(out["download_ok"] and (has_image_gen or file_ids or urls))
                _log(
                    phase="download_done",
                    mode=mode,
                    ms=timings["download_ms"],
                    images=out["images"],
                    ok=out["ok"],
                )
                if out["ok"] and token:
                    try:
                        from services.account_service import account_service

                        account_service.mark_image_result(token, True)
                    except Exception:
                        pass
            finally:
                # do not close sess twice; leave to outer
                api.session = requests.Session()  # detach
                try:
                    api.close()
                except Exception:
                    pass

            break
        except Exception as exc:
            last_err = exc
            _bump_cf_obs(out["cf_observability"], exc, phase="tasks")
            msg = _sanitize_error(exc)
            retryable = any(
                x in msg
                for x in (
                    "TLS",
                    "OPENSSL",
                    "curl: (35)",
                    "Recv failure",
                    "Connection",
                    "SSLEOF",
                    "EOF",
                    "cloudflare_or_edge_html_block",
                    "status=403",
                    "status=429",
                    "status=503",
                )
            )
            _log(phase="attempt_fail", mode=mode, attempt=attempt, error=msg[:260], retryable=retryable)
            break
    else:
        last_err = last_err or RuntimeError("exhausted_attempts")

    timings["total_ms"] = int((time.time() - t_all) * 1000)
    out["timings_ms"] = timings
    out["traffic"] = meter.snapshot()
    out["cf_observability"] = merge_propagated_cf(out.get("cf_observability") or empty_cf_observability())
    out["cf_layers"] = dict(out["cf_observability"])
    if not out.get("ok"):
        out["error"] = _sanitize_error(last_err, 400) if last_err else "failed"
    if int(out["cf_observability"].get("propagated_cf") or 0) > 0:
        out["cf_classification"] = "cf403"
    else:
        out["cf_classification"] = _cf_classification(out.get("error"))
    token = str(secret.get("access_token") or "").strip()
    if token and int(out["cf_observability"].get("propagated_cf") or 0) > 0:
        try:
            from services.proxy_cf_failover import maybe_swap_after_cf_layers

            out["proxy_failover"] = maybe_swap_after_cf_layers(token, out.get("cf_observability"))
        except Exception as exc:
            out["proxy_failover"] = {"ok": False, "error": _sanitize_error(exc, 180)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["local_clash", "panda_direct", "panda_webshare"])
    ap.add_argument("--secret", default=str(SECRET_DEFAULT))
    ap.add_argument("--proxy", default="", help="override proxy URL; empty means mode default")
    ap.add_argument("--prompt", default=MEDIUM_PROMPT)
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--result-file", default="")
    ap.add_argument(
        "--protocol",
        default="picture_v2",
        choices=["picture_v2", "spa_tool"],
        help="picture_v2=conduit+system_hints[picture_v2]（对齐生产/v1/images）；spa_tool=空hints文本shape",
    )
    ap.add_argument(
        "--image-gen-deadline",
        type=float,
        default=25.0,
        help="SSE 内 N 秒无 image_gen 验收门禁（默认 25s）",
    )
    ap.add_argument(
        "--sse-diagnostic-read-secs",
        type=float,
        default=90.0,
        help="SSE 总读取墙钟秒数；gate fail 后继续只读至该时刻（默认 90s）",
    )
    args = ap.parse_args()

    secret = json.loads(Path(args.secret).read_text(encoding="utf-8"))
    if not secret.get("access_token"):
        _log(ok=False, error="missing_access_token")
        return 2

    if args.proxy:
        proxy: str | None = args.proxy
    elif args.mode == "local_clash":
        proxy = "http://127.0.0.1:7897"
    elif args.mode == "panda_direct":
        proxy = None
    else:
        proxy = str(secret.get("proxy") or "").strip() or None
        if not proxy:
            _log(ok=False, error="missing_webshare_proxy_in_secret")
            return 2

    _log(
        phase="start",
        mode=args.mode,
        email=secret.get("email"),
        proxy_hash=proxy_hash(proxy),
        prompt_chars=len(args.prompt),
        protocol=args.protocol,
        image_gen_deadline=args.image_gen_deadline,
    )
    result = run_once(
        secret,
        proxy,
        args.mode,
        args.prompt,
        protocol=args.protocol,
        image_gen_deadline=args.image_gen_deadline,
        sse_diagnostic_read_secs=args.sse_diagnostic_read_secs,
        out_dir=Path(args.out_dir),
    )
    output_dir = Path(args.out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.result_file) if args.result_file else output_dir / f"result_{args.mode}_{int(time.time())}.json"
    write_evidence(out_path, result)
    _log(phase="done", path=str(out_path), ok=bool(result.get("ok")), timings=result.get("timings_ms"), traffic=result.get("traffic"))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
