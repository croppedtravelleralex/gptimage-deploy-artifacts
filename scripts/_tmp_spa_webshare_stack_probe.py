#!/usr/bin/env python3
"""差 IP Webshare：curl_cffi cold/warm cookie 对照 + Camoufox 页面暖机结果记录。

Camoufox→Webshare 打开 chatgpt.com 可能 NS_ERROR_NET_RESET（本机观测）；
curl_cffi 同出口仍可 prepare（与 bench3 一致）。不宣称 CF 绕过。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from curl_cffi import requests  # noqa: E402

from services.openai_backend_api import DEFAULT_CLIENT_BUILD_NUMBER, DEFAULT_CLIENT_VERSION  # noqa: E402
from utils.helper import anonymize_token, new_uuid  # noqa: E402
from utils.pow import build_legacy_requirements_token, parse_pow_resources  # noqa: E402

SECRET = ROOT / "data" / "runlogs" / "spa_repro" / "qaflow_secret.json"
OUT_DIR = ROOT / "data" / "runlogs" / "spa_repro" / "bench3"
BASE = "https://chatgpt.com"


def _log(**kw: Any) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def _fp(secret: dict) -> dict:
    fp = dict(secret.get("fp") or {})
    fp.setdefault(
        "user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    fp.setdefault("impersonate", "chrome131")
    fp.setdefault("oai-device-id", new_uuid())
    fp.setdefault("oai-session-id", new_uuid())
    return fp


def _hdr(fp: dict, token: str, cookie: str = "") -> dict:
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": fp["user-agent"],
        "Authorization": f"Bearer {token}",
        "OAI-Device-Id": fp["oai-device-id"],
        "OAI-Session-Id": fp["oai-session-id"],
        "OAI-Client-Version": DEFAULT_CLIENT_VERSION,
        "OAI-Client-Build-Number": DEFAULT_CLIENT_BUILD_NUMBER,
        "Origin": BASE,
        "Referer": BASE + "/",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def _prepare(fp: dict, token: str, proxy: str, cookie: str, label: str) -> dict[str, Any]:
    proxies = {"http": proxy, "https": proxy}
    out: dict[str, Any] = {"label": label, "ok": False}
    for attempt in range(1, 6):
        try:
            s = requests.Session(impersonate=fp["impersonate"])
            home = s.get(
                BASE + "/",
                headers={"User-Agent": fp["user-agent"], **({"Cookie": cookie} if cookie else {})},
                proxies=proxies,
                timeout=90,
            )
            scripts, build = parse_pow_resources(home.text or "")
            p = build_legacy_requirements_token(fp["user-agent"], scripts, build)
            prep = s.post(
                BASE + "/backend-api/sentinel/chat-requirements/prepare",
                headers=_hdr(fp, token, cookie),
                json={"p": p},
                proxies=proxies,
                timeout=90,
            )
            out.update(
                {
                    "home_status": home.status_code,
                    "req_prepare_status": prep.status_code,
                    "ok": prep.status_code == 200,
                    "attempt": attempt,
                    "home_bytes": len(home.text or ""),
                }
            )
            _log(phase="curl", **{k: out[k] for k in ("label", "home_status", "req_prepare_status", "ok", "attempt")})
            return out
        except Exception as exc:
            _log(phase="retry", label=label, attempt=attempt, error=str(exc)[:160])
            time.sleep(0.8)
    out["error"] = "exhausted"
    return out


def _try_camoufox(proxy: str) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "stack": "camoufox_page"}
    try:
        from camoufox.sync_api import Camoufox
        from urllib.parse import urlparse as _u

        p = _u(proxy)
        cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port or 80}"}
        if p.username:
            cfg["username"] = p.username
        if p.password:
            cfg["password"] = p.password
        with Camoufox(headless=True, proxy=cfg, geoip=True) as browser:
            page = browser.new_page()
            try:
                page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=45000)
                out["egress_ip"] = json.loads(page.inner_text("body") or "{}").get("ip")
            except Exception as exc:
                out["egress_error"] = str(exc)[:120]
            try:
                resp = page.goto(BASE + "/", wait_until="domcontentloaded", timeout=90000)
                out["home_status"] = int(resp.status if resp else 0)
                out["ok"] = out["home_status"] in (200, 304)
            except Exception as exc:
                out["home_error"] = str(exc)[:160]
                out["ok"] = False
    except Exception as exc:
        out["error"] = str(exc)[:200]
    _log(phase="camoufox", **out)
    return out


def main() -> int:
    secret = json.loads(SECRET.read_text(encoding="utf-8"))
    proxy = str(secret.get("proxy") or "")
    token = str(secret.get("access_token") or "")
    fp = _fp(secret)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _log(phase="start", proxy_host=urlparse(proxy).hostname)

    cam = _try_camoufox(proxy)
    cold = _prepare(fp, token, proxy, "", "cold_empty_cookie")
    # optional: reuse Clash-warmed cookie names only via empty — warm cookie from Camoufox if any
    warm_cookie = ""
    warm = _prepare(fp, token, proxy, warm_cookie, "warm_same_as_cold") if not cam.get("ok") else cold

    report = {
        "mode": "webshare_badip_warm_probe",
        "proxy_host": urlparse(proxy).hostname,
        "email": secret.get("email"),
        "token_fp": anonymize_token(token),
        "camoufox": cam,
        "curl_cold": cold,
        "note": "Camoufox page may NET_RESET; curl_cffi prepare is the workable stack on this DC IP (see bench3).",
        "ok": bool(cold.get("ok")),
    }
    path = OUT_DIR / f"warm_webshare_curl_{int(time.time())}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(phase="done", path=str(path), curl_ok=cold.get("ok"), camoufox_ok=cam.get("ok"), home_error=cam.get("home_error"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
