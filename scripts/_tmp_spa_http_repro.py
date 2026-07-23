#!/usr/bin/env python3
"""HTTP repro of ChatGPT conversation/image via curl_cffi — fixed Clash + fixed account.

Does NOT rotate accounts. Uses access_token from local secret.
Proxy forced to http://127.0.0.1:7897 regardless of account sticky proxy.

Usage:
  python scripts/_tmp_spa_http_repro.py --text
  python scripts/_tmp_spa_http_repro.py --image
  python scripts/_tmp_spa_http_repro.py --text --image
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from curl_cffi import requests  # noqa: E402

from services.openai_backend_api import OpenAIBackendAPI  # noqa: E402
from utils.helper import iter_sse_payloads  # noqa: E402

SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
OUT = ROOT / "data" / "runlogs" / "spa_repro"
PROXY = "http://127.0.0.1:7897"


def _force_clash_proxy(api: OpenAIBackendAPI) -> None:
    """Force Clash on account; rebuild Session with proxy= (curl_cffi preferred)."""
    api.account = dict(api.account or {})
    api.account["proxy"] = PROXY
    api.account["proxy_provider"] = "clash_local"
    api.account["lifecycle_ip_mode"] = "experiment_clash_fixed"
    try:
        api.session.close()
    except Exception:
        pass
    api.session = requests.Session(
        impersonate=str((api.fp or {}).get("impersonate") or "chrome131"),
        proxy=PROXY,
        verify=False,
        timeout=90,
    )
    api.session.headers.update(
        {
            "User-Agent": api.user_agent,
            "Accept-Language": str((api.fp or {}).get("accept-language") or "en-US,en;q=0.9"),
        }
    )
    api._resource_session = None


def _summarize_exc(exc: BaseException) -> dict:
    msg = str(exc)
    low = msg.lower()
    cf = any(x in low for x in ("cloudflare", "cf_edge", "just a moment", "attention required"))
    return {
        "error_type": type(exc).__name__,
        "error": msg[:400],
        "looks_like_cf": cf,
    }


def run_text(api: OpenAIBackendAPI) -> dict:
    out: dict = {"ok": False, "purpose": "text"}
    t0 = time.time()
    try:
        chunks: list[str] = []
        for payload in api.stream_conversation(prompt="Reply with exactly: PONG", model="auto"):
            chunks.append(payload)
            if len(chunks) >= 8:
                break
        out["ok"] = len(chunks) > 0
        out["sse_chunks"] = len(chunks)
        out["first_payload_prefix"] = (chunks[0][:200] if chunks else "")
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
    except Exception as exc:
        out.update(_summarize_exc(exc))
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
    return out


def run_image(api: OpenAIBackendAPI) -> dict:
    out: dict = {"ok": False, "purpose": "image"}
    t0 = time.time()
    try:
        chunks: list[str] = []
        file_ids: list[str] = []
        conversation_id = ""
        has_image_tool = False
        for payload in api.stream_conversation(
            prompt="Generate a simple flat blue circle icon, no text",
            model="gpt-4o",
            system_hints=["picture_v2"],
        ):
            chunks.append(payload)
            low = payload.lower()
            if "image_gen" in low or "tool_invoked" in low or "sediment://" in low:
                has_image_tool = True
            if not conversation_id:
                m = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
                if m:
                    conversation_id = m.group(1)
            for m in re.finditer(
                r'"file_id"\s*:\s*"([^"]+)"|file-(service|[\w-]+)',
                payload,
            ):
                if m.group(1):
                    file_ids.append(m.group(1))
            if len(chunks) >= 40 and (conversation_id or file_ids):
                break
        out["sse_chunks"] = len(chunks)
        out["conversation_id"] = conversation_id
        out["file_ids"] = list(dict.fromkeys(file_ids))[:8]
        out["has_image_tool"] = has_image_tool
        # A conversation id only proves submit acceptance.  It does not prove
        # that the upstream actually invoked the image tool.
        out["ok"] = bool(has_image_tool or file_ids)
        out["first_payload_prefix"] = (chunks[0][:200] if chunks else "")
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
    except Exception as exc:
        out.update(_summarize_exc(exc))
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--image", action="store_true")
    args = ap.parse_args()
    if not args.text and not args.image:
        args.text = True

    secret = json.loads(SECRET.read_text(encoding="utf-8"))
    token = str(secret.get("access_token") or "").strip()
    if not token:
        print(json.dumps({"ok": False, "error": "missing_access_token"}), flush=True)
        return 2

    # probe egress with same Session style
    try:
        ip = requests.get(
            "https://api.ipify.org?format=json",
            proxy=PROXY,
            impersonate="chrome131",
            timeout=20,
        ).json()
    except Exception as exc:
        ip = {"error": str(exc)[:160]}
    print(json.dumps({"phase": "egress", "proxy": PROXY, "ip": ip, "email": secret.get("email")}, ensure_ascii=False), flush=True)

    # Seed account blob so constructor picks Clash via proxy_settings
    seeded = {
        "email": secret.get("email"),
        "access_token": token,
        "fp": secret.get("fp") or {},
        "proxy": PROXY,
        "proxy_provider": "clash_local",
        "status": "正常",
    }
    api = OpenAIBackendAPI(access_token=token)
    if not api.account:
        api.account = seeded
        api.fp = api._build_fp()
        api.user_agent = api.fp["user-agent"]
        api.device_id = api.fp["oai-device-id"]
        api.session_id = api.fp["oai-session-id"]
    _force_clash_proxy(api)

    results: dict = {"proxy": PROXY, "email": secret.get("email"), "runs": []}
    try:
        if args.text:
            r = run_text(api)
            results["runs"].append(r)
            print(json.dumps({"phase": "text_result", **r}, ensure_ascii=False), flush=True)
        if args.image:
            r = run_image(api)
            results["runs"].append(r)
            print(json.dumps({"phase": "image_result", **r}, ensure_ascii=False), flush=True)
    finally:
        try:
            api.close()
        except Exception:
            pass

    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / f"http_repro_{int(time.time())}.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"phase": "done", "path": str(out_path), "ok": all(x.get("ok") for x in results["runs"])}, ensure_ascii=False), flush=True)
    return 0 if results["runs"] and all(x.get("ok") for x in results["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
